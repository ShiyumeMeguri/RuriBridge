# -*- coding: utf-8 -*-
"""Strokes: the paths brushes took, drawn on the model through a camera or in the UV view.

A stroke is its points, each a place on the screen of the camera the stroke was drawn through (``0..1``
across and up), or, for a stroke drawn in the UV view, a place in the UV layout. A project replays them on
whatever surface it has: a stroke through a camera lands on what the camera sees under each point, a stroke in
the UV view lands where it says.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the camera of a stroke is moved as the patch of surface under
  it moved, and the points of the stroke are moved on the screen to where that camera now sees the UV place each
  one used to fall on.
* ``follow_surface`` -- the UV layout moved, the mesh stayed: a stroke through a camera is right as it is, and
  the points of a stroke in the UV view are carried to where the layout now puts the surface they were on.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import brushes, geometry, placement

VIEW_2D = 0
VIEW_3D = 1
HOLDERS = brushes.HOLDERS
#: How far on the screen (``0..1``) a symmetry duplicate may lie from where it belongs, on the path it shares with its stroke,
#: before it is given a path of its own.
SHARED_PATH_TOLERANCE = 1e-3


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


def _mirror_of(stroke):
    return np.array(stroke.number("duplicateMatrix")).reshape(4, 4).T if stroke.number("duplicateCount") == 1 else None


class _Followed:
    """A stroke through a camera that is being followed: its action, the rows of its points as they were, and its track."""

    def __init__(self, action, stroke, rows=None, mirror=None):
        self.action = action
        self.stroke = stroke
        self.rows = _rows(stroke) if rows is None else rows
        key = (stroke.number("viewMatrix"), stroke.number("projectionMatrix"))
        self.track = placement.Track(key, geometry.Camera(*key), self.rows[:, :2].copy(), mirror)

    def of_its_own(self, uids):
        """The symmetry duplicate of this stroke as a stroke of its own: the same path through the camera mirrored by the
        duplicate matrix, the same random seed. This stroke no longer has the duplicate."""
        if uids is None:
            raise ValueError("a duplicate that is given a stroke of its own needs uids to name it")
        mirror = self.track.mirror
        own = self.stroke.copy()
        own.set_number("uid", uids.take())
        own.set_number("viewMatrix", geometry.moved_view(self.stroke.number("viewMatrix"), mirror))
        for each in (self.stroke, own):
            each.set_number("duplicateCount", 0)
            each.set_number("duplicateMatrix", (0.0,) * 16)
        self.track.mirror = None
        self.track.mirror_after = None
        return _Followed(self.action, own, self.rows.copy())

    def write(self):
        """Put the followed points, camera and duplicate matrix into the stroke."""
        self.rows[:, :2] = self.track.points
        _store(self.stroke, self.rows)
        if self.track.transform is not None:
            self.stroke.set_number("viewMatrix", geometry.moved_view(self.track.key[0], self.track.transform))
        if self.track.mirror_after is not None:
            self.stroke.set_number("duplicateMatrix", geometry.storage(self.track.mirror_after))


def _with_own(strokes, owned):
    """``strokes`` with the stroke each duplicate was given (``owned``, by ``id`` of the stroke it came from) right after that stroke."""
    return [kept for stroke in strokes for kept in ((stroke, owned[id(stroke)].stroke) if id(stroke) in owned else (stroke,))]


def follow_uv(material, context):
    """Move the points of the strokes drawn on the model so each lands on the UV place it fell on before. A symmetry duplicate
    shares the path of its stroke, so it can only follow what moved the way the stroke's own patch did; one that left the
    path further than ``SHARED_PATH_TOLERANCE`` is made a stroke of its own, with a path of its own."""
    followed = [_Followed(action, stroke, mirror=_mirror_of(stroke)) for action, stroke in _kept(material, VIEW_3D)]
    placement.follow_uv([each.track for each in followed], context)
    placement.follow_mirror([each.track for each in followed], [each.rows[:, :2] for each in followed], context)
    apart = [each for each in followed if each.track.mirror_error is not None and each.track.mirror_error > SHARED_PATH_TOLERANCE]
    owned = {id(each.stroke): each.of_its_own(context.uids) for each in apart}
    placement.follow_uv([each.track for each in owned.values()], context)
    for action in {id(each.action): each.action for each in apart}.values():
        action["strokes"] = _with_own(action["strokes"], owned)
    followed.extend(owned.values())
    counts = Counter()
    counts["duplicates given a path of their own"] = len(owned)
    for each in followed:
        each.write()
        counts["strokes"] += 1
        counts["points"] += len(each.rows)
        counts["seen"] += each.track.seen
        counts["moved"] += each.track.moved
    factors = {id(each.stroke): each.track.size_ratio for each in followed if each.track.size_ratio is not None}
    brushes.resize(material, factors, (brushes.VIEW, brushes.OBJECT), context.uids)
    return dict(counts)


def follow_surface(material, context):
    """Carry the points of the strokes drawn in the UV view to where the layout now puts the surface they were on,
    and give the strokes whose brush is measured in the layout the size that keeps their footprint on the surface."""
    counts = Counter()
    factors_2d = {}
    for stroke in _strokes(material, VIEW_2D):
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
        placement.carry_along(rows[:, :2], before, where)
        counts["strokes"] += 1
        counts["points"] += len(rows)
        counts["seen"] += int(found.sum())
        counts["moved"] += int(placed.sum())
        _store(stroke, rows)
        ratio = geometry.length_ratio(context.old, triangle[where], context.new, new_triangle[placed])
        if ratio is not None:
            factors_2d[id(stroke)] = 1.0 / ratio
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
