# -*- coding: utf-8 -*-
"""Publishing to and subscribing from one arena channel.

A channel is single-writer by construction -- Blender owns ``to_painter``,
Painter owns ``to_blender`` -- which is why nothing here needs a lock beyond the
slot's own seqlock. Publishing is append-only: every publish gets a fresh,
immutable generation directory, so two publishes in a row cannot clobber each
other and a consumer that polls slowly still sees both.

Generations are kept until the consumer acknowledges them. If a consumer stops
consuming, the backlog is capped and the oldest are dropped -- loudly, with the
count recorded in the slot, because a bridge that silently discards a mesh is
worse than one that says it did.
"""

from __future__ import annotations

import contextlib
import shutil

from . import record as record_module
from .arena import MAX_OUTSTANDING_GENERATIONS
from .log import logger

LOG = logger("channel")


class Generation:
    """One immutable published unit: a directory plus the record inside it."""

    __slots__ = ("channel", "number", "directory", "record")

    def __init__(self, channel, number, directory, record):
        self.channel = channel
        self.number = number
        self.directory = directory
        self.record = record

    @property
    def kind(self):
        return self.record.get("kind")

    def path(self, name):
        return self.directory / name

    def __repr__(self):
        return "Generation({0!r}, {1}, kind={2!r})".format(
            self.channel, self.number, self.kind)


class Staging:
    """A generation directory being filled. Nothing sees it until it publishes."""

    __slots__ = ("publisher", "number", "directory", "generation")

    def __init__(self, publisher, number, directory):
        self.publisher = publisher
        self.number = number
        self.directory = directory
        self.generation = None

    def path(self, name):
        return self.directory / name

    def publish(self, record):
        self.generation = self.publisher.commit(self.number, record)
        return self.generation


class Publisher:
    """The writing end of one channel.

    ``listeners`` are the roster indices of everyone who hears this channel. A
    payload is owed until every one of them has taken it, so retirement is a
    minimum over exactly that set -- not over the roster, which would let an
    application that never listens hold payloads forever.
    """

    def __init__(self, arena, channel, listeners=()):
        self.arena = arena
        self.channel = channel
        self.listeners = tuple(listeners)

    def next_generation_number(self):
        state = self.arena.read_slot(self.channel)
        existing = self.arena.existing_generations(self.channel)
        highest = max(existing) if existing else 0
        return max(state.generation, highest) + 1

    @contextlib.contextmanager
    def staging(self):
        """Fill a generation, then publish it. Anything else discards it.

        Staging through a context manager rather than a bare call is what keeps a
        producer that fails halfway -- an export Painter refuses, a mesh that
        evaluates to nothing -- from leaving a directory behind every time it is
        retried. There is no cleanup to remember, so there is none to forget.
        """
        number = self.next_generation_number()
        directory = self.arena.generation_directory(self.channel, number)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        staging = Staging(self, number, directory)
        try:
            yield staging
        except BaseException:
            self.arena.discard_generation(self.channel, number)
            raise
        if staging.generation is None:
            LOG.warning("%s generation %d was staged but never published; discarding",
                        self.channel, number)
            self.arena.discard_generation(self.channel, number)

    def commit(self, number, record):
        """Write the record, then flip the slot. Order matters: the record is
        the last byte written before anything becomes visible."""
        directory = self.arena.generation_directory(self.channel, number)
        record_module.write(directory, record)
        payload_bytes = sum(
            entry.stat().st_size for entry in directory.rglob("*") if entry.is_file())
        dropped = self._retire(number)
        self.arena.publish(self.channel, number, payload_bytes, dropped)
        LOG.info("published %s generation %d (%s, %d bytes)",
                 self.channel, number, record.get("kind"), payload_bytes)
        return Generation(self.channel, number, directory, record)

    def publish_record(self, record):
        """Publish a generation whose whole payload is the record itself."""
        with self.staging() as staging:
            return staging.publish(record)

    def _retire(self, newest):
        """Drop what nobody can still be using, and cap an unread backlog.

        The generation the consumer acknowledged *last* is kept alongside the
        newest one, because acknowledging means "I have taken this", not "I have
        finished with it": a Blender image loaded out of a payload keeps pointing
        at it until a newer payload replaces it, and retiring it underneath would
        leave that image dangling. Two survivors is the whole cost.
        """
        state = self.arena.read_slot(self.channel)
        acknowledged = state.taken_by(self.listeners)
        dropped = state.dropped_generations
        outstanding = []
        for number in self.arena.existing_generations(self.channel):
            if number >= newest or number == acknowledged:
                continue
            if number <= acknowledged:
                self.arena.discard_generation(self.channel, number)
            else:
                outstanding.append(number)
        overflow = len(outstanding) - (MAX_OUTSTANDING_GENERATIONS - 1)
        if overflow > 0:
            for number in outstanding[:overflow]:
                LOG.warning(
                    "channel %s backlog exceeded %d unacknowledged generations; "
                    "dropping generation %d before the consumer read it",
                    self.channel, MAX_OUTSTANDING_GENERATIONS, number)
                self.arena.discard_generation(self.channel, number)
                dropped += 1
        return dropped


class Subscriber:
    """The reading end of one channel.

    Holds the last generation it acknowledged so it can hand back every
    generation published since, in order, rather than only the newest.
    """

    def __init__(self, arena, channel, listener):
        self.arena = arena
        self.channel = channel
        #: This consumer's own roster index. Its acknowledgement lives in its own
        #: word, so two applications reading one channel cannot retire each
        #: other's payloads -- which is what happened the first time a third
        #: application attached.
        self.listener = listener
        state = arena.read_slot(channel)
        self._acknowledged = (state.acknowledged_by[listener]
                              if listener < len(state.acknowledged_by) else 0)

    @property
    def acknowledged_generation(self):
        return self._acknowledged

    def skip_to_latest(self):
        """Treat everything already published as seen. Used when attaching."""
        state = self.arena.read_slot(self.channel)
        self._acknowledged = state.generation
        self.arena.acknowledge(self.channel, self._acknowledged, self.listener)
        return self._acknowledged

    def latest(self, kind=None):
        """The newest published generation, acknowledged or not.

        ``pending`` deliberately starts from where this subscriber attached, so a
        session that opens hours later does not replay history. Asking for the
        latest is the other, explicit question -- "what does the other side
        currently have" -- and it is the one a pull answers.
        """
        state = self.arena.read_slot(self.channel)
        for number in reversed(self.arena.existing_generations(self.channel)):
            if number > state.generation:
                continue
            directory = self.arena.generation_directory(self.channel, number)
            try:
                payload = record_module.read(directory)
            except record_module.RecordError as error:
                LOG.warning("skipping generation %d on %s: %s", number, self.channel, error)
                continue
            if kind is None or payload.get("kind") == kind:
                return Generation(self.channel, number, directory, payload)
        return None

    def catch_up(self, kind):
        """Acknowledge history, but stop short of the newest generation of a kind.

        Used when a host attaches and finds work already waiting. Skipping
        everything would throw away exactly the thing the other side published a
        moment ago -- the whole point of publishing before the other application
        was even started -- while replaying everything would re-apply meshes that
        have since been superseded. What matters is the newest generation of the
        kind being waited for, and whatever came after it.
        """
        newest = self.latest(kind)
        if newest is None:
            return self.skip_to_latest()
        self._acknowledged = max(self._acknowledged, newest.number - 1)
        self.arena.acknowledge(self.channel, self._acknowledged, self.listener)
        return self._acknowledged

    def pending(self):
        """Every unacknowledged generation still on disk, oldest first."""
        state = self.arena.read_slot(self.channel)
        if state.generation <= self._acknowledged:
            return []
        ready = []
        for number in self.arena.existing_generations(self.channel):
            if number <= self._acknowledged or number > state.generation:
                continue
            directory = self.arena.generation_directory(self.channel, number)
            try:
                payload = record_module.read(directory)
            except record_module.RecordError as error:
                LOG.warning("skipping generation %d on %s: %s", number, self.channel, error)
                self._acknowledged = max(self._acknowledged, number)
                continue
            ready.append(Generation(self.channel, number, directory, payload))
        return ready

    def acknowledge(self, generation):
        """Consumers acknowledge one at a time, after handling each.

        There is no drain-everything helper on purpose: a consumer that has to
        hand control back to its host mid-batch -- because what it just did left
        the host busy -- must be able to stop after acknowledging exactly what it
        finished, and a generator that acknowledges after the yield cannot
        express that.
        """
        self._acknowledged = max(self._acknowledged, generation.number
                                 if isinstance(generation, Generation) else generation)
        self.arena.acknowledge(self.channel, self._acknowledged, self.listener)


class StateWriter:
    """The writing end of an inline state channel."""

    def __init__(self, arena, channel):
        self.arena = arena
        self.channel = channel

    def write(self, record):
        self.arena.write_state(self.channel, record)
        return record


class StateReader:
    """The reading end of an inline state channel.

    Latest-wins by construction: there is one slot, so a value that was replaced
    before this side looked was never news. What it tracks is only the generation
    counter, to answer "is this different from what I last saw".
    """

    def __init__(self, arena, channel):
        self.arena = arena
        self.channel = channel
        self._seen = arena.read_state(channel)[1]

    def skip_to_latest(self):
        self._seen = self.arena.read_state(self.channel)[1]
        return self._seen

    def take(self):
        """The record if it changed since the last take, otherwise None."""
        payload, generation = self.arena.read_state(self.channel)
        if payload is None or generation == self._seen:
            return None
        self._seen = generation
        return payload
