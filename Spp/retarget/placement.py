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
#: At most this many places of a track are used to find the motion of its patch.
_PATCH_PLACES = 48


class Track:
    """Points (n, 2) on the screen of ``camera`` (``0..1`` across and up).

    ``transform`` is what the camera was moved by when the track was followed (``None`` before, and where it did not
    move): the 4x4 rigid motion of the world the camera now sees the way it saw it. ``size_ratio`` is how many times
    longer a step of the UV layout is across the surface under the track now than it was (``None`` where unknown).
    ``mirror`` is the 4x4 matrix of the symmetry duplicate of the paint -- the same path seen through the camera mirrored
    by it -- or ``None``; ``mirror_after`` the matrix that keeps the duplicate where it fell, once followed.
    """

    def __init__(self, key, camera, points, mirror=None):
        self.key = key
        self.camera = camera
        self.points = points
        self.mirror = mirror
        self.seen = 0
        self.moved = 0
        self.transform = None
        self.size_ratio = None
        self.mirror_after = None


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


def follow_mirror(tracks, old_points, context):
    """For the tracks that have a symmetry duplicate and were followed (``old_points`` their points before): set each
    ``mirror_after`` to the matrix that keeps the duplicate on the UV places it fell on. The duplicate camera follows the
    rigid motion of the patch it sees, so the matrix is that motion, then the old matrix, then the track's own motion undone."""
    for track, before in zip(tracks, old_points):
        if track.mirror is None:
            continue
        screen = before * 2.0 - 1.0
        index = geometry.ScreenIndex(context.old, track.camera.following(track.mirror), screen.min(axis=0) - _MARGIN, screen.max(axis=0) + _MARGIN)
        _where, triangle, weights = _seen(index, context, screen)
        uv, place, new_triangle, new_weights = across(context, triangle, weights)
        placed = new_triangle >= 0
        if placed.any():
            patch = rigid(context, uv[placed], place[placed], context.new.point(new_triangle[placed], new_weights[placed]))
            own = track.transform if track.transform is not None else np.eye(4)
            track.mirror_after = patch @ track.mirror @ np.linalg.inv(own)


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
            where, triangle, weights = _seen(index, context, track.points * 2.0 - 1.0)
            new_triangle, _weights = context.counterpart.carry(triangle, weights)
            found = new_triangle >= 0
            track.seen = len(where)
            track.size_ratio = geometry.length_ratio(context.old, triangle[found], context.new, new_triangle[found])


def _seen(index, context, screen):
    """The points (``screen``, ``-1..1``) that the camera of ``index`` sees on the old surface, and the triangle and
    weights it sees under each: ``(where, triangle, weights)``. A material is painted by the strokes of its own stack
    through its own surface only: a surface of another material in front does not hide it from the brush."""
    triangle, weights, _depth = index.query(screen, context.old_mask)
    where = np.nonzero(triangle >= 0)[0]
    return where, triangle[where], weights[where]


def across(context, triangle, weights):
    """Places of the old surface, and where the same places of the layout are on the new: ``(uv, old point, new triangle,
    new weights)``, the triangle ``-1`` where the new layout has nothing there."""
    uv = context.old.uv(triangle, weights)
    place = context.old.point(triangle, weights)
    new_triangle, new_weights, _offset = context.new_layout.query(uv, place)
    return uv, place, new_triangle, new_weights


def rigid(context, uv, place, new_place):
    """The rigid motion of the patch of surface around the places ``uv`` (old points ``place``, new points ``new_place``):
    the patch is some of the places themselves and some around them."""
    reach = float(np.clip(_REACH_SHARE * float((uv.max(axis=0) - uv.min(axis=0)).max()), _REACH_LEAST, _REACH_MOST))
    chosen = np.linspace(0, len(uv) - 1, min(len(uv), _PATCH_PLACES)).astype(np.int64)
    uv, place, new_place = uv[chosen], place[chosen], new_place[chosen]
    around = (uv[:, None, :] + reach * _AROUND[None, :, :]).reshape(-1, 2)
    nearby = np.repeat(place, len(_AROUND), axis=0)
    old_triangle, old_weights, _offset = context.old_layout_exact.query(around, nearby)
    new_triangle, new_weights, _offset = context.new_layout.query(around, nearby)
    both = (old_triangle >= 0) & (new_triangle >= 0)
    return geometry.rigid_fit(np.concatenate([place, context.old.point(old_triangle[both], old_weights[both])]),
                              np.concatenate([new_place, context.new.point(new_triangle[both], new_weights[both])]))


def _move(track, index, context):
    where, triangle, weights = _seen(index, context, track.points * 2.0 - 1.0)
    uv, place, new_triangle, new_weights = across(context, triangle, weights)
    placed = new_triangle >= 0
    if not placed.any():
        return len(where), 0
    new_place = context.new.point(new_triangle[placed], new_weights[placed])
    track.transform = rigid(context, uv[placed], place[placed], new_place)
    track.points[where[placed]] = track.camera.following(track.transform).screen(new_place)
    track.size_ratio = geometry.length_ratio(context.old, triangle[placed], context.new, new_triangle[placed])
    return len(where), int(placed.sum())
