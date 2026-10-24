# -*- coding: utf-8 -*-
"""Places on the screen that paint was put at, and how long a step of the UV layout is across the surface under them.

A ``Track`` is some points on the screen of one camera -- the path of a stroke -- each the place where the camera saw
the surface the paint was put on. When the UV layout changed under a mesh that stayed, a brush measured in the texture
covers another stretch of the surface, which the size of the brush makes up for (``measure_layout``).
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from . import geometry

_MARGIN = 0.01


class Track:
    """Points (n, 2) on the screen of ``camera`` (``0..1`` across and up). ``size_ratio`` is how many times longer a step of the UV
    layout is across the surface under the track now than it was (``None`` where unknown), ``seen`` how many of the points fall on
    the surface of the material."""

    def __init__(self, key, camera, points):
        self.key = key
        self.camera = camera
        self.points = points
        self.seen = 0
        self.size_ratio = None


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


def carry_along(points, before, moved):
    """Move the points of a path that were not placed -- the camera saw no surface of the material there, or the new surface has
    none -- by the step of their neighbours along the path (``moved`` the indices of the points that were placed, ``before`` where
    all of them were), so that what the path did beside the surface it still does: a point just off the silhouette stays just off it."""
    rest = np.setdiff1d(np.arange(len(points)), moved)
    if not len(rest) or not len(moved):
        return
    step = points[moved] - before[moved]
    for axis in range(2):
        points[rest, axis] = before[rest, axis] + np.interp(rest, moved, step[:, axis])
