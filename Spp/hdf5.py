# -*- coding: utf-8 -*-
"""The named byte datasets of an HDF5 file, read and replaced in place.

Covers what a Painter project holds and nothing else: superblock version 0, symbol-table groups, version 1
object headers, and one-dimensional byte datasets stored contiguous or chunked, without filters. Anything
outside that raises ``Hdf5Error`` instead of being guessed at.

Replacing a dataset keeps its storage class. A contiguous block is rewritten where it lies when the new
bytes fit and moved to the end of the file when they do not. A chunked dataset keeps the chunks it has and
takes the extra ones from the end of the file; its chunk B-tree is written anew there when the set of chunks
changed. What the old tree and the old block leave behind is not reclaimed, as HDF5 itself does not reclaim
it in files of this version.
"""

from __future__ import annotations

import struct
from collections import namedtuple

SIGNATURE = b"\x89HDF\r\n\x1a\n"
UNDEFINED = 0xFFFFFFFFFFFFFFFF

_DATASPACE = 0x01
_DATATYPE = 0x03
_LAYOUT = 0x08
_FILTERS = 0x0B
_ATTRIBUTE = 0x0C
_CONTINUATION = 0x10
_SYMBOL_TABLE = 0x11

_CONTIGUOUS = 1
_CHUNKED = 2

_SUPERBLOCK_SIZE = 96
_END_OF_FILE = 40
_ROOT_CACHE_TYPE = 72
_ROOT_SCRATCH = 80
_SYMBOL_ENTRY = 40
_CACHED_GROUP = 1

#: The default half-width of a chunk B-tree node, which a version 0 superblock has no field to change.
_TREE_HALF_WIDTH = 32
_TREE_ENTRIES = 2 * _TREE_HALF_WIDTH
_TREE_HEADER = 24
_TREE_KEY = 24
_TREE_NODE = _TREE_HEADER + _TREE_ENTRIES * 8 + (_TREE_ENTRIES + 1) * _TREE_KEY

Message = namedtuple("Message", "kind body size")
Entry = namedtuple("Entry", "offset address size")
Storage = namedtuple("Storage", "kind length limit address size chunk length_at layout_at")


class Hdf5Error(ValueError):
    """A file this module does not read, or a request it does not honour."""


def _aligned(size):
    return (size + 7) // 8 * 8


class File:
    """An HDF5 file opened for reading, or for reading and replacing its datasets."""

    def __init__(self, path, writable=False):
        self.path = path
        self.writable = writable
        self._dirty = False
        self._children = {}
        self._file = open(path, "r+b" if writable else "rb")
        try:
            self._read_superblock()
        except BaseException:
            self._file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def close(self):
        if self._file.closed:
            return
        if self.writable and self._dirty:
            self._write(_END_OF_FILE, struct.pack("<Q", self._end))
            self._file.truncate(self._end)
        self._file.close()

    def _read_superblock(self):
        head = self._read(0, _SUPERBLOCK_SIZE)
        if head[:8] != SIGNATURE or any(head[8:13]) or head[13] != 8 or head[14] != 8:
            raise Hdf5Error("not a version 0 HDF5 file with 8-byte offsets and lengths")
        if struct.unpack_from("<I", head, _ROOT_CACHE_TYPE)[0] != _CACHED_GROUP:
            raise Hdf5Error("the root group does not cache its tree and heap")
        self._root = struct.unpack_from("<QQ", head, _ROOT_SCRATCH)
        self._end = struct.unpack_from("<Q", head, _END_OF_FILE)[0]
        self._file.seek(0, 2)
        if self._file.tell() < self._end:
            raise Hdf5Error("the file is shorter than the {0} bytes its superblock says".format(self._end))

    def _read(self, offset, size):
        self._file.seek(offset)
        data = self._file.read(size)
        if len(data) != size:
            raise Hdf5Error("the file ends inside the {0} bytes at {1}".format(size, offset))
        return data

    def _write(self, offset, data):
        self._file.seek(offset)
        self._file.write(data)

    def _allocate(self, size):
        address = self._end
        self._end += size
        self._dirty = True
        return address

    def _text(self, offset):
        found = b""
        while True:
            self._file.seek(offset + len(found))
            block = self._file.read(128)
            end = block.find(b"\x00")
            if end >= 0:
                return (found + block[:end]).decode("utf-8")
            if not block:
                raise Hdf5Error("a name at {0} is not terminated".format(offset))
            found += block

    def _walk(self, node, heap, found):
        head = self._read(node, _TREE_HEADER)
        if head[:4] != b"TREE" or head[4] != 0:
            raise Hdf5Error("no group tree node at {0}".format(node))
        used = struct.unpack_from("<H", head, 6)[0]
        body = self._read(node + _TREE_HEADER, used * 16 + 8)
        names = struct.unpack("<Q", self._read(heap + 24, 8))[0]
        for index in range(used):
            child = struct.unpack_from("<Q", body, index * 16 + 8)[0]
            if head[5]:
                self._walk(child, heap, found)
                continue
            block = self._read(child, 8)
            if block[:4] != b"SNOD":
                raise Hdf5Error("no symbol node at {0}".format(child))
            count = struct.unpack_from("<H", block, 6)[0]
            entries = self._read(child + 8, count * _SYMBOL_ENTRY)
            for symbol in range(count):
                at = symbol * _SYMBOL_ENTRY
                name_offset, header, cache = struct.unpack_from("<QQI", entries, at)
                scratch = struct.unpack_from("<QQ", entries, at + 24) if cache == _CACHED_GROUP else None
                found[self._text(names + name_offset)] = (header, scratch)

    def _group(self, group):
        if group not in self._children:
            found = {}
            self._walk(group[0], group[1], found)
            self._children[group] = found
        return self._children[group]

    def _messages(self, header):
        head = self._read(header, 16)
        if head[0] != 1:
            raise Hdf5Error("object header version {0} at {1}".format(head[0], header))
        count = struct.unpack_from("<H", head, 2)[0]
        blocks = [(header + 16, struct.unpack_from("<I", head, 8)[0])]
        found = []
        while blocks and len(found) < count:
            start, length = blocks.pop(0)
            block = self._read(start, length)
            position = 0
            while position + 8 <= length and len(found) < count:
                kind, size = struct.unpack_from("<HH", block, position)
                if kind == _CONTINUATION:
                    blocks.append(struct.unpack_from("<QQ", block, position + 8))
                found.append(Message(kind, start + position + 8, size))
                position += 8 + size
        return found

    def _subgroup(self, header, scratch):
        if scratch is not None:
            return scratch
        for message in self._messages(header):
            if message.kind == _SYMBOL_TABLE:
                return struct.unpack("<QQ", self._read(message.body, 16))
        raise Hdf5Error("the object at {0} is not a group".format(header))

    def _header(self, path):
        parts = path.split("/")
        group = self._root
        for part in parts[:-1]:
            if part not in self._group(group):
                raise Hdf5Error("{0} has no {1}".format(path, part))
            group = self._subgroup(*self._group(group)[part])
        if parts[-1] not in self._group(group):
            raise Hdf5Error("{0} is not in the file".format(path))
        return self._group(group)[parts[-1]][0]

    def names(self):
        """The path of every dataset in the file."""
        found = []
        self._collect(self._root, "", found)
        return sorted(found)

    def _collect(self, group, prefix, found):
        for name, (header, scratch) in self._group(group).items():
            kinds = None if scratch is not None else {message.kind for message in self._messages(header)}
            if scratch is not None or _SYMBOL_TABLE in kinds:
                self._collect(self._subgroup(header, scratch), prefix + name + "/", found)
            elif _LAYOUT in kinds:
                found.append(prefix + name)

    def _storage(self, path):
        messages = {message.kind: message for message in self._messages(self._header(path))}
        if _DATASPACE not in messages or _LAYOUT not in messages or _DATATYPE not in messages:
            raise Hdf5Error("{0} is not a dataset".format(path))
        space = messages[_DATASPACE]
        dataspace = self._read(space.body, 8)
        if dataspace[0] not in (1, 2):
            raise Hdf5Error("{0} has a version {1} dataspace".format(path, dataspace[0]))
        if dataspace[1] != 1:
            raise Hdf5Error("{0} has {1} dimensions, not one".format(path, dataspace[1]))
        if struct.unpack("<I", self._read(messages[_DATATYPE].body + 4, 4))[0] != 1:
            raise Hdf5Error("{0} does not hold single bytes".format(path))
        length_at = space.body + (8 if dataspace[0] == 1 else 4)
        length = struct.unpack("<Q", self._read(length_at, 8))[0]
        limit = struct.unpack("<Q", self._read(length_at + 8, 8))[0] if dataspace[2] & 1 else None
        layout_at = messages[_LAYOUT].body
        layout = self._read(layout_at, 3)
        if layout[0] != 3:
            raise Hdf5Error("{0} has a version {1} data layout".format(path, layout[0]))
        if layout[1] == _CONTIGUOUS:
            address, size = struct.unpack("<QQ", self._read(layout_at + 2, 16))
            return Storage(_CONTIGUOUS, length, limit, address, size, None, length_at, layout_at)
        if layout[1] != _CHUNKED:
            raise Hdf5Error("{0} is stored as layout class {1}, not contiguous or chunked".format(path, layout[1]))
        if layout[2] != 2:
            raise Hdf5Error("{0} is chunked in {1} dimensions, not one".format(path, layout[2] - 1))
        if _FILTERS in messages:
            raise Hdf5Error("{0} is filtered".format(path))
        address, chunk, element = struct.unpack("<QII", self._read(layout_at + 3, 16))
        if element != 1:
            raise Hdf5Error("{0} has chunks of {1}-byte elements".format(path, element))
        return Storage(_CHUNKED, length, limit, address, None, chunk, length_at, layout_at)

    def _entries(self, node):
        head = self._read(node, _TREE_HEADER)
        if head[:4] != b"TREE" or head[4] != 1:
            raise Hdf5Error("no chunk tree node at {0}".format(node))
        used = struct.unpack_from("<H", head, 6)[0]
        body = self._read(node + _TREE_HEADER, used * (_TREE_KEY + 8) + _TREE_KEY)
        found = []
        for index in range(used):
            at = index * (_TREE_KEY + 8)
            size, mask, offset, inner = struct.unpack_from("<IIQQ", body, at)
            child = struct.unpack_from("<Q", body, at + _TREE_KEY)[0]
            if mask or inner:
                raise Hdf5Error("the chunk at {0} is filtered or offset in another dimension".format(offset))
            if head[5]:
                found.extend(self._entries(child))
            else:
                found.append(Entry(offset, child, size))
        return found

    def length(self, path):
        """How many bytes the dataset holds."""
        return self._storage(path).length

    def read(self, path):
        """The bytes of the dataset at ``path``."""
        storage = self._storage(path)
        if storage.kind == _CONTIGUOUS:
            return self._read(storage.address, storage.length)
        found = [] if storage.address == UNDEFINED else self._entries(storage.address)
        count = -(-storage.length // storage.chunk)
        buffer = bytearray(count * storage.chunk)
        with memoryview(buffer) as view:
            for entry in found:
                if entry.size != storage.chunk or entry.offset % storage.chunk or entry.offset // storage.chunk >= count:
                    raise Hdf5Error("the chunk at {0} does not fit {1}".format(entry.offset, path))
                self._file.seek(entry.address)
                if self._file.readinto(view[entry.offset:entry.offset + entry.size]) != entry.size:
                    raise Hdf5Error("the file ends inside the chunk at {0}".format(entry.address))
        del buffer[storage.length:]
        return bytes(buffer)

    def attribute(self, path, name):
        """The raw value of the attribute ``name`` of the dataset at ``path``."""
        start, size = self._attribute(path, name)
        return self._read(start, size)

    def _attribute(self, path, name):
        for message in self._messages(self._header(path)):
            if message.kind != _ATTRIBUTE:
                continue
            head = self._read(message.body, 8)
            if head[0] != 1:
                raise Hdf5Error("{0} has a version {1} attribute".format(path, head[0]))
            name_size, type_size, space_size = struct.unpack_from("<HHH", head, 2)
            if self._read(message.body + 8, name_size).split(b"\x00")[0].decode("utf-8") == name:
                start = message.body + 8 + _aligned(name_size) + _aligned(type_size) + _aligned(space_size)
                return start, message.body + message.size - start
        raise Hdf5Error("{0} has no attribute {1}".format(path, name))

    def set_attribute(self, path, name, value):
        """Overwrite the start of the value of the attribute ``name`` of the dataset at ``path``."""
        self._need_writable()
        start, size = self._attribute(path, name)
        if len(value) > size:
            raise Hdf5Error("the attribute {0} holds {1} bytes, not {2}".format(name, size, len(value)))
        self._write(start, value)

    def _need_writable(self):
        if not self.writable:
            raise Hdf5Error("the file is open for reading only")

    def write(self, path, data):
        """Replace the bytes of the dataset at ``path`` with ``data``."""
        self._need_writable()
        if not data:
            raise Hdf5Error("an empty dataset is not written")
        storage = self._storage(path)
        fixed = storage.limit is not None and storage.limit != UNDEFINED
        if fixed and storage.kind == _CHUNKED and storage.limit < len(data):
            raise Hdf5Error("{0} cannot grow past {1} bytes".format(path, storage.limit))
        if storage.kind == _CONTIGUOUS:
            self._write_contiguous(storage, data)
        else:
            self._write_chunked(storage, data)
        self._write(storage.length_at, struct.pack("<Q", len(data)))
        if fixed and storage.kind == _CONTIGUOUS:
            self._write(storage.length_at + 8, struct.pack("<Q", len(data)))

    def _write_contiguous(self, storage, data):
        address = storage.address if len(data) <= storage.size else self._allocate(len(data))
        self._write(address, data)
        self._write(storage.layout_at + 2, struct.pack("<QQ", address, len(data)))

    def _write_chunked(self, storage, data):
        chunk = storage.chunk
        held = {} if storage.address == UNDEFINED else {entry.offset: entry.address for entry in self._entries(storage.address)}
        entries = []
        for offset in range(0, len(data), chunk):
            address = held[offset] if offset in held else self._allocate(chunk)
            piece = data[offset:offset + chunk]
            self._write(address, piece + bytes(chunk - len(piece)))
            entries.append(Entry(offset, address, chunk))
        if sorted(held) != [entry.offset for entry in entries]:
            self._write(storage.layout_at + 3, struct.pack("<Q", self._write_tree(entries)))

    def _write_tree(self, entries):
        """Write the chunk B-tree of ``entries`` at the end of the file; the address of its root node."""
        plan = []
        count = len(entries)
        while True:
            groups = self._groups(count)
            plan.append(groups)
            if len(groups) == 1:
                break
            count = len(groups)
        position = self._allocate(sum(len(groups) for groups in plan) * _TREE_NODE)
        addresses = []
        for groups in plan:
            addresses.append([position + index * _TREE_NODE for index in range(len(groups))])
            position += len(groups) * _TREE_NODE
        keys = [self._key(entry.size, entry.offset, 0) for entry in entries]
        children = [entry.address for entry in entries]
        last = self._key(0, entries[-1].offset, 1)
        for level, groups in enumerate(plan):
            for index, (start, count) in enumerate(groups):
                after = keys[groups[index + 1][0]] if index + 1 < len(groups) else last
                left = addresses[level][index - 1] if index else UNDEFINED
                right = addresses[level][index + 1] if index + 1 < len(groups) else UNDEFINED
                self._write(addresses[level][index], self._node(
                    level, left, right, keys[start:start + count] + [after], children[start:start + count]))
            keys = [keys[start] for start, _count in groups]
            children = addresses[level]
        return addresses[-1][0]

    @staticmethod
    def _groups(count):
        """How ``count`` items divide among as few nodes as hold them, evenly: ``(start, size)`` for each."""
        nodes = -(-count // _TREE_ENTRIES)
        base, extra = divmod(count, nodes)
        start = 0
        groups = []
        for index in range(nodes):
            size = base + (1 if index < extra else 0)
            groups.append((start, size))
            start += size
        return groups

    @staticmethod
    def _key(size, offset, inner):
        return struct.pack("<IIQQ", size, 0, offset, inner)

    @staticmethod
    def _node(level, left, right, keys, children):
        body = b"TREE" + bytes((1, level)) + struct.pack("<HQQ", len(children), left, right)
        for key, child in zip(keys, children):
            body += key + struct.pack("<Q", child)
        body += keys[len(children)]
        return body + bytes(_TREE_NODE - len(body))
