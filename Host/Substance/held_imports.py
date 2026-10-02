# -*- coding: utf-8 -*-
"""The files imported project resources are read from, kept until the project closes.

Painter reads a project resource from the file it was imported from, and not at import:
the first time something needs it -- a layer computed, a map exported, the project
saved -- which can be long after, and a resource whose file is gone by then comes out
flat without a word. What the bridge imports arrives as transport, retired as soon as
two newer deliveries exist, so it is held here first, as a second name for the same
bytes, until the project closes: by then the project has embedded it or let it go.

One folder per Painter process, with a lock that process keeps open. A folder whose lock
refuses to go belongs to a Painter still running; any other is swept the next time a
Painter needs the folder. Beside the sessions, so a held name is a link on the same
volume and not a copy.
"""

from __future__ import annotations

import os
import shutil

from ...Kernel import arena as arena_module
from ...Kernel.log import logger

LOG = logger("painter.held")

FOLDER_NAME = "painter_imports"
_LOCK_NAME = "held.lock"
_state = {"folder": None, "lock": None}


def _sweep(base):
    """Take away what Painters no longer running left behind."""
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            (entry / _LOCK_NAME).unlink(missing_ok=True)
        except PermissionError:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as error:
            LOG.debug("%s is still in use; leaving it for next time: %s", entry, error)


def _folder():
    if _state["folder"] is None:
        base = arena_module.default_root().parent / FOLDER_NAME
        base.mkdir(parents=True, exist_ok=True)
        _sweep(base)
        folder = base / str(os.getpid())
        folder.mkdir(exist_ok=True)
        _state["lock"] = open(folder / _LOCK_NAME, "w")
        _state["folder"] = folder
    return _state["folder"]


def hold(path, digest):
    """Where to import a delivered file from: the same bytes, under a name nothing
    retires before the project closes."""
    held = _folder() / (digest + os.path.splitext(path)[1])
    if not held.exists():
        os.link(path, held)
    return str(held)


def release():
    """The project closed: none of its resources is read from these files any more."""
    folder = _state["folder"]
    if folder is None:
        return
    for entry in folder.iterdir():
        if entry.name == _LOCK_NAME:
            continue
        try:
            entry.unlink()
        except OSError as error:
            LOG.warning("could not let go of %s: %s", entry.name, error)
