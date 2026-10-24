# -*- coding: utf-8 -*-
"""Paint that moves with faces into another material: the layers copied for them, made to land where the faces lie.

Faces painted into one material can be painted into another, and a copy of the layers that painted them goes with them. A
copy holds the strokes and polygon fills as they were made, in the layout and on the polygons of the material they were made
in; in the material it lives in now, the faces lie in another layout, and the polygons have other numbers. A stroke drawn in
the UV view or a polygon fill clicked in it has to be carried to where the faces lie now, and what lay on other faces of the
material it came from has nothing to land on, so it is left out. The passes are those of an anchor on the mesh, run on the copy
alone and on the faces that moved only (``Context`` with ``moved``).
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, hits, selection, strokes
from .context import Context

LAYER = "DataLayer"
#: How far apart, as a share of the model's diagonal, two corners may lie for a polygon of one surface to be found again in the other.
FIND_AGAIN = 1e-5


def moved(old, new, source, target):
    """The polygons of the faces carried from ``source`` into ``target``, as a boolean per triangle of each surface: ``(old, new)``.
    A face is carried when a polygon of ``source`` in ``old`` is found again in ``new`` painting into ``target``."""
    old_mask = old.mask(source)
    triangles = np.nonzero(old_mask)[0]
    diagonal = float(np.linalg.norm(new.positions.max(axis=0) - new.positions.min(axis=0)))
    found, _weights = geometry.Correspondence(old, new, FIND_AGAIN * diagonal, same_material=False).carry(
        triangles, np.repeat(selection.CENTRE, len(triangles), axis=0))
    into = np.zeros(len(triangles), dtype=bool)
    present = found >= 0
    into[present] = new.materials[found[present]] == new.names.index(target)
    old_moved = np.zeros(len(old.triangles), dtype=bool)
    new_moved = np.zeros(len(new.triangles), dtype=bool)
    old_moved[triangles[into]] = True
    new_moved[found[into]] = True
    return old_moved, new_moved


def _layer(material, uid):
    found = [item for item in stream.walk(material) if item.type.startswith(LAYER) and "uid" in item and item.number("uid") == uid]
    if len(found) != 1:
        raise ValueError("{0} layers of {1} have the uid {2}, not one".format(len(found), material.text("sceneMaterialName"), uid))
    return found[0]


def run(material, roots, old, new, source, uids):
    """Repair the layers ``roots`` (uids) of ``material`` -- a ``DataMaterial`` -- which are copies of layers of the material
    called ``source``, for the faces ``old`` paints into ``source`` and ``new`` into ``material``. Returns what each pass did."""
    target = material.text("sceneMaterialName")
    old_moved, new_moved = moved(old, new, source, target)
    if not old_moved.any():
        raise ValueError("no face painted into {0} by the old surface is painted into {1} by the new one".format(source, target))
    context = Context(old, new, source, uids=uids, moved=(old_moved, new_moved))
    report = {}
    for uid in roots:
        layer = _layer(material, uid)
        for step in (strokes.follow_surface, hits.follow_surface):
            name = "{0}.{1}".format(step.__module__.rsplit(".", 1)[-1], step.__name__)
            report.setdefault(name, Counter()).update(step(layer, context))
    return {name: dict(counts) for name, counts in report.items()}
