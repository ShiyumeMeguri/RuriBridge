# -*- coding: utf-8 -*-
"""The shared-memory arena both hosts address.

Everything the bridge moves lives in one session directory whose files are
mapped into both processes. On Windows a mapped file *is* a section object, so
two processes mapping the same file address the same physical pages: what
Blender writes into a mapped page is the page Painter's importer reads, with no
serialise / write / read round trip in between. Every file the arena creates
carries FILE_ATTRIBUTE_TEMPORARY, which tells the cache manager not to write it
back to storage while there is memory to hold it, so a "file" here is a name for
a region of RAM that both processes can already see.

The mutable state is deliberately tiny: one fixed-size control map with one slot
per channel. Payloads are immutable generation directories -- a generation is
written in full and only then published by bumping its slot -- so a reader never
needs a lock on a payload. Only the handful of integers inside a slot are
guarded, by a seqlock: the writer makes the sequence odd, edits, then makes it
even, and a reader that sees an odd or a changed sequence reads again.

The store-ordering the seqlock relies on holds because both hosts are x86-64,
where stores are not reordered with other stores.

Windows only, by construction: the mechanism above is Win32 section semantics,
and the two hosts this bridges are Windows applications.
"""

from __future__ import annotations

import ctypes
import errno
import json
import mmap
import os
import shutil
import struct
import time
from pathlib import Path

from .log import logger

LOG = logger("arena")

CONTROL_MAGIC = b"RURIBRDG"
FORMAT_VERSION = 3
CONTROL_FILE_NAME = "control.bin"
SESSION_DIRECTORY_NAME = "RuriDccBridge"
ROOT_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_ROOT"
DEFAULT_SESSION = "default"

HEADER_SIZE = 64
SLOT_SIZE = 128
CHANNEL_NAME_SIZE = 32
INLINE_CAPACITY = 8192

SLOT_OFFSET_SEQUENCE = 32
SLOT_OFFSET_GENERATION = 40
SLOT_OFFSET_PAYLOAD_BYTES = 48
SLOT_OFFSET_ACKNOWLEDGED = 56
SLOT_OFFSET_DROPPED = 64
SLOT_OFFSET_WRITER_PROCESS = 72
SLOT_OFFSET_INLINE_BYTES = 80
SLOT_OFFSET_HEARTBEAT = 88

MAX_OUTSTANDING_GENERATIONS = 8
SEQLOCK_READ_ATTEMPTS = 64
PRESENCE_SECONDS = 3.0

FILE_ATTRIBUTE_TEMPORARY = 0x00000100

_HEADER = struct.Struct("<8sIIQ")
_UNSIGNED_64 = struct.Struct("<Q")
_UNSIGNED_32 = struct.Struct("<I")


class ArenaError(RuntimeError):
    """Any refusal to build or attach to a session."""


def _require_windows():
    if os.name != "nt":
        raise ArenaError(
            "the arena is built on Win32 section semantics and both hosts it "
            "bridges are Windows applications; this platform is {0}".format(os.name))


def _mark_temporary(path):
    """Ask the cache manager to keep this file's pages in memory."""
    handle = ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_TEMPORARY)
    if not handle:
        LOG.debug("could not mark %s temporary: %s", path, ctypes.GetLastError())


def default_root():
    """Where sessions live unless the environment says otherwise.

    Not the temporary folder, even though the files carry the temporary
    attribute: those two things are unrelated. The attribute asks the cache
    manager to keep the pages in memory, while the folder decides who may delete
    them -- and a consumer whose images point at a payload here would lose them
    to a disk cleanup it never asked for.
    """
    override = os.environ.get(ROOT_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / SESSION_DIRECTORY_NAME / "sessions"


class SlotState:
    """One consistent read of a channel slot."""

    __slots__ = ("channel", "sequence", "generation", "payload_bytes",
                 "acknowledged_generation", "dropped_generations", "writer_process_id",
                 "heartbeat")

    def __init__(self, channel, sequence, generation, payload_bytes,
                 acknowledged_generation, dropped_generations, writer_process_id,
                 heartbeat=0):
        self.channel = channel
        self.sequence = sequence
        self.generation = generation
        self.payload_bytes = payload_bytes
        self.acknowledged_generation = acknowledged_generation
        self.dropped_generations = dropped_generations
        self.writer_process_id = writer_process_id
        self.heartbeat = heartbeat

    @property
    def writer_is_live(self):
        """Whether the side that owns this channel is attached right now.

        A stamp the writer refreshes on every pump, rather than a process
        lookup: the question is not whether an application is running but
        whether its half of the bridge is attached, and only the plugin itself
        can answer that.
        """
        if not self.heartbeat:
            return False
        return (time.time_ns() - self.heartbeat) < PRESENCE_SECONDS * 1e9

    def __repr__(self):
        return ("SlotState(channel={0!r}, sequence={1}, generation={2}, "
                "payload_bytes={3}, acknowledged={4}, dropped={5}, writer={6})").format(
                    self.channel, self.sequence, self.generation, self.payload_bytes,
                    self.acknowledged_generation, self.dropped_generations,
                    self.writer_process_id)


class Arena:
    """A mapped session: the control block plus its generation directories."""

    def __init__(self, root, session, channels, control_file, control_map, epoch):
        self.root = Path(root)
        self.session = session
        self.channels = tuple(channels)
        self.directory = self.root / session
        self._control_file = control_file
        self._control = control_map
        self.epoch = epoch
        self._index_by_channel = {name: index for index, name in enumerate(self.channels)}

    @classmethod
    def open_session(cls, channels, session=DEFAULT_SESSION, root=None):
        """Attach to the named session, building it if nobody has yet.

        Whichever host starts first builds; the other attaches. The build is a
        write-then-rename so a half-written control block is never visible.
        """
        _require_windows()
        root = Path(root) if root is not None else default_root()
        directory = root / session
        directory.mkdir(parents=True, exist_ok=True)
        _mark_temporary(directory)
        control_path = directory / CONTROL_FILE_NAME
        if not control_path.exists():
            cls._materialise_control(control_path, channels)
        return cls._attach(root, session, channels, control_path)

    @classmethod
    def attach_existing(cls, session=DEFAULT_SESSION, root=None):
        """Attach to a session that already exists, reading its channel list."""
        _require_windows()
        root = Path(root) if root is not None else default_root()
        control_path = root / session / CONTROL_FILE_NAME
        if not control_path.exists():
            raise ArenaError("no session at {0}".format(control_path))
        channels = cls._read_channel_names(control_path)
        return cls._attach(root, session, channels, control_path)

    @staticmethod
    def _control_size(channel_count):
        return HEADER_SIZE + (SLOT_SIZE + INLINE_CAPACITY) * channel_count

    @staticmethod
    def _inline_base(channel_count, index):
        return HEADER_SIZE + SLOT_SIZE * channel_count + INLINE_CAPACITY * index

    @classmethod
    def _materialise_control(cls, control_path, channels):
        size = cls._control_size(len(channels))
        block = bytearray(size)
        epoch = time.time_ns()
        _HEADER.pack_into(block, 0, CONTROL_MAGIC, FORMAT_VERSION, len(channels), epoch)
        for index, name in enumerate(channels):
            encoded = name.encode("ascii")
            if len(encoded) >= CHANNEL_NAME_SIZE:
                raise ArenaError("channel name {0!r} does not fit in {1} bytes".format(
                    name, CHANNEL_NAME_SIZE))
            base = HEADER_SIZE + index * SLOT_SIZE
            block[base:base + len(encoded)] = encoded
        staging = control_path.with_name(control_path.name + ".{0}.staging".format(os.getpid()))
        with open(staging, "wb") as handle:
            handle.write(block)
        _mark_temporary(staging)
        try:
            os.replace(staging, control_path)
        except OSError as error:
            os.unlink(staging)
            if not control_path.exists():
                raise ArenaError("could not publish control block: {0}".format(error))
        _mark_temporary(control_path)

    @classmethod
    def _read_channel_names(cls, control_path):
        with open(control_path, "rb") as handle:
            header = handle.read(HEADER_SIZE)
            magic, version, count, _epoch = _HEADER.unpack_from(header, 0)
            if magic != CONTROL_MAGIC:
                raise ArenaError("{0} is not a bridge control block".format(control_path))
            if version != FORMAT_VERSION:
                raise ArenaError(
                    "control block at {0} is format {1}, this build speaks {2}".format(
                        control_path, version, FORMAT_VERSION))
            names = []
            for _ in range(count):
                slot = handle.read(SLOT_SIZE)
                names.append(slot[:CHANNEL_NAME_SIZE].rstrip(b"\x00").decode("ascii"))
        return tuple(names)

    @classmethod
    def _attach(cls, root, session, channels, control_path):
        expected = cls._control_size(len(channels))
        control_file = open(control_path, "r+b")
        try:
            control_map = mmap.mmap(control_file.fileno(), expected, access=mmap.ACCESS_WRITE)
        except (ValueError, OSError) as error:
            control_file.close()
            raise ArenaError("could not map {0}: {1}".format(control_path, error))
        magic, version, count, epoch = _HEADER.unpack_from(control_map, 0)
        if magic != CONTROL_MAGIC or version != FORMAT_VERSION:
            control_map.close()
            control_file.close()
            raise ArenaError(
                "control block at {0} is magic {1!r} format {2}; expected {3!r} format {4}".format(
                    control_path, magic, version, CONTROL_MAGIC, FORMAT_VERSION))
        if count != len(channels):
            control_map.close()
            control_file.close()
            raise ArenaError(
                "session {0} carries {1} channels, this build declares {2}".format(
                    session, count, len(channels)))
        arena = cls(root, session, channels, control_file, control_map, epoch)
        LOG.debug("attached to session %s at %s (epoch %d)", session, arena.directory, epoch)
        return arena

    def close(self):
        self._control.close()
        self._control_file.close()

    def __enter__(self):
        return self

    def __exit__(self, error_type, error_value, traceback):
        self.close()
        return False

    def slot_index(self, channel):
        try:
            return self._index_by_channel[channel]
        except KeyError:
            raise ArenaError("session {0} has no channel {1!r}; it has {2}".format(
                self.session, channel, ", ".join(self.channels)))

    def _slot_base(self, channel):
        return HEADER_SIZE + self.slot_index(channel) * SLOT_SIZE

    def _read_unsigned_64(self, offset):
        return _UNSIGNED_64.unpack_from(self._control, offset)[0]

    def _write_unsigned_64(self, offset, value):
        _UNSIGNED_64.pack_into(self._control, offset, value)

    def read_slot(self, channel):
        """A torn-free read of one slot, retried until the seqlock settles."""
        base = self._slot_base(channel)
        for _ in range(SEQLOCK_READ_ATTEMPTS):
            first = self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE)
            if first % 2:
                continue
            generation = self._read_unsigned_64(base + SLOT_OFFSET_GENERATION)
            payload_bytes = self._read_unsigned_64(base + SLOT_OFFSET_PAYLOAD_BYTES)
            acknowledged = self._read_unsigned_64(base + SLOT_OFFSET_ACKNOWLEDGED)
            dropped = self._read_unsigned_64(base + SLOT_OFFSET_DROPPED)
            writer = _UNSIGNED_32.unpack_from(self._control, base + SLOT_OFFSET_WRITER_PROCESS)[0]
            heartbeat = self._read_unsigned_64(base + SLOT_OFFSET_HEARTBEAT)
            if self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE) == first:
                return SlotState(channel, first, generation, payload_bytes,
                                 acknowledged, dropped, writer, heartbeat)
        raise ArenaError(
            "slot {0!r} never settled in {1} attempts; a writer is wedged mid-publish".format(
                channel, SEQLOCK_READ_ATTEMPTS))

    def publish(self, channel, generation, payload_bytes, dropped_generations=None):
        """Make a finished generation visible, under the seqlock."""
        base = self._slot_base(channel)
        sequence = self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE)
        self._write_unsigned_64(base + SLOT_OFFSET_SEQUENCE, sequence + 1)
        self._write_unsigned_64(base + SLOT_OFFSET_GENERATION, generation)
        self._write_unsigned_64(base + SLOT_OFFSET_PAYLOAD_BYTES, payload_bytes)
        if dropped_generations is not None:
            self._write_unsigned_64(base + SLOT_OFFSET_DROPPED, dropped_generations)
        _UNSIGNED_32.pack_into(self._control, base + SLOT_OFFSET_WRITER_PROCESS, os.getpid())
        self._write_unsigned_64(base + SLOT_OFFSET_SEQUENCE, sequence + 2)

    def touch(self, channel):
        """Say "my half is still attached". One aligned store, no seqlock.

        Deliberately outside the seqlock: a torn read of a timestamp can only
        make presence look slightly stale, which the next pump corrects, and
        making every pump take the lock would put a write on the hot path for
        a value nobody makes decisions on beyond "recent or not".
        """
        base = HEADER_SIZE + self.slot_index(channel) * SLOT_SIZE
        self._write_unsigned_64(base + SLOT_OFFSET_HEARTBEAT, time.time_ns())
        _UNSIGNED_32.pack_into(self._control, base + SLOT_OFFSET_WRITER_PROCESS, os.getpid())

    def acknowledge(self, channel, generation):
        """Record how far the reader has consumed. One aligned store, no lock."""
        base = self._slot_base(channel)
        self._write_unsigned_64(base + SLOT_OFFSET_ACKNOWLEDGED, generation)

    def write_state(self, channel, payload):
        """Publish a small record entirely inside the mapped control block.

        For state that only ever means "the latest value" -- a handful of shader
        uniforms, say -- a generation directory is the wrong shape: it costs a
        directory and a file per change, and it preserves an ordering nobody
        reads, because a superseded value has no meaning. This writes the bytes
        into pages both processes already have mapped, under the same seqlock
        that guards the rest of the slot, so a change costs no filesystem at all.
        """
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > INLINE_CAPACITY:
            raise ArenaError(
                "state record for {0} is {1} bytes and the inline area holds {2}; it "
                "belongs in a generation, not in the control block".format(
                    channel, len(encoded), INLINE_CAPACITY))
        index = self.slot_index(channel)
        base = HEADER_SIZE + index * SLOT_SIZE
        inline = self._inline_base(len(self.channels), index)
        sequence = self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE)
        self._write_unsigned_64(base + SLOT_OFFSET_SEQUENCE, sequence + 1)
        self._control[inline:inline + len(encoded)] = encoded
        self._write_unsigned_64(base + SLOT_OFFSET_INLINE_BYTES, len(encoded))
        self._write_unsigned_64(base + SLOT_OFFSET_GENERATION,
                                self._read_unsigned_64(base + SLOT_OFFSET_GENERATION) + 1)
        _UNSIGNED_32.pack_into(self._control, base + SLOT_OFFSET_WRITER_PROCESS, os.getpid())
        self._write_unsigned_64(base + SLOT_OFFSET_SEQUENCE, sequence + 2)

    def read_state(self, channel):
        """The latest inline record and its generation, or (None, 0)."""
        index = self.slot_index(channel)
        base = HEADER_SIZE + index * SLOT_SIZE
        inline = self._inline_base(len(self.channels), index)
        for _ in range(SEQLOCK_READ_ATTEMPTS):
            first = self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE)
            if first % 2:
                continue
            length = self._read_unsigned_64(base + SLOT_OFFSET_INLINE_BYTES)
            generation = self._read_unsigned_64(base + SLOT_OFFSET_GENERATION)
            if length == 0:
                if self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE) == first:
                    return None, generation
                continue
            encoded = bytes(self._control[inline:inline + length])
            if self._read_unsigned_64(base + SLOT_OFFSET_SEQUENCE) != first:
                continue
            return json.loads(encoded.decode("utf-8")), generation
        raise ArenaError("state slot {0!r} never settled in {1} attempts".format(
            channel, SEQLOCK_READ_ATTEMPTS))

    def channel_directory(self, channel):
        path = self.directory / channel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def generation_directory(self, channel, generation):
        return self.channel_directory(channel) / "{0:016d}".format(generation)

    def existing_generations(self, channel):
        directory = self.channel_directory(channel)
        found = []
        for entry in directory.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                found.append(int(entry.name))
        found.sort()
        return found

    def discard_generation(self, channel, generation):
        path = self.generation_directory(channel, generation)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def create_mapped_file(self, path, size):
        """Create a file of exactly ``size`` bytes and map it read/write.

        The returned map is the producer's writing surface and the consumer's
        reading surface at once: filling it is the only time these bytes are
        ever written.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)
        _mark_temporary(path)
        handle = open(path, "r+b")
        try:
            mapped = mmap.mmap(handle.fileno(), size, access=mmap.ACCESS_WRITE)
        except (ValueError, OSError) as error:
            handle.close()
            raise ArenaError("could not map {0} at {1} bytes: {2}".format(path, size, error))
        return handle, mapped

    def describe(self):
        """Every slot, for the CLI and for the two panels."""
        return [self.read_slot(channel) for channel in self.channels]


def list_sessions(root=None):
    """Session names present under the root, newest control block first."""
    root = Path(root) if root is not None else default_root()
    if not root.exists():
        return []
    found = []
    for entry in root.iterdir():
        control = entry / CONTROL_FILE_NAME
        if entry.is_dir() and control.exists():
            found.append((control.stat().st_mtime, entry.name))
    found.sort(reverse=True)
    return [name for _mtime, name in found]


def remove_session(session=DEFAULT_SESSION, root=None):
    """Delete a session directory outright. Refuses while it is still mapped."""
    root = Path(root) if root is not None else default_root()
    directory = root / session
    if not directory.exists():
        return False
    try:
        shutil.rmtree(directory)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EBUSY):
            raise ArenaError(
                "session {0} is still mapped by a running host; close Blender or "
                "Painter first".format(session))
        raise
    return True
