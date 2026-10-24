# -*- coding: utf-8 -*-
"""Places on the screen that paint was put at, and moving them when the surface under them changed.

A ``Track`` is some points on the screen of one camera -- the path of a stroke, the click of a polygon fill --
each the place where the camera saw the surface the paint was put on. A project replays them through the same
camera onto whatever surface it holds, so a track that is to keep landing on the same UV places has to follow the
surface when the mesh moves: the camera is moved as the patch of surface under the track moved (the best rigid
motion between the old patch and the new one), so that it sees the new patch the way it saw the old, and what is
left over -- the patch having bent -- moves the points on the screen.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from . import geometry

_MARGIN = 0.01
#: How far, in the UV layout, the patch around a track reaches, as a share of the track's own extent, and its bounds.
_REACH_SHARE = 0.1
_REACH_LEAST = 2e-3
_REACH_MOST = 0.05
_AROUND = np.array([(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)])


class Track:
    """Points (n, 2) on the screen of ``camera`` (``0..1`` across and up), and whether paint goes through the model.

    ``transform`` is what the camera was moved by when the track was followed (``None`` before, and where it did not
    move): the 4x4 rigid motion of the world the camera now sees the way it saw it. ``size_ratio`` is how many times
    longer a step of the UV layout is across the surface under the track now than it was (``None`` where unknown).
    """

    def __init__(self, key, camera, points, through):
        self.key = key
        self.camera = camera
        self.points = points
        self.through = through
        self.seen = 0
        self.moved = 0
        self.transform = None
        self.size_ratio = None


def follow_uv(tracks, context):
    """Move each track so that it lands on the UV places it fell on before, through a camera that follows the patch."""
    groups = defaultdict(list)
    for track in tracks:
        groups[track.key].append(track)
    for members in groups.values():
        screen = np.concatenate([track.points for track in members]) * 2.0 - 1.0
        index = geometry.ScreenIndex(context.old, members[0].camera, screen.min(axis=0) - _MARGIN, screen.max(axis=0) + _MARGIN)
        for track in members:
            track.seen, track.moved = _move(track, index, context)


def _patch(context, uv, place):
    """Points of the old and of the new surface at the same UV places: the track's own and some around them."""
    reach = float(np.clip(_REACH_SHARE * float((uv.max(axis=0) - uv.min(axis=0)).max()), _REACH_LEAST, _REACH_MOST))
    around = (uv[:, None, :] + reach * _AROUND[None, :, :]).reshape(-1, 2)
    nearby = np.repeat(place, len(_AROUND), axis=0)
    old_triangle, old_weights, _offset = context.old_layout.query(around, nearby)
    new_triangle, new_weights, _offset = context.new_layout.query(around, nearby)
    both = (old_triangle >= 0) & (new_triangle >= 0)
    return (context.old.point(old_triangle[both], old_weights[both]), context.new.point(new_triangle[both], new_weights[both]))


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
    if not placed.any():
        return int(seen.sum()), 0
    new_place = context.new.point(new_triangle[placed], new_weights[placed])
    patch_old, patch_new = _patch(context, uv[placed], place[placed])
    transform = geometry.rigid_fit(np.concatenate([place[placed], patch_old]), np.concatenate([new_place, patch_new]))
    track.transform = transform
    track.points[where[placed]] = track.camera.following(transform).screen(new_place)
    track.size_ratio = geometry.length_ratio(context.old, triangle[where][placed], context.new, new_triangle[placed])
    return int(seen.sum()), int(placed.sum())


def measure_layout(tracks, context):
    """For tracks through a camera on a mesh that did not change: set each ``size_ratio`` from the surface its points
    fall on, across the old layout and the new."""
    groups = defaultdict(list)
    for track in tracks:
        groups[track.key].append(track)
    for members in groups.values():
        screen = np.concatenate([track.points for track in members]) * 2.0 - 1.0
        index = geometry.ScreenIndex(context.old, members[0].camera, screen.min(axis=0) - _MARGIN, screen.max(axis=0) + _MARGIN)
        for track in members:
            triangle, weights, _depth = index.query(track.points * 2.0 - 1.0, context.old_mask if track.through else None)
            seen = triangle >= 0
            if not track.through:
                seen &= context.old_mask[np.where(seen, triangle, 0)]
            where = np.nonzero(seen)[0]
            new_triangle, _weights = context.counterpart.carry(triangle[where], weights[where])
            found = new_triangle >= 0
            track.seen = int(seen.sum())
            track.size_ratio = geometry.length_ratio(context.old, triangle[where][found], context.new, new_triangle[found])
