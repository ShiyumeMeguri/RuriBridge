# -*- coding: utf-8 -*-
"""Which application this interpreter is.

Read off the interpreter, not configured: an application embeds its own Python
and its own API module, and by the time a plugin of ours loads, that module is
**already imported**. So the question is asked of ``sys.modules`` and of nothing
else.

Asking the import system whether it *could* find the module answers a different
question and answers it wrong: ``bpy`` is installable from PyPI, so a plain
interpreter with it on disk would claim to be Blender (measured -- this machine
is one). Being able to import an application's API is not being inside it.

Two present at once, or none, is an error rather than a priority order. A
priority order is how a plugin loads half-way into the wrong application and
reports it as a missing attribute three steps later.

A driver package under here is the only place allowed to import its own
application's API.
"""

from __future__ import annotations

import importlib
import sys

#: Driver folder -> the module that application has already imported by the time
#: our code runs. The folder name is also the name the application answers to
#: everywhere else in this package (a channel's speaker, a peer's row).
DRIVERS = {
    "Blender": "bpy",
    "Substance": "substance_painter",
    "Cascadeur": "csc",
}


def inside():
    """Every application whose API is live in this interpreter."""
    return sorted(name for name, module in DRIVERS.items() if module in sys.modules)


def detect():
    """The name of the application this interpreter is."""
    present = inside()
    if len(present) == 1:
        return present[0]
    if not present:
        raise RuntimeError(
            "RuriBridge is running outside every application it drives (expected "
            "one of {0}); it bridges applications and has nothing to do outside "
            "one".format(", ".join(
                "{0} ({1})".format(name, module)
                for name, module in sorted(DRIVERS.items()))))
    raise RuntimeError(
        "{0} are live in one interpreter; the application cannot be read off it "
        "any more".format(" and ".join(present)))


def driver(name=None):
    """Import the driver for an application.

    On demand, because importing a driver imports that application's API:
    importing all of them is how a package ends up requiring every host it can
    name, and importing one we are not inside would simply fail.
    """
    return importlib.import_module("." + (name or detect()), __name__)
