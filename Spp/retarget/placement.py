# -*- coding: utf-8 -*-
"""Places on the screen that paint was put at, and moving them when the surface under them changed.

A ``Track`` is some points on the screen of one camera -- the path of a stroke, the click of a polygon fill --
each the place where the camera saw the surface the paint was put on. A project replays them through the same
camera onto whatever surface it holds, so a track that is to keep landing on the same UV places has to be
moved when the mesh moves.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from . import geometry

_MARGIN = 0.01


class Track:
    """Points (n, 2) on the screen of ``camera`` (``0..1`` across and up), and whether paint goes through the model."""

    def __init__(self, key, camera, points, through):
        self.key = key
        self.camera = camera
        self.points = points
        self.through = through
        self.seen = 0
        self.moved = 0


def follow_uv(tracks, context):
    """Move each track's points so the camera sees, under each, the UV place it saw there before."""
    groups = defaultdict(list)
    for track in tracks:
        groups[track.key].append(track)
    for members in groups.values():
        screen = np.concatenate([track.points for track in members]) * 2.0 - 1.0
        index = geometry.ScreenIndex(context.old, members[0].camera, screen.min(axis=0) - _MARGIN, screen.max(axis=0) + _MARGIN)
        for track in members:
            track.seen, track.moved = _move(track, index, context)


def _move(track, index, context):
    screen = track.points * 2.0 - 1.0
    triangle, weights, _depth = index.query(screen, context.old_mask if track.through else None)
    seen = triangle >= 0
    if not track.through:
        seen &= context.old_mask[np.where(seen, triangle, 0)]
    where = np.nonzero(seen)[0]
    place = context.old.point(triangle[where], weights[where])
    uv = context.old.uv(triangle[where], weights[where])
    new_triangle, new_weights, _offset = context.new_layout.query(uv, place)
    placed = new_triangle >= 0
    track.points[where[placed]] = track.camera.screen(context.new.point(new_triangle[placed], new_weights[placed]))
    return int(seen.sum()), int(placed.sum())
