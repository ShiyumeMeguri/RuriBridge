# -*- coding: utf-8 -*-
"""Polygon fills: the clicks and rectangles that chose which polygons a fill covers.

A click (``DataDecalHitPunctual``) is one place on the screen of the camera it was made through; the polygon
under it, or the mesh element or UV chunk the polygon belongs to, is what it chose. A rectangle
(``DataDecalHitRect``) chose what its two corners enclose. Both are replayed on whatever surface the project
holds, through the camera the hit keeps as the inverse of its projection and view together.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the camera of a click is moved as the patch of surface under
  it moved and the click is moved on the screen to where that camera now sees the UV place it fell on, as a
  stroke's point is. A rectangle chooses by what lies under an area of the screen, which no move of its corners
  keeps; it is reported, not carried.
* ``follow_surface`` -- the UV layout moved, the mesh stayed: the click of a hit made in the UV view is carried to
  where the layout now puts the polygon that was under it (a click through a camera is right as it is).
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, placement

HOLDER = "DataActionDecal"
PUNCTUAL = "DataDecalHitPunctual"
VIEW_2D = 0
VIEW_3D = 1


def _hits(material, kind, view):
    return [hit for action in stream.walk(material) if action.type == HOLDER for hit in action["hits"]
            if hit.type == kind and hit.number("viewType") == view]


def follow_surface(material, context):
    """Carry the clicks of the polygon fills made in the UV view to where the layout now puts the polygon under them."""
    counts = Counter()
    for hit in _hits(material, PUNCTUAL, VIEW_2D):
        position = np.array([hit.number("position")], dtype=np.float64)
        triangle, weights, _offset = context.old_layout.query(position)
        counts["clicks"] += 1
        if triangle[0] < 0:
            continue
        counts["seen"] += 1
        new_triangle, new_weights = context.counterpart.carry(triangle, weights)
        if new_triangle[0] < 0:
            continue
        counts["moved"] += 1
        hit.set_number("position", tuple(float(value) for value in context.new.uv(new_triangle, new_weights)[0]))
    return dict(counts)


def follow_uv(material, context):
    """Move the clicks of the polygon fills made through a camera so each lands on the UV place it fell on before."""
    tracks = []
    for hit in _hits(material, PUNCTUAL, VIEW_3D):
        key = hit.number("projViewInvMatrix")
        position = np.array([hit.number("position")], dtype=np.float64)
        tracks.append((hit, placement.Track(key, geometry.Camera.from_inverse(key), position)))
    placement.follow_uv([track for _hit, track in tracks], context)
    counts = Counter()
    for hit, track in tracks:
        hit.set_number("position", tuple(float(value) for value in track.points[0]))
        if track.transform is not None:
            hit.set_number("projViewInvMatrix", geometry.moved_inverse(track.key, track.transform))
        counts["clicks"] += 1
        counts["seen"] += track.seen
        counts["moved"] += track.moved
    return dict(counts)
