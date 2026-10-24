# -*- coding: utf-8 -*-
"""Polygon fills: the clicks and rectangles that chose which polygons a fill covers (see ``selection`` for how they choose).

A hit chooses polygons through a camera or in the UV view. After the surface changed the same polygons have to be chosen,
and a choice through a camera depends on the mesh and one in the UV view on the layout, so a hit is replaced by clicks in
the UV view -- one on each polygon it chose, or one on each element or UV chunk when it chose by those -- placed in the
layout the project has now. They choose the same polygons whatever the mesh looks like.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the hits made through a camera are replaced by clicks in the layout.
* ``follow_surface`` -- the UV layout moved, the mesh stayed: the hits made in the UV view are replaced by clicks at where
  the layout now puts the polygons they chose (a hit through a camera is right as it is). A UV chunk is chosen by one click
  only where the new layout still holds it as one chunk; otherwise its polygons are clicked one by one.

A hit whose polygons lie over others in the layout cannot be told apart by a click in the layout; it is left as it is.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, selection

HOLDER = "DataActionDecal"
PUNCTUAL = "DataDecalHitPunctual"
RECT = "DataDecalHitRect"
VIEW_2D = 0
VIEW_3D = 1
FIELDS = (("color", 0x03), ("duplicateCount", 0x0B), ("duplicateMatrix", 0x0E), ("granularity", 0x09), ("paintThrough", 0x09),
          ("position", 0x02), ("projViewInvMatrix", 0x0E), ("uid", 0x0C), ("viewType", 0x09))
DEFINITION = stream.Definition(PUNCTUAL, FIELDS)
_WINDOW = 0.02


def _click(context, template, position, granularity):
    """A click in the UV view like ``template`` (its colour), at ``position``, of ``granularity``."""
    values = {"color": template.number("color"), "duplicateCount": 0, "duplicateMatrix": (0.0,) * 16, "granularity": granularity,
              "paintThrough": 0, "position": tuple(float(value) for value in position), "projViewInvMatrix": (0.0,) * 16,
              "uid": context.uids.take(), "viewType": VIEW_2D}
    return stream.Object(DEFINITION, [stream.encode(code, values[name]) for name, code in FIELDS])


def _actions(material):
    return [action for action in stream.walk(material) if action.type == HOLDER]


def _mirror(hit):
    return np.array(hit.number("duplicateMatrix")).reshape(4, 4).T if hit.number("duplicateCount") == 1 else None


def _seeds_through_camera(hit, context):
    """The triangles of the old surface the hit seeds: a boolean per triangle."""
    camera = geometry.Camera.from_inverse(hit.number("projViewInvMatrix"))
    cameras = [camera] + ([camera.following(_mirror(hit))] if _mirror(hit) is not None else [])
    seeds = np.zeros(len(context.old.triangles), dtype=bool)
    for each in cameras:
        if hit.type == RECT:
            low, high = np.minimum(hit.number("position0"), hit.number("position1")), np.maximum(hit.number("position0"), hit.number("position1"))
            seeds |= selection.on_screen(context.old, each, low, high)
        else:
            point = np.array([hit.number("position")], dtype=np.float64) * 2.0 - 1.0
            index = geometry.ScreenIndex(context.old, each, point[0] - _WINDOW, point[0] + _WINDOW)
            triangle, _weights, _depth = index.query(point, context.old_mask)
            if triangle[0] >= 0:
                seeds[triangle[0]] = True
    return seeds


def _seeds_in_layout(hit, context):
    """The triangles of the old surface the hit seeds, for a hit made in the UV view: a boolean per triangle."""
    seeds = np.zeros(len(context.old.triangles), dtype=bool)
    if hit.type == RECT:
        low, high = np.minimum(hit.number("position0"), hit.number("position1")), np.maximum(hit.number("position0"), hit.number("position1"))
        own = np.nonzero(context.old_mask)[0]
        seeds[own[selection.overlapping(context.old.uvs[context.old.triangles[own]], low, high)]] = True
    else:
        triangle, _weights, _offset = context.old_layout.query(np.array([hit.number("position")], dtype=np.float64))
        if triangle[0] >= 0:
            seeds[triangle[0]] = True
    return seeds


def _choice(seeds, granularity, context):
    """What the hit chooses, as what has to be clicked: ``(0, triangles)``, each polygon of the material chosen, or
    ``(granularity, triangles)`` for an element or UV chunk, one polygon of the material in each chosen."""
    if granularity not in (selection.ELEMENT, selection.CHUNK):
        return 0, np.nonzero(seeds & context.old_mask)[0]
    labels = context.old_elements if granularity == selection.ELEMENT else context.old_chunks
    own = np.nonzero(context.old_mask & np.isin(labels, np.unique(labels[seeds])))[0]
    _labels, first = np.unique(labels[own], return_index=True)
    return granularity, own[first]


def _clicks_in_old_layout(context, template, choice):
    granularity, triangles = choice
    if len(triangles) and (context.old_layout.holding(selection.centroid_uv(context.old, triangles)) > 1).any():
        return None
    return [_click(context, template, position, granularity) for position in selection.centroid_uv(context.old, triangles)]


def _carried(context, triangles):
    """The triangles of the new surface that are the old ``triangles``; ``-1`` where there is none."""
    return context.counterpart.carry(triangles, np.repeat(selection.CENTRE, len(triangles), axis=0))[0]


def _chunks_stay(context, triangles):
    """Whether the UV chunk of each of the old ``triangles`` is, polygon for polygon, one UV chunk of the new layout."""
    for triangle in triangles:
        carried = _carried(context, np.nonzero(context.old_mask & (context.old_chunks == context.old_chunks[triangle]))[0])
        if (carried < 0).any():
            return False
        labels = np.unique(context.new_chunks[carried])
        if len(labels) != 1 or int((context.new_mask & (context.new_chunks == labels[0])).sum()) != len(carried):
            return False
    return True


def _clicks_in_new_layout(context, template, seeds, granularity):
    """Clicks that choose in the new layout what ``seeds`` chose in the old; ``None`` where they cannot."""
    kind, triangles = _choice(seeds, granularity, context)
    if kind == selection.CHUNK and not _chunks_stay(context, triangles):
        kind, triangles = 0, np.nonzero(context.old_mask & np.isin(context.old_chunks, context.old_chunks[triangles]))[0]
    new_triangles = _carried(context, triangles)
    if (new_triangles < 0).any() or (context.new_layout.holding(selection.centroid_uv(context.new, new_triangles)) > 1).any():
        return None
    return [_click(context, template, position, kind) for position in selection.centroid_uv(context.new, new_triangles)]


def _replace(material, convert):
    """Rebuild the hits of every polygon fill of the material: ``convert(hit)`` gives the hits that take a hit's place,
    or ``None`` to leave it. Counts what was done; a fill left with no hit is taken out of the stack that holds it."""
    counts = Counter()
    emptied = []
    for action in _actions(material):
        rebuilt = []
        for hit in action["hits"]:
            made = convert(hit)
            counts["hits"] += 1
            if made is None:
                counts["left"] += 1
                rebuilt.append(hit)
            else:
                counts["clicks"] += len(made)
                rebuilt.extend(made)
        action["hits"] = rebuilt
        if not rebuilt:
            emptied.append(action)
    for items in stream.holders(material, (HOLDER,)):
        items[:] = [entry for entry in items if not any(entry is gone for gone in emptied)]
    counts["fills without a hit left"] = len(emptied)
    return counts


def follow_uv(material, context):
    """Replace the hits made through a camera by clicks in the UV layout on the polygons they chose."""
    def convert(hit):
        if hit.number("viewType") != VIEW_3D:
            return None
        seeds = _seeds_through_camera(hit, context)
        return _clicks_in_old_layout(context, hit, _choice(seeds, hit.number("granularity"), context))

    return dict(_replace(material, convert))


def follow_surface(material, context):
    """Replace the hits made in the UV view by clicks in the new layout on the polygons they chose."""
    def convert(hit):
        if hit.number("viewType") != VIEW_2D or hit.number("duplicateCount") != 0:
            return None
        return _clicks_in_new_layout(context, hit, _seeds_in_layout(hit, context), hit.number("granularity"))

    return dict(_replace(material, convert))
