# -*- coding: utf-8 -*-
"""What crosses, and which application may speak or hear it.

A topic is declared the way a capability-gated feature is declared everywhere
else: a key, and the capability a host must answer for. The difference is that a
stream has two ends, so it names two -- speaking and hearing are not the same
question, and most streams have one author and several readers. The model is
stated by the application it is authored in and taken by the two that cannot
change it; the performance is stated and taken by the two that have an animation
surface; the baked channels are stated by the one that bakes them.

Nothing here is a table by application name. A host answers for capabilities, a
topic asks for capabilities, and the join decides what that application is
offered. A fourth application is a row in the roster, and its channels appear.

The channel a topic travels on is ``"<key>@<speaker>"``. A stream is
single-writer by construction, so the writer's name is half of the address and
the arena needs no lock beyond its own slot. **The channel set is never written
down** -- it is every topic crossed with every peer that may speak it, computed
from the roster, and it is part of the session's identity: a session whose
channels differ from these was built by a different set of applications and is
rebuilt rather than attached to, because a session is transport and carries
nothing worth migrating.
"""

from __future__ import annotations

from . import host as host_port
from . import peers as peers_module

#: Every publication is kept until the consumer acknowledges it: a model nobody
#: has taken yet is still owed.
QUEUED = "queued"
#: One slot, latest wins. A value replaced before the other side looked was never
#: news, and building a generation directory for it would be the wrong shape.
STATE = "state"

#: A channel name has to fit the arena's slot field, and the speaker is half of
#: it. Checked when the name is built rather than when the arena refuses it.
MAX_CHANNEL_NAME = 31


class Topic:
    """One named stream of statements."""

    __slots__ = ("key", "kind", "speaks", "hears", "description")

    def __init__(self, key, kind, speaks=None, hears=None, description=""):
        self.key = key
        self.kind = kind
        #: The capability needed to PRODUCE this, or None for one anybody can.
        self.speaks = speaks
        #: The capability needed to CONSUME it.
        self.hears = hears
        self.description = description

    def spoken_by(self, capabilities):
        return self.speaks is None or self.speaks in capabilities

    def heard_by(self, capabilities):
        return self.hears is None or self.hears in capabilities

    def channel(self, speaker):
        name = "{0}@{1}".format(self.key, speaker)
        if len(name) > MAX_CHANNEL_NAME:
            raise ValueError(
                "channel name {0!r} is {1} characters and the arena slot holds "
                "{2}".format(name, len(name), MAX_CHANNEL_NAME))
        return name

    def __repr__(self):
        return "<Topic {0} ({1})>".format(self.key, self.kind)


#: A model: geometry, its material rows, and the transform each piece sits at.
#: ONE application states it -- the one the model is authored in -- and the
#: others take it. A texturing tool and an animation tool both need it and
#: neither can change it, so a model coming back from either could only be a
#: worse copy of the one that went out.
MESH = Topic(
    "mesh", QUEUED, speaks=host_port.SCENE_GRAPH, hears=host_port.MODEL_INTAKE,
    description="A model and its materials, as the speaker has it now")

#: Baked channels coming out of a texturing tool. Only a host whose materials are
#: a graph has anywhere to put them.
TEXTURES = Topic(
    "tex", QUEUED, speaks=host_port.TEXTURE_SETS, hears=host_port.NODE_MATERIALS,
    description="Baked texture channels, per material")

#: The rig and what it is doing. Both ends need an animation surface, and a
#: texturing tool has none -- it says so rather than being special-cased.
#:
#: The skeleton travels WITH the performance because a performance without the
#: bones it is keyed to is not one, and the model channel next door carries
#: geometry and materials for a texturing tool -- no skeleton in it at all.
#:
#: Coming back, what is USED is the channels against the names: the authoring
#: side already has the rig, and rebuilding one from the payload would leave a
#: second, differently-oriented copy of those bones beside the first, with every
#: round trip rotating the axes a little further.
ANIMATION = Topic(
    "anim", QUEUED, speaks=host_port.ANIMATION, hears=host_port.ANIMATION,
    description="A rig and what it is doing, keyed by the names both sides share")

#: The knobs of the shading stack, both ways. Latest-wins: a slider drag is one
#: value that keeps changing, not a queue of values owed.
SHADING = Topic(
    "shade", STATE, speaks=host_port.SHADING_PARAMETERS,
    hears=host_port.SHADING_PARAMETERS,
    description="The shading stack's parameter values")

#: What each side currently is: which document is open, what it is bound to,
#: where the application lives. Every host can answer, so no capability is asked,
#: and it is how the other side stops guessing.
PRESENCE = Topic(
    "here", STATE,
    description="Which document this application has open, and what it is bound to")

#: "Please do a thing" -- send me a model, bake me your channels. One topic
#: rather than one per errand: what is being asked lives in the record, so a
#: third application can ask for a model without a line of new plumbing.
REQUEST = Topic(
    "ask", QUEUED,
    description="A request aimed at another application")

TOPICS = (MESH, TEXTURES, ANIMATION, SHADING, PRESENCE, REQUEST)


def by_key(key):
    for one in TOPICS:
        if one.key == key:
            return one
    raise KeyError(key)


def spoken_by(capabilities):
    return tuple(one for one in TOPICS if one.spoken_by(capabilities))


def heard_by(capabilities):
    return tuple(one for one in TOPICS if one.heard_by(capabilities))


def channels(capabilities_by_peer=None):
    """Every channel a session needs, in a stable order.

    Stable because it is the session's identity: the same roster always computes
    the same list, and a control block whose list differs was built by different
    software.
    """
    table = (peers_module.capabilities_by_peer() if capabilities_by_peer is None
             else capabilities_by_peer)
    names = []
    for one in TOPICS:
        for peer in sorted(table):
            if one.spoken_by(table[peer]):
                names.append(one.channel(peer))
    return tuple(names)


def subscriptions(host_name, capabilities):
    """(topic, speaker-channel) for everything this host should be listening to.

    Everything every OTHER application may speak and this one can hear. A host
    never subscribes to its own channel: it would hear its own voice, and the
    echo problem is solved by not creating it rather than by filtering it.
    """
    table = peers_module.capabilities_by_peer()
    wanted = []
    for one in TOPICS:
        if not one.heard_by(capabilities):
            continue
        for peer in sorted(table):
            if peer != host_name and one.spoken_by(table[peer]):
                wanted.append((one, one.channel(peer)))
    return tuple(wanted)


def publications(host_name, capabilities):
    """(topic, own-channel) for everything this host may publish."""
    return tuple((one, one.channel(host_name))
                 for one in TOPICS if one.spoken_by(capabilities))
