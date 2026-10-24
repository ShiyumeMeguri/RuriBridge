# -*- coding: utf-8 -*-
"""A retarget of a project: its document repaired for the surface it holds now, given the one it was painted on."""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import document as document_module
from .. import fbx, materials, project, scene, stream
from . import canvas, canvas_geometry, engine, faces, geometry, prune
from .context import Context
from .selection import CENTRE


def run_surfaces(target, old, new, names, source=None, source_set=None, passes=None):
    """Repair the materials called ``names`` of the open, writable project ``target`` for a layout that changed under a mesh that stayed.

    ``old`` is the ``geometry.Surface`` the paint was made on, ``new`` the one it is to be painted on. ``source`` is the new mesh read
    with the UV set ``source_set`` that holds the layout the paint was made in, which paint laid through the layout needs. ``passes``
    names the passes to run (see ``engine.run``). Returns what each material's passes did.
    """
    document = stream.read(target.read(project.DOCUMENT))
    found = {material.text("sceneMaterialName"): material for material in document.root["materials"]}
    missing = [name for name in names if name not in found]
    if missing:
        raise ValueError("the project has no material called {0}".format(", ".join(missing)))
    uids = document_module.Uids(document.root)
    report = {name: engine.run(found[name], old, new, source=source, source_set=source_set, uids=uids, passes=passes)
              for name in names}
    document_module.require_unique(document.root, "the repaired document")
    target.write(project.DOCUMENT, stream.write(document))
    return report


def split_surfaces(target, old, new, source, name):
    """Add to the open, writable project ``target`` a material called ``name``: a copy of ``source`` cleaned for the polygons
    it has in ``new`` -- the surface the project is to have, whose polygons of ``name`` were those of ``source`` in ``old``,
    with the same UV layout. Returns what the cleaning took out."""
    streams = materials.Streams(target)
    uid, _mapping = materials.clone(streams, source, name)
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


def carry_surfaces(target, old, new, events, before=None):
    """Repair the paint of layers copied into materials of the open, writable project ``target``, for faces carried into those
    from other materials: ``old`` is the surface the paint was made on and ``new`` the one the project holds, and each of
    ``events`` -- ``{"source", "target", "roots", "canvases"}`` -- names the material the faces came from, the one the copies are in,
    the uids of the copies' root layers and the copies that read a canvas (see ``faces.read_canvases``), which are read from
    ``before``, the document of the project as it stood before the faces moved. Returns what the passes did, by material the
    copies are in."""
    document = stream.read(target.read(project.DOCUMENT))
    found = {material.text("sceneMaterialName"): material for material in document.root["materials"]}
    missing = [event["target"] for event in events if event["target"] not in found]
    if missing:
        raise ValueError("the project has no material called {0}".format(", ".join(missing)))
    uids = document_module.Uids(document.root)
    report = {}
    for event in events:
        if event.get("canvases"):
            if before is None:
                raise ValueError("copies that read a canvas need the document of the project as it stood before the faces moved")
            origins = {material.text("sceneMaterialName"): material for material in before.root["materials"]}
            merged = report.setdefault(event["target"], {})
            merged["faces.read_canvases"] = faces.read_canvases(found[event["target"]], event["canvases"], origins[event["source"]], uids)
        done = faces.run(found[event["target"]], event["roots"], old, new, event["source"], uids) if event["roots"] else {}
        merged = report.setdefault(event["target"], {})
        for name, counts in done.items():
            total = Counter(merged.get(name, {}))
            total.update(counts)
            merged[name] = dict(total)
    document_module.require_unique(document.root, "the repaired document")
    target.write(project.DOCUMENT, stream.write(document))
    return report


def carry(target, old_scene, events, uv_set=0, old_document=None):
    """``carry_surfaces`` for the surfaces held in scene files: ``old_scene`` is the bytes of the ``scene.assbin`` the paint was made on
    and the project's own scene is the surface it is on now; ``old_document`` the bytes of the project's document as it stood
    before the faces moved, for the copies that read a canvas."""
    old = geometry.Surface.of(scene.read(old_scene), uv_set)
    new = geometry.Surface.of(scene.read(target.read(project.SCENE)), uv_set)
    return carry_surfaces(target, old, new, events, None if old_document is None else stream.read(old_document))


def lay_canvases(target, names, surface, surface_out, follow=()):
    """Give each material called ``names`` of the open, writable project ``target`` a canvas (see ``canvas``), unless its surface did not
    change, and write the file of the surface the project is to have to ``surface_out``: the surface in the FBX ``surface``, and after it
    the surface of every canvas the project has, the ones it had and the ones this made, taken from the scene the project holds now.
    ``follow`` are uids of layers of the materials that something outside the document keeps. Returns what each material got:
    ``{"canvases": {material: canvas}, "kept": [material that holds nothing but what a canvas already holds, or nothing], "unchanged":
    [material whose surface is the very one it was painted on], "attached": [canvas the project had], "followed": {uid: the uid it
    became}}``."""
    streams = materials.Streams(target)
    held = sorted(canvas.canvases(streams))
    made = canvas.templates() if names else None
    parts, uv_names = fbx.read(surface)
    found = scene.read(target.read(project.SCENE))
    same = [name for name in names if canvas_geometry.unchanged(parts, found, name)]
    laid, followed = {}, {}
    for name in names:
        if name not in same:
            laid[name], mapping = canvas.lay(streams, name, made)
            followed.update({str(uid): mapping[uid] for uid in follow if uid in mapping})
    if any(laid.values()):
        streams.save(target)
    placements = {name: name for name in held}
    placements.update({name: given for name, given in laid.items() if given})
    fbx.write(surface_out, *canvas_geometry.compose(parts, uv_names, found, placements))
    return {"canvases": {name: given for name, given in laid.items() if given}, "kept": sorted(name for name, given in laid.items() if not given),
            "unchanged": sorted(same), "attached": held, "followed": followed}


def run(target, old_scene, names, uv_set=0, source_set=None, passes=None):
    """``run_surfaces`` for the surfaces held in scene files: ``old_scene`` is the bytes of the ``scene.assbin`` the
    paint was made on and the project's own scene is the surface it is on now; ``names`` is ``None`` for every material both
    surfaces have polygons of."""
    old = geometry.Surface.of(scene.read(old_scene), uv_set)
    current = scene.read(target.read(project.SCENE))
    new = geometry.Surface.of(current, uv_set)
    source = None if source_set is None else geometry.Surface.of(current, source_set)
    if names is None:
        names = every_material(target, old, new)
    return run_surfaces(target, old, new, names, source, source_set, passes)


def every_material(target, old, new):
    """The materials of the project's document that both surfaces have polygons of."""
    document = stream.read(target.read(project.DOCUMENT))
    return [name for name in (material.text("sceneMaterialName") for material in document.root["materials"])
            if name in old.names and name in new.names and old.mask(name).any() and new.mask(name).any()]
