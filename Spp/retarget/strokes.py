# -*- coding: utf-8 -*-
"""Strokes: the paths brushes took, drawn on the model through a camera or in the UV view.

A stroke is its points, each a place on the screen of the camera the stroke was drawn through (``0..1``
across and up), or, for a stroke drawn in the UV view, a place in the UV layout. A project replays them on
whatever surface it has: a stroke through a camera lands on what the camera sees under each point, a stroke in
the UV view lands where it says.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the points of a stroke through a camera are moved on the
  screen to where the camera now sees the UV place each one used to fall on.
* ``follow_surface`` -- the UV layout moved, the mesh stayed: a stroke through a camera is right as it is, and
  the points of a stroke in the UV view are carried to where the layout now puts the surface they were on.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, placement

VIEW_2D = 0
VIEW_3D = 1
HOLDERS = ("DataActionPaint", "DataActionEraser", "DataActionSmudge")


def _strokes(material, view):
    """``(stroke, paints through)`` for each stroke of ``view`` kept by an action of the material."""
    found = []
    for action in stream.walk(material):
        if action.type not in HOLDERS:
            continue
        brush = action["brush"]
        through = brush is not None and "paintThrough" in brush and brush.number("paintThrough") != 0
        for stroke in action["strokes"]:
            if stroke.number("viewType") == view:
                found.append((stroke, through))
    return found


def _rows(stroke):
    return np.array(stroke["points"].rows(), dtype=np.float64)


def _store(stroke, rows):
    definition = stroke["points"].definition
    stroke["points"] = stream.Packed.of(definition, [(float(x), float(y), float(time), int(kind)) for x, y, time, kind in rows])


def follow_uv(material, context):
    """Move the points of the strokes drawn on the model so each lands on the UV place it fell on before."""
    tracks, rows = [], []
    for stroke, through in _strokes(material, VIEW_3D):
        key = (stroke.number("viewMatrix"), stroke.number("projectionMatrix"))
        stroke_rows = _rows(stroke)
        track = placement.Track(key, geometry.Camera(*key), stroke_rows[:, :2].copy(), through)
        tracks.append((stroke, stroke_rows, track))
    placement.follow_uv([track for _stroke, _rows_, track in tracks], context)
    counts = Counter()
    for stroke, stroke_rows, track in tracks:
        stroke_rows[:, :2] = track.points
        _store(stroke, stroke_rows)
        counts["strokes"] += 1
        counts["points"] += len(stroke_rows)
        counts["seen"] += track.seen
        counts["moved"] += track.moved
    return dict(counts)


def follow_surface(material, context):
    """Carry the points of the strokes drawn in the UV view to where the layout now puts the surface they were on."""
    counts = Counter()
    for stroke, _through in _strokes(material, VIEW_2D):
        rows = _rows(stroke)
        triangle, weights, offset = context.old_layout.query(rows[:, :2])
        found = triangle >= 0
        where = np.nonzero(found)[0]
        new_triangle, new_weights = context.counterpart.carry(triangle[where], weights[where])
        missing = new_triangle < 0
        if missing.any():
            place = context.old.point(triangle[where][missing], weights[where][missing])
            near_triangle, near_weights = context.new_space.query(place)
            new_triangle[missing] = near_triangle
            new_weights[missing] = near_weights
        placed = new_triangle >= 0
        where = where[placed]
        carried = context.new.uv(new_triangle[placed], new_weights[placed])
        carried += _carry_offset(context, triangle[where], new_triangle[placed], offset[where])
        rows[where, :2] = carried
        counts["strokes"] += 1
        counts["points"] += len(rows)
        counts["seen"] += int(found.sum())
        counts["moved"] += int(placed.sum())
        _store(stroke, rows)
    return dict(counts)


def _carry_offset(context, old_triangle, new_triangle, offset):
    """The step of ``offset`` in the old layout, as the same step across the surface in the new one."""
    old_frame, old_usable = context.old.frames(old_triangle)
    new_frame, new_usable = context.new.frames(new_triangle)
    across = np.einsum("nij,nj->ni", old_frame, offset)
    gram = np.einsum("nji,njk->nik", new_frame, new_frame)
    solved = np.linalg.solve(gram + 1e-30 * np.eye(2), np.einsum("nji,nj->ni", new_frame, across)[..., None])[..., 0]
    return np.where((old_usable & new_usable)[:, None], solved, 0.0)
