# -*- coding: utf-8 -*-
"""A retarget of a project: its document repaired for the surface it holds now, given the one it was painted on."""

from __future__ import annotations

import numpy as np

from .. import document as document_module
from .. import materials, project, scene, stream
from . import engine, geometry, prune
from .context import Context
from .selection import CENTRE


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
    uids = document_module.Uids(document.root)
    report = {name: engine.run(found[name], old, new, anchor, source=source, source_set=source_set, uids=uids) for name in names}
    target.write(project.DOCUMENT, stream.write(document))
    return report


def split_surfaces(target, old, new, source, name):
    """Add to the open, writable project ``target`` a material called ``name``: a copy of ``source`` cleaned for the polygons
    it has in ``new`` -- the surface the project is to have, whose polygons of ``name`` were those of ``source`` in ``old``,
    with the same UV layout. Returns what the cleaning took out."""
    streams = materials.Streams(target)
    uid = materials.clone(streams, source, name)
    copy = next(material for material in streams.materials if material.number(document_module.UID) == uid)
    context = Context(old, new, source, uids=document_module.Uids(streams.document.root))
    triangles = np.nonzero(context.old_mask)[0]
    counterparts, _weights = context.counterpart_any_material.carry(triangles, np.repeat(CENTRE, len(triangles), axis=0))
    moved = np.zeros(len(old.triangles), dtype=bool)
    found = counterparts >= 0
    moved[triangles[found]] = new.materials[counterparts[found]] == new.names.index(name)
    report = prune.run(copy, context, moved)
    streams.save(target)
    return report


def split(target, old_scene, new_scene, source, name, uv_set=0):
    """``split_surfaces`` for the surfaces held in scene files: ``old_scene`` the bytes of the project's own scene, ``new_scene``
    those of the one the split material's polygons are in."""
    return split_surfaces(target, geometry.Surface.of(scene.read(old_scene), uv_set), geometry.Surface.of(scene.read(new_scene), uv_set), source, name)


def run(target, old_scene, names, anchor, uv_set=0, source_set=None):
    """``run_surfaces`` for the surfaces held in scene files: ``old_scene`` is the bytes of the ``scene.assbin`` the
    paint was made on and the project's own scene is the surface it is on now."""
    old = geometry.Surface.of(scene.read(old_scene), uv_set)
    current = scene.read(target.read(project.SCENE))
    new = geometry.Surface.of(current, uv_set)
    source = None if source_set is None else geometry.Surface.of(current, source_set)
    return run_surfaces(target, old, new, names, anchor, source, source_set)
