# -*- coding: utf-8 -*-
"""One live attachment, addressed by topic instead of by channel.

Nothing above this line names a channel. A leg asks for the topic it wants to
publish and gets its own endpoint; it asks what it should be listening to and
gets one endpoint per application that may speak it. Which channels those are was
computed from the roster (:mod:`Kernel.topic`), so an application appearing or
disappearing changes what a leg is handed without changing a line of it.

Attaching is also where the session's identity is checked. The channel list IS
the format: a control block whose channels differ from the ones this build
computes was built by different software, and it is rebuilt rather than
migrated -- a session is transport and holds nothing worth keeping.

Presence is per application, not per session: each side stamps a heartbeat on the
channels it owns, so "is Painter attached" is a read of Painter's own slots. That
is deliberately not a process lookup -- an application can be running with the
plugin switched off, and telling somebody to go and switch it on is a different
answer from starting a second copy.
"""

from __future__ import annotations

from . import arena as arena_module
from . import channel as channel_module
from . import peers as peers_module
from . import topic as topic_module
from .log import logger

LOG = logger("session")


class Endpoint:
    """One application's end of one topic."""

    __slots__ = ("topic", "peer", "channel", "reader")

    def __init__(self, topic, peer, channel, reader):
        self.topic = topic
        self.peer = peer
        self.channel = channel
        #: A ``Subscriber`` for a queued topic, a ``StateReader`` for a state one.
        self.reader = reader

    def __repr__(self):
        return "<Endpoint {0} from {1}>".format(self.topic.key, self.peer)


class Session:
    """The arena, plus this host's endpoints on it."""

    def __init__(self, arena, name, capabilities):
        self.arena = arena
        self.name = name
        self.capabilities = frozenset(capabilities)
        self._publishers = {}
        self._writers = {}
        self._sources = {}
        for one, own in topic_module.publications(name, self.capabilities):
            if one.kind == topic_module.QUEUED:
                self._publishers[one.key] = channel_module.Publisher(arena, own)
            else:
                self._writers[one.key] = channel_module.StateWriter(arena, own)
        for one, remote in topic_module.subscriptions(name, self.capabilities):
            speaker = remote.split("@", 1)[1]
            reader = (channel_module.Subscriber(arena, remote)
                      if one.kind == topic_module.QUEUED
                      else channel_module.StateReader(arena, remote))
            self._sources.setdefault(one.key, []).append(
                Endpoint(one, speaker, remote, reader))

    # -- attaching ---------------------------------------------------------
    @classmethod
    def open(cls, name, capabilities, session=arena_module.DEFAULT_SESSION, root=None):
        arena = arena_module.Arena.open_session(
            topic_module.channels(), session=session, root=root)
        made = cls(arena, name, capabilities)
        LOG.info("attached as %s to session %s (%d channel(s), %d publication(s), "
                 "%d subscription(s))", name, session, len(arena.channels),
                 len(made._publishers) + len(made._writers),
                 sum(len(entries) for entries in made._sources.values()))
        return made

    def close(self):
        self.arena.close()

    # -- speaking ----------------------------------------------------------
    def publisher(self, topic):
        """This host's writing end of a queued topic."""
        try:
            return self._publishers[topic.key]
        except KeyError:
            raise KeyError(
                "{0} does not speak {1!r}: it answers for {2} and the topic asks "
                "for {3}".format(self.name, topic.key, sorted(self.capabilities),
                                 topic.speaks))

    def writer(self, topic):
        """This host's writing end of a state topic."""
        try:
            return self._writers[topic.key]
        except KeyError:
            raise KeyError(
                "{0} does not speak {1!r}".format(self.name, topic.key))

    def speaks(self, topic):
        return topic.key in self._publishers or topic.key in self._writers

    # -- hearing -----------------------------------------------------------
    def sources(self, topic):
        """Every application this host should be listening to for that topic."""
        return tuple(self._sources.get(topic.key, ()))

    def hears(self, topic):
        return bool(self._sources.get(topic.key))

    def incoming(self):
        """Every unacknowledged publication waiting, oldest first across topics.

        Ordered by generation rather than by topic, because two topics published
        in an order that mattered -- a model and then the request to bake it --
        have to arrive in that order too.
        """
        waiting = []
        for entries in self._sources.values():
            for endpoint in entries:
                if endpoint.topic.kind != topic_module.QUEUED:
                    continue
                for generation in endpoint.reader.pending():
                    waiting.append((generation.number, endpoint, generation))
        waiting.sort(key=lambda row: row[0])
        return tuple((endpoint, generation) for _number, endpoint, generation in waiting)

    def changed_state(self):
        """Every state topic whose value moved since this host last looked."""
        moved = []
        for entries in self._sources.values():
            for endpoint in entries:
                if endpoint.topic.kind != topic_module.STATE:
                    continue
                payload = endpoint.reader.take()
                if payload is not None:
                    moved.append((endpoint, payload))
        return tuple(moved)

    # -- who is there ------------------------------------------------------
    def touch(self):
        """Stamp the heartbeat on every channel this host owns."""
        for one, own in topic_module.publications(self.name, self.capabilities):
            self.arena.touch(own)

    def present(self, peer_name):
        """Whether that application's half of the bridge is attached right now.

        True when ANY channel it owns has a live stamp: a peer publishes its
        presence on ``here@<name>`` whatever else it does, so one live slot is
        the whole answer and a peer that speaks nothing else is still visible.
        """
        peer = peers_module.by_name(peer_name)
        for one, own in topic_module.publications(peer.name, peer.capabilities):
            if self.arena.read_slot(own).writer_is_live:
                return True
        return False

    def attendance(self):
        """Every application in the roster and whether it is here."""
        return tuple((peer.name, peer.name == self.name or self.present(peer.name))
                     for peer in peers_module.PEERS)
