# -*- coding: utf-8 -*-
"""The port: everything the bridge asks of the application it is running in.

An abstract base class, so a driver that forgets a member cannot be constructed.
A missing answer is a startup error, not a ``None`` three stages later.

Nothing above this line branches on which application it is. What differs between
Blender, Painter and Cascadeur is stated as **capabilities** -- and a capability
is named after the question it answers, never after the application that happens
to answer it, so a fourth application declares its way in rather than being
recognised.

A capability is declared here when something ASKS it. Every one below is asked by
a topic (:mod:`Kernel.topic`): that is the only reason each exists.
"""

from __future__ import annotations

import abc

#: The application's document is a tree of objects with their own transforms, so
#: it can STATE a model. Painter's document is one mesh it was handed; it has
#: nothing to state.
SCENE_GRAPH = "scene_graph"
#: There is an animation surface -- actions, curves, a playhead -- so a
#: performance means something.
ANIMATION = "animation"
#: A model published elsewhere can be brought into this application's document.
#: Not the same question as SCENE_GRAPH: a texturing tool has no scene graph and
#: its whole document is built from a model, while an animation tool has a scene
#: graph and animates the rig that is already in it -- the model does not arrive,
#: it was already there.
MODEL_INTAKE = "model_intake"
#: Surfaces are grouped into texture sets that can be baked out. This is what
#: makes a texture publication possible at all.
TEXTURE_SETS = "texture_sets"
#: Materials are a node graph, so baked channels have somewhere to land.
NODE_MATERIALS = "node_materials"
#: There is a shading stack whose parameter values can be read and written, so
#: the two ends can hold the same look.
SHADING_PARAMETERS = "shading_parameters"

#: Levels the port's log takes. Named rather than passed through, because the
#: three applications spell them differently and the kernel should not learn any
#: of the three spellings.
DEBUG = "debug"
INFO = "info"
WARNING = "warning"
ERROR = "error"

#: The bound driver. Held at module scope on purpose: a development reload that
#: cleared it would leave every module reporting "no host" from the next call on.
HOLDS_PROCESS_STATE = True

_BOUND = None


class Host(abc.ABC):
    """One application, as the bridge needs to use it."""

    @property
    @abc.abstractmethod
    def name(self):
        """The string this application answers to everywhere: the driver folder,
        its row in the roster, and the speaker half of a channel name. One
        string, no mapping table."""

    @property
    @abc.abstractmethod
    def capabilities(self):
        """What this application can answer for. Read from the roster rather than
        written here, so the answer is the same whether or not it is running."""

    @abc.abstractmethod
    def log(self, level, message):
        """Say something where this application's user will find it."""

    @abc.abstractmethod
    def schedule(self, seconds, function):
        """Call ``function`` on the main thread after a delay, once.

        The pump is this and nothing else. Blender has app timers, Painter has
        QTimer, and an application that is summoned per message has no loop at
        all -- so it answers by refusing, and the kernel never builds a pump it
        could not run."""

    @abc.abstractmethod
    def redraw(self):
        """The panel's description changed and the screen has not caught up."""

    @abc.abstractmethod
    def receive(self, topic, generation):
        """Materialise one incoming publication into this application.

        Only reached for a topic this host HEARS, so an implementation never
        needs a guard: the capability answered that question before the
        subscription existed."""

    @abc.abstractmethod
    def collect(self, topic):
        """This application's current answer for a state topic, or None.

        Only reached for a topic this host SPEAKS."""


def bind(driver):
    """Install the one driver for this process."""
    global _BOUND
    _BOUND = driver
    return driver


def current():
    if _BOUND is None:
        raise RuntimeError(
            "no application is bound: a driver under Host/ binds itself when it "
            "is imported, and nothing above that line runs before it does")
    return _BOUND


def bound():
    """The driver, or None -- for the few places that must ask without failing."""
    return _BOUND
