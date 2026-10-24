# -*- coding: utf-8 -*-
"""Polygon fills: the clicks and rectangles that chose which polygons a fill covers (see ``selection`` for how they choose).

A hit chooses polygons through a camera or in the UV view. After the surface changed the same polygons have to be chosen,
and a choice through a camera depends on the mesh and one in the UV view on the layout, so a hit is replaced by clicks in
the UV view -- one on each polygon it chose, or one on each element or UV chunk when it chose by those -- placed in the
layout the project has now. They choose the same polygons whatever the mesh looks like.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the hits made through a camera are replaced by clicks in the layout.
* ``follow_surface`` -- the UV layout moved, the mesh stayed: the hits made in the UV view are replaced by clicks at where
  the layout now puts the polygons they chose (a hit through a camera is right as it is).

A replacement is only made when the rules, run on the clicks, choose exactly the polygons the hit chose: a UV chunk is
clicked as one only where the layout still holds it as one chunk, and a polygon that lies over another in the layout (a
mirrored island) is only clicked where the one under it is chosen too. Otherwise the hit is left as it is and counted.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, placement, selection

HOLDER = "DataActionDecal"
PUNCTUAL = "DataDecalHitPunctual"
RECT = "DataDecalHitRect"
VIEW_2D = 0
VIEW_3D = 1
FIELDS = (("color", 0x03), ("duplicateCount", 0x0B), ("duplicateMatrix", 0x0E), ("granularity", 0x09), ("paintThrough", 0x09),
          ("position", 0x02), ("projViewInvMatrix", 0x0E), ("uid", 0x0C), ("viewType", 0x09))
DEFINITION = stream.Definition(PUNCTUAL, FIELDS)
_WINDOW = 0.02
#: What ``convert`` gives for a hit the pass is not about: it stays and is not counted.
UNTOUCHED = object()


class _Layout:
    """A surface with its UV layout as hits are clicked in it: what a set of clicks chooses, by the rules of ``selection``."""

    def __init__(self, surface, mask, elements, chunks):
        self.surface = surface
        self.mask = mask
        self.elements = elements
        self.chunks = chunks
        self.index = geometry.UvIndex(surface, mask)

    def chosen(self, positions, granularities):
        """The triangles clicks at ``positions`` (n, 2) of the given ``granularities`` choose: a boolean per triangle."""
        point, triangle = self.index.holding(positions)
        out = np.zeros(len(self.surface.triangles), dtype=bool)
        for granularity in np.unique(granularities):
            seeds = np.zeros(len(self.surface.triangles), dtype=bool)
            seeds[triangle[granularities[point] == granularity]] = True
            out |= selection.widened(seeds, granularity, lambda kind: self.elements if kind == selection.ELEMENT else self.chunks)
        return out & self.mask


def _click(context, template, position, granularity):
    """A click in the UV view like ``template`` (its colour), at ``position``, of ``granularity``."""
    values = {"color": template.number("color"), "duplicateCount": 0, "duplicateMatrix": (0.0,) * 16, "granularity": granularity,
              "paintThrough": 0, "position": tuple(float(value) for value in position), "projViewInvMatrix": (0.0,) * 16,
              "uid": context.uids.take(), "viewType": VIEW_2D}
    return stream.Object(DEFINITION, [stream.encode(code, values[name]) for name, code in FIELDS])


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
        triangle, _weights, _offset = context.old_layout_exact.query(np.array([hit.number("position")], dtype=np.float64))
        if triangle[0] >= 0:
            seeds[triangle[0]] = True
    return seeds


def _compacts(granularity, layout, wanted):
    """The ways to click ``wanted`` in fewer clicks than polygons, most compact first, as ``(granularity, triangles)``: one
    polygon of the material in each UV chunk or element it is made of. A hit that chose by one of those can only be said by it."""
    kinds = {selection.CHUNK: (selection.CHUNK,), selection.ELEMENT: (selection.ELEMENT,)}.get(granularity, (selection.CHUNK, selection.ELEMENT))
    found = []
    for kind in kinds:
        labels = layout.chunks if kind == selection.CHUNK else layout.elements
        own = np.nonzero(layout.mask & wanted)[0]
        _labels, first = np.unique(labels[own], return_index=True)
        found.append((kind, own[first]))
    return found


def _replacement(context, template, layout, chosen, compacts):
    """Clicks in ``layout`` that choose exactly the triangles ``chosen`` (a boolean per triangle of its surface): the compact
    ones first (``compacts`` as ``_compacts`` gives them), then one on every polygon; ``None`` where none of them does."""
    for granularity, triangles in list(compacts) + [(0, np.nonzero(chosen)[0])]:
        positions = selection.centroid_uv(layout.surface, triangles)
        if (layout.chosen(positions, np.full(len(triangles), granularity)) == chosen).all():
            return [_click(context, template, position, granularity) for position in positions]
    return None


def _replace(material, convert):
    """Rebuild the hits of every polygon fill of the material: ``convert(hit)`` gives the hits that take a hit's place,
    ``None`` for a hit that has to stay one (counted as ``left``) or ``UNTOUCHED`` for one the pass is not about. A fill left
    with no hit is taken out of the stack that holds it."""
    counts = Counter()
    emptied = []
    for action in [found for found in stream.walk(material) if found.type == HOLDER]:
        rebuilt = []
        for hit in action["hits"]:
            made = convert(hit)
            if made is UNTOUCHED:
                rebuilt.append(hit)
                continue
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


def _follow_camera(hit, context, wanted):
    """Move the camera of a hit the way the patch of the polygons it chose moved, so it still sees them: all that can be done
    for a hit that cannot be said by clicks in the layout. Counts nothing as ``None`` -- the hit stays a hit of its own."""
    triangles = np.nonzero(wanted)[0]
    if not len(triangles):
        return None
    weights = np.repeat(selection.CENTRE, len(triangles), axis=0)
    uv, place, new_triangle, new_weights = placement.across(context, triangles, weights)
    placed = new_triangle >= 0
    if not placed.any():
        return None
    motion = placement.rigid(context, uv[placed], place[placed], context.new.point(new_triangle[placed], new_weights[placed]))
    hit.set_number("projViewInvMatrix", geometry.moved_inverse(hit.number("projViewInvMatrix"), motion))
    return None


def follow_uv(material, context):
    """Replace the hits made through a camera by clicks in the UV layout on the polygons they chose; those that clicks cannot say
    (polygons that lie over others in the layout) keep their camera, moved with the patch of polygons they chose."""
    layout = _Layout(context.old, context.old_mask, context.old_elements, context.old_chunks)

    def convert(hit):
        if hit.number("viewType") != VIEW_3D:
            return UNTOUCHED
        granularity = hit.number("granularity")
        seeds = _seeds_through_camera(hit, context)
        wanted = selection.widened(seeds, granularity, lambda kind: context.old_elements if kind == selection.ELEMENT else context.old_chunks) & context.old_mask
        made = _replacement(context, hit, layout, wanted, _compacts(granularity, layout, wanted))
        return made if made is not None else _follow_camera(hit, context, wanted & context.old_mask)

    return dict(_replace(material, convert))


def follow_surface(material, context):
    """Replace the hits made in the UV view by clicks in the new layout on the polygons they chose."""
    layout = _Layout(context.new, context.new_mask, context.new_elements, context.new_chunks)

    def convert(hit):
        if hit.number("viewType") != VIEW_2D:
            return UNTOUCHED
        if hit.number("duplicateCount") != 0:
            return None
        granularity = hit.number("granularity")
        seeds = _seeds_in_layout(hit, context)
        wanted_old = selection.widened(seeds, granularity, lambda kind: context.old_elements if kind == selection.ELEMENT else context.old_chunks) & context.old_mask
        old_triangles = np.nonzero(wanted_old)[0]
        carried = context.counterpart.carry(old_triangles, np.repeat(selection.CENTRE, len(old_triangles), axis=0))[0]
        if (carried < 0).any():
            return None
        wanted = np.zeros(len(context.new.triangles), dtype=bool)
        wanted[carried] = True
        return _replacement(context, hit, layout, wanted, _compacts(granularity, layout, wanted))

    return dict(_replace(material, convert))
