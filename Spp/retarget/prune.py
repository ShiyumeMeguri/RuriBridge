# -*- coding: utf-8 -*-
"""Cleaning a material that was split off another: paint that cannot reach the polygons it was split off with.

The new material is a copy of the one it came from -- the same layers, strokes and hits -- laid over the polygons that were
given to it. A stroke or hit that never touched one of those polygons paints nothing on them, and replayed on them alone it
could land on a polygon that used to be behind the one it was made on, so it is taken out; what touches them, even by a
single texel, stays. Layers left with nothing to paint are taken out with them. Fills are not judged: a fill covers the
layout, and what a mask or a geometry mask does to it is not for this pass to work out.

What a stroke touches is judged by where it fell, in the UV layout (the layout is the same in the copy) and in space, and how
far its brush reaches from there: the reaches are measured ones (a test model, the default brush) taken generously, so that a
stroke is only taken out when it is far from every polygon of the new material both ways, and kept whenever its reach is not
one this can judge.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import brushes, geometry, selection

#: What a stamp touches, as a radius in multiples of the brush size, in the texture, across the surface in object units, or
#: across the surface per unit of the screen at the stamp's depth. Measured on the default brush as 0.45, 0.4 and 0.78 (a
#: diameter of 0.87, 0.78 and 1.55 per unit of size, to where the paint is a texel above nothing); MARGIN covers softer and
#: larger alphas and the jitters.
MARGIN = 2.5
RADIUS = {brushes.TEXTURE: 0.45, brushes.OBJECT: 0.4, brushes.VIEW: 0.78}
#: How far, in the UV layout, a reach may go before the pass gives up judging it and keeps the stroke.
MAX_REACH = 0.25
#: The reaches, as shares of the scene's diagonal, the spatial indexes of the polygons to keep are built for.
SPACE_REACHES = (0.01, 0.04, 0.16)
HIT_HOLDER = "DataActionDecal"
PAINTING = brushes.HOLDERS + (HIT_HOLDER,)
VIEW_3D = 1


def _size(brush):
    """The brush's size grown by the margin and the jitters, in its own units; ``None`` for a brush this cannot judge."""
    if brush is None or "brushSizeSpace" not in brush:
        return None
    jitter = 1.0 + brush.number("brushSizeJitter") + (brush["stampsEmitter"].number("positionJitter") if brush["stampsEmitter"] is not None else 0.0)
    return brush.number("brushSize") * MARGIN * jitter


def _scale(surface, triangles):
    """How many units of the surface lie across a unit of the layout at each of ``triangles`` (infinite where it has no layout)."""
    frames, usable = surface.frames(triangles)
    area = np.abs(np.linalg.det(np.einsum("nji,njk->nik", frames, frames)))
    return np.where(usable, np.sqrt(np.sqrt(area)), np.inf)


def _reach(space, size, scale, along):
    """How far, across the surface, a stamp of the brush reaches from its place: ``scale`` is how many units of the surface
    lie across a unit of the layout there and ``along`` how many across a unit of the screen."""
    if space == brushes.TEXTURE:
        return RADIUS[space] * size * scale
    return RADIUS[space] * size * (along if space == brushes.VIEW else 1.0)


class _Space:
    """The polygons the paint is to keep, findable by a place in space within a reach: one index for each of a few reaches."""

    def __init__(self, surface, moved):
        self.surface = surface
        self.moved = moved
        self.diagonal = float(np.linalg.norm(surface.positions.max(axis=0) - surface.positions.min(axis=0)))
        self.indexes = {}

    def near(self, points, radius):
        """Whether any of ``points`` is within its ``radius`` of a polygon of the index; ``None`` where a radius is beyond what
        the index is built for."""
        largest = float(np.max(radius))
        reach = next((share * self.diagonal for share in SPACE_REACHES if share * self.diagonal >= largest), None)
        if reach is None:
            return None
        if reach not in self.indexes:
            self.indexes[reach] = geometry.SpaceIndex(self.surface, self.moved, reach)
        triangle, weights = self.indexes[reach].query(points)
        found = triangle >= 0
        return bool((found & (np.linalg.norm(self.surface.point(np.maximum(triangle, 0), weights) - points, axis=1) <= radius)).any())


def _stroke_touches(stroke, brush, context, moved_index, moved_space):
    """Whether the stroke may touch a polygon of the moved ones, by the layout or in space: ``True`` where that cannot be told."""
    points = np.array(stroke["points"].rows(), dtype=np.float64)[:, :2]
    size = _size(brush)
    if size is None:
        return True
    if not len(points):
        return False
    space = brush.number("brushSizeSpace")
    if stroke.number("viewType") != VIEW_3D:
        triangle, weights, _offset = context.old_layout.query(points)
        found = triangle >= 0
        scale = _scale(context.old, triangle[found])
        radius = max(RADIUS.values()) * size
        return _may_touch(context, moved_index, moved_space, context.old.uv(triangle[found], weights[found]), context.old.point(triangle[found], weights[found]),
                          np.full(len(scale), radius) / scale, np.full(len(scale), radius))
    camera = geometry.Camera(stroke.number("viewMatrix"), stroke.number("projectionMatrix"))
    focal = abs(stroke.number("projectionMatrix")[0])
    mirror = np.array(stroke.number("duplicateMatrix")).reshape(4, 4).T if stroke.number("duplicateCount") == 1 else None
    through = "paintThrough" in brush and brush.number("paintThrough") != 0
    screen = points * 2.0 - 1.0
    for each in [camera] + ([camera.following(mirror)] if mirror is not None else []):
        index = geometry.ScreenIndex(context.old, each, screen.min(axis=0) - 0.02, screen.max(axis=0) + 0.02)
        if through and (index.query(screen, moved)[0] >= 0).any():
            return True
        triangle, weights, _depth = index.query(screen, context.old_mask)
        found = triangle >= 0
        if not found.any():
            continue
        place = context.old.point(triangle[found], weights[found])
        scale = _scale(context.old, triangle[found])
        reach = _reach(space, size, scale, each.clip(place)[:, 3] / max(focal, 1e-9))
        if _may_touch(context, moved_index, moved_space, context.old.uv(triangle[found], weights[found]), place, reach / scale, reach):
            return True
    return False


def _may_touch(context, moved_index, moved_space, uv, place, reach_uv, reach_space):
    """Whether the places (``uv`` in the layout, ``place`` in space) with their reaches may touch the moved polygons; true where
    a reach is beyond what can be judged."""
    if (reach_uv > MAX_REACH).any():
        return True
    triangle, _weights, offset = moved_index.query(uv)
    if ((triangle >= 0) & (np.linalg.norm(offset, axis=1) <= reach_uv)).any():
        return True
    near = moved_space.near(place, reach_space)
    return near is None or near


def _hit_touches(hit, context, moved):
    """Whether the hit chooses a polygon of ``moved``, by the rules of ``selection``."""
    seeds = np.zeros(len(context.old.triangles), dtype=bool)
    rectangle = hit.type == "DataDecalHitRect"
    if hit.number("viewType") == VIEW_3D:
        camera = geometry.Camera.from_inverse(hit.number("projViewInvMatrix"))
        for each in [camera] + ([camera.following(np.array(hit.number("duplicateMatrix")).reshape(4, 4).T)] if hit.number("duplicateCount") == 1 else []):
            if rectangle:
                seeds |= selection.on_screen(context.old, each, np.minimum(hit.number("position0"), hit.number("position1")), np.maximum(hit.number("position0"), hit.number("position1")))
            else:
                point = np.array([hit.number("position")], dtype=np.float64) * 2.0 - 1.0
                triangle, _weights, _depth = geometry.ScreenIndex(context.old, each, point[0] - 0.02, point[0] + 0.02).query(point, context.old_mask)
                if triangle[0] >= 0:
                    seeds[triangle[0]] = True
    elif rectangle:
        own = np.nonzero(context.old_mask)[0]
        low, high = np.minimum(hit.number("position0"), hit.number("position1")), np.maximum(hit.number("position0"), hit.number("position1"))
        seeds[own[selection.overlapping(context.old.uvs[context.old.triangles[own]], low, high)]] = True
    else:
        triangle, _weights, _offset = context.old_layout.query(np.array([hit.number("position")], dtype=np.float64))
        if triangle[0] >= 0:
            seeds[triangle[0]] = True
    chosen = selection.widened(seeds, hit.number("granularity"), lambda kind: context.old_elements if kind == selection.ELEMENT else context.old_chunks)
    return bool((chosen & moved).any())


def _leaves(layer):
    """The actions of the layer's stacks that are not groups."""
    found = []
    pending = [action for field in ("actions", "maskActions") if field in layer and layer[field] is not None for action in layer[field]["items"] if action is not None]
    while pending:
        action = pending.pop()
        if action.type == "DataActionGroup":
            pending.extend(child for child in action["subStack"]["items"] if child is not None)
        else:
            found.append(action)
    return found


def run(material, context, moved):
    """Take out of ``material`` -- the copy -- the strokes and hits that cannot touch the polygons ``moved`` (a boolean per
    triangle of the old surface), then the actions and the layers that were paint and have none left. Returns what was done."""
    counts = Counter()
    painted = [layer for layer in stream.walk(material) if layer.type == "DataLayerColor" and any(leaf.type in PAINTING for leaf in _leaves(layer))]
    moved_index = geometry.UvIndex(context.old, moved, reach=MAX_REACH)
    moved_space = _Space(context.old, moved)
    for action in [found for found in stream.walk(material) if found.type in brushes.HOLDERS]:
        kept = [stroke for stroke in action["strokes"] if _stroke_touches(stroke, action["brush"], context, moved_index, moved_space)]
        counts["strokes kept"] += len(kept)
        counts["strokes taken out"] += len(action["strokes"]) - len(kept)
        action["strokes"] = kept
    for action in [found for found in stream.walk(material) if found.type == HIT_HOLDER]:
        kept = [hit for hit in action["hits"] if _hit_touches(hit, context, moved)]
        counts["hits kept"] += len(kept)
        counts["hits taken out"] += len(action["hits"]) - len(kept)
        action["hits"] = kept
    emptied = [found for found in stream.walk(material) if (found.type in brushes.HOLDERS and not found["strokes"] and not found["strokes3D"])
               or (found.type == HIT_HOLDER and not found["hits"])]
    _remove(material, PAINTING, emptied)
    counts["actions taken out"] = len(emptied)
    gone = [layer for layer in painted if not _leaves(layer)]
    _remove(material, ("DataLayerColor",), gone)
    counts["layers taken out"] = len(gone)
    return dict(counts)


def _remove(material, types, gone):
    """Take the objects ``gone`` out of the lists of the material that hold objects of ``types``."""
    for items in stream.holders(material, types):
        items[:] = [entry for entry in items if not any(entry is found for found in gone)]
