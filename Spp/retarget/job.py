# -*- coding: utf-8 -*-
"""A retarget of a project: its document repaired for the surface it holds now, given the one it was painted on."""

from __future__ import annotations

from .. import project, scene, stream
from . import engine, geometry


def run_surfaces(target, old, new, names, anchor, source=None, source_set=None):
    """Repair the materials called ``names`` of the open, writable project ``target``.

    ``old`` is the ``geometry.Surface`` the paint was made on, ``new`` the one it is to be painted on, and ``anchor``
    (``engine.UV`` or ``engine.SURFACE``) says what stayed. ``source`` is the new mesh read with the UV set
    ``source_set`` that holds the layout the paint was made in, which paint laid through the layout needs. Returns
    what each material's passes did.
    """
    document = stream.read(target.read(project.DOCUMENT))
    found = {material.text("sceneMaterialName"): material for material in document.root["materials"]}
    missing = [name for name in names if name not in found]
    if missing:
        raise ValueError("the project has no material called {0}".format(", ".join(missing)))
    report = {name: engine.run(found[name], old, new, anchor, source=source, source_set=source_set) for name in names}
    target.write(project.DOCUMENT, stream.write(document))
    return report


def run(target, old_scene, names, anchor, uv_set=0, source_set=None):
    """``run_surfaces`` for the surfaces held in scene files: ``old_scene`` is the bytes of the ``scene.assbin`` the
    paint was made on and the project's own scene is the surface it is on now."""
    old = geometry.Surface.of(scene.read(old_scene), uv_set)
    current = scene.read(target.read(project.SCENE))
    new = geometry.Surface.of(current, uv_set)
    source = None if source_set is None else geometry.Surface.of(current, source_set)
    return run_surfaces(target, old, new, names, anchor, source, source_set)
