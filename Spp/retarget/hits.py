# -*- coding: utf-8 -*-
"""Polygon fills: the clicks and rectangles that chose which polygons a fill covers.

A click (``DataDecalHitPunctual``) is one place on the screen of the camera it was made through; the polygon
under it, or the mesh element or UV chunk the polygon belongs to, is what it chose. A rectangle
(``DataDecalHitRect``) chose what its two corners enclose. Both are replayed on whatever surface the project
holds, through the camera the hit keeps as the inverse of its projection and view together.

* ``follow_uv`` -- the mesh moved, the UV layout stayed: the click of a hit through a camera is moved on the screen
  to where the camera now sees the UV place it fell on, as a stroke's point is. A rectangle chooses by what lies
  under an area of the screen, which no move of its corners keeps; it is reported, not carried.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream
from . import geometry, placement

HOLDER = "DataActionDecal"
PUNCTUAL = "DataDecalHitPunctual"
VIEW_3D = 1


def _hits(material, kind, view):
    return [hit for action in stream.walk(material) if action.type == HOLDER for hit in action["hits"]
            if hit.type == kind and hit.number("viewType") == view]


def follow_uv(material, context):
    """Move the clicks of the polygon fills made through a camera so each lands on the UV place it fell on before."""
    tracks = []
    for hit in _hits(material, PUNCTUAL, VIEW_3D):
        key = hit.number("projViewInvMatrix")
        through = "paintThrough" in hit and hit.number("paintThrough") != 0
        position = np.array([hit.number("position")], dtype=np.float64)
        tracks.append((hit, placement.Track(key, geometry.Camera.from_inverse(key), position, through)))
    placement.follow_uv([track for _hit, track in tracks], context)
    counts = Counter()
    for hit, track in tracks:
        hit.set_number("position", tuple(float(value) for value in track.points[0]))
        counts["clicks"] += 1
        counts["seen"] += track.seen
        counts["moved"] += track.moved
    return dict(counts)
