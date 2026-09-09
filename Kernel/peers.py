# -*- coding: utf-8 -*-
"""The applications this bridge reaches, as facts rather than behaviour.

A capability set answers "what can this application do", and every driver used to
state its own. That works right up until you need the answer for an application
that **is not running** -- which is the whole of bridging. You cannot ask
Cascadeur what it can do while it is closed, and the session's channel list has
to be known before anybody attaches.

So the roster lives here, host-free, and each driver reads its own row instead of
repeating it: one statement, three readers. This is not a table of behaviour by
host name -- nothing branches on ``peer.name``, everything still asks a
capability. It is the difference between deciding what to DO and knowing who
exists.

Two other facts have the same shape, and no amount of asking the running
application will reveal them:

``resident``  whether our code runs continuously inside it. Blender and Painter
              load a plugin and keep it, so they watch the session themselves.
              Cascadeur runs a command and exits, so it has to be SUMMONED to
              come and look -- and that difference is the only reason the
              transport needs a doorbell at all.
``attach``    how our code gets in: this package IS the Blender add-on, Painter
              takes a directory junction to it, and Cascadeur takes a copy of one
              counterpart script into its own command folder, because it loads
              commands by module path out of its installation.

**One payload format, everywhere: glTF binary.** Not a preference and not a
negotiation -- a second format would mean the same model exists twice, in two
encodings, with two sets of rounding. GLB is chosen because its binary chunk on
disk already IS the memory layout of the vertex arrays, which is what makes a
publish a write into pages the other side has mapped rather than a serialise.
An application whose build cannot read it does not get a quieter fallback; it
fails at attach, by name.
"""

from __future__ import annotations

from . import host as host_port

#: Our code is already inside: the package IS that application's add-on.
ATTACH_NATIVE = "native"
#: A directory junction from its plugin folder to this package.
ATTACH_JUNCTION = "junction"
#: A copy of the counterpart script into its own command folder.
ATTACH_COPY = "copy"


class Peer:
    """One application, seen from outside it."""

    __slots__ = ("name", "label", "capabilities", "resident", "attach",
                 "plugin_subpath", "executable_name", "summon_template")

    def __init__(self, name, label, capabilities, resident, attach,
                 plugin_subpath=(), executable_name="", summon_template=()):
        self.name = name
        self.label = label
        self.capabilities = frozenset(capabilities)
        self.resident = resident
        self.attach = attach
        #: Where our code goes inside its user or installation folder.
        self.plugin_subpath = tuple(plugin_subpath)
        #: What its executable is called. WHERE it is gets asked of the operating
        #: system's own registration, never guessed by walking Program Files --
        #: an install outside the default location is normal and a walk misses it.
        self.executable_name = executable_name
        #: How to make a non-resident application come and look. ``{command}`` is
        #: the counterpart script's module path.
        self.summon_template = tuple(summon_template)

    def summons(self, command):
        if self.resident:
            raise ValueError(
                "{0} keeps a plugin running and watches the session itself; "
                "summoning it per message would be describing a limit it does "
                "not have".format(self.name))
        return [part.format(command=command) for part in self.summon_template]

    def __repr__(self):
        return "<Peer {0}>".format(self.name)


BLENDER = Peer(
    "Blender", "Blender",
    capabilities=(host_port.SCENE_GRAPH, host_port.ANIMATION,
                  host_port.MODEL_INTAKE, host_port.NODE_MATERIALS,
                  host_port.SHADING_PARAMETERS),
    resident=True,
    attach=ATTACH_NATIVE,
    executable_name="blender.exe")

SUBSTANCE = Peer(
    "Substance", "Substance 3D Painter",
    capabilities=(host_port.TEXTURE_SETS, host_port.MODEL_INTAKE,
                  host_port.SHADING_PARAMETERS),
    resident=True,
    attach=ATTACH_JUNCTION,
    plugin_subpath=("python", "plugins"),
    executable_name="Adobe Substance 3D Painter.exe")

#: An animation tool. It TAKES the rig -- it has to have one to animate -- and
#: what it has to say back is the performance and nothing else. The model has one
#: author, and it is not this application: it cannot change a mesh, so a mesh
#: leaving here could only be a worse copy of the one that arrived.
CASCADEUR = Peer(
    "Cascadeur", "Cascadeur",
    capabilities=(host_port.ANIMATION, host_port.MODEL_INTAKE),
    resident=False,
    attach=ATTACH_COPY,
    plugin_subpath=("resources", "scripts", "python", "commands", "ruri"),
    executable_name="cascadeur.exe",
    summon_template=("--run-script", "commands.ruri.{command}"))

PEERS = (BLENDER, SUBSTANCE, CASCADEUR)


def by_name(name):
    for one in PEERS:
        if one.name == name:
            return one
    raise KeyError(
        "no application named {0!r} in the roster; the roster and the driver "
        "folders under Host/ are the same set of names".format(name))


def capabilities_by_peer():
    """What each application can answer for.

    The input the channel set is computed from -- and the reason it can be
    computed while nothing else is running.
    """
    return {one.name: one.capabilities for one in PEERS}


def others(name):
    """Everyone except the application asking."""
    return tuple(one for one in PEERS if one.name != name)
