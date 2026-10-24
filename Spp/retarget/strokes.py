# -*- coding: utf-8 -*-
"""Strokes: the paths brushes took, drawn on the model through a camera or in the UV view.

A stroke is its points, each a place on the screen of the camera the stroke was drawn through (``0..1``
across and up), or, for a stroke drawn in the UV view, a place in the UV layout. A project replays them on
whatever surface it has: a stroke through a camera lands on what the camera sees under each point, a stroke in
the UV view lands where it says.

* ``follow_surface`` -- the UV layout changed, the mesh stayed: a stroke through a camera is right as it is, and
  the points of a stroke in the UV view are carried to where the layout now puts the surface they were on. Paint carried
  with faces into another material (``Context.carrying``) is this too, but a stroke keeps only the points that lay on those
  faces: the runs of them are its strokes.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import brushes, geometry, placement

VIEW_2D = 0
VIEW_3D = 1
HOLDERS = brushes.HOLDERS


def _strokes(material, view):
    """Each stroke of ``view`` kept by an action of the material."""
    return [stroke for _action, stroke in _kept(material, view)]


def _kept(material, view):
    """``(action, stroke)`` for each stroke of ``view`` kept by an action of the material."""
    return [(action, stroke) for action in stream.walk(material) if action.type in HOLDERS for stroke in action["strokes"] if stroke.number("viewType") == view]


def _rows(stroke):
    return np.array(stroke["points"].rows(), dtype=np.float64)


def _store(stroke, rows):
    definition = stroke["points"].definition
    stroke["points"] = stream.Packed.of(definition, [(float(x), float(y), float(time), int(kind)) for x, y, time, kind in rows])


def _per_point(stroke, field, count):
    """The rows of an array a stroke keeps for each of its points (pressure, tilt), ``None`` for a stroke that has none."""
    values = stroke[field]
    if not isinstance(values, stream.Packed) or not values.count:
        return None
    rows = values.rows()
    if len(rows) != count:
        raise ValueError("a stroke has {0} values of {1} for {2} points".format(len(rows), field, count))
    return rows


def _remnants(stroke, rows, kept):
    """The strokes a stroke becomes when only its points ``kept`` (indices, ascending) remain: one for each run of consecutive
    ones -- the stroke itself for the first, copies of it for the others -- and none when no point remains. A copy has no uid
    yet."""
    if not len(kept):
        return []
    runs = np.split(kept, np.nonzero(np.diff(kept) > 1)[0] + 1)
    arrays = {field: _per_point(stroke, field, len(rows)) for field in ("pressure", "tilt")}
    pieces = [stroke] + [stroke.copy() for _run in runs[1:]]
    for piece, run in zip(pieces, runs):
        _store(piece, rows[run])
        for field, values in arrays.items():
            if values is not None:
                piece[field] = stream.Packed.of(piece[field].definition, [values[index] for index in run])
    return pieces


def follow_surface(material, context):
    """Carry the points of the strokes drawn in the UV view to where the layout now puts the surface they were on,
    and give the strokes whose brush is measured in the layout the size that keeps their footprint on the surface."""
    counts = Counter()
    factors_2d = {}
    remnants = {}
    for action, stroke in _kept(material, VIEW_2D):
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
        before = rows[:, :2].copy()
        rows[where, :2] = carried
        counts["strokes"] += 1
        counts["points"] += len(rows)
        counts["seen"] += int(found.sum())
        counts["moved"] += int(placed.sum())
        if context.carrying:
            pieces = _remnants(stroke, rows, where)
            for piece in pieces[1:]:
                piece.set_number("uid", context.uids.take())
            remnants[id(stroke)] = (action, pieces)
            counts["strokes left with no point"] += 0 if pieces else 1
            counts["strokes cut in pieces"] += max(0, len(pieces) - 1)
        else:
            placement.carry_along(rows[:, :2], before, where)
            _store(stroke, rows)
            pieces = [stroke]
        ratio = geometry.length_ratio(context.old, triangle[where], context.new, new_triangle[placed])
        if ratio is not None:
            for piece in pieces:
                factors_2d[id(piece)] = 1.0 / ratio
    for action in {id(action): action for action, _pieces in remnants.values()}.values():
        action["strokes"] = [piece for member in action["strokes"] for piece in (remnants[id(member)][1] if id(member) in remnants else [member])]
    brushes.resize(material, factors_2d, (brushes.VIEW, brushes.TEXTURE), context.uids)
    tracks = []
    for stroke in _strokes(material, VIEW_3D):
        key = (stroke.number("viewMatrix"), stroke.number("projectionMatrix"))
        tracks.append((stroke, placement.Track(key, geometry.Camera(*key), _rows(stroke)[:, :2].copy())))
    placement.measure_layout([track for _stroke, track in tracks], context)
    factors_3d = {id(stroke): 1.0 / track.size_ratio for stroke, track in tracks if track.size_ratio is not None}
    brushes.resize(material, factors_3d, (brushes.TEXTURE,), context.uids)
    return dict(counts)


def _carry_offset(context, old_triangle, new_triangle, offset):
    """The step of ``offset`` in the old layout, as the same step across the surface in the new one."""
    old_frame, old_usable = context.old.frames(old_triangle)
    new_frame, new_usable = context.new.frames(new_triangle)
    across = np.einsum("nij,nj->ni", old_frame, offset)
    gram = np.einsum("nji,njk->nik", new_frame, new_frame)
    solved = np.linalg.solve(gram + 1e-30 * np.eye(2), np.einsum("nji,nj->ni", new_frame, across)[..., None])[..., 0]
    return np.where((old_usable & new_usable)[:, None], solved, 0.0)
