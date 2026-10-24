# -*- coding: utf-8 -*-
"""Fills: an image or a procedural laid over the surface through a projection.

A fill laid through the UV layout puts its content at fixed places of the layout, so a layout that moves the
surface leaves the content where it was. The UV set to UV set projection takes the content from another UV set of
the mesh and lays it on the surface by way of that set; a fill given the set that still holds the layout the paint
was made in follows the surface through any layout the mesh has now, live, with nothing baked.

* ``follow_layout`` -- the layout changed, the mesh stayed: the fills laid through the UV layout are switched to
  the UV set to UV set projection from the set of the new mesh that holds the old layout.
* ``follow_space`` -- the mesh moved, the layout stayed: a fill laid on the surface through a frame in space (planar,
  triplanar, spherical, cylindrical, warp) keeps the same places of the surface where the frame moves with the surface; that
  is exact where the surface moved as one rigid body; where most of it did (half of it changed, half left) the frames follow
  that motion and the share of the surface off it is reported, and where none did nothing is moved and the fills are counted.
  The frame is a position ``t`` (scene units), Euler angles giving the rotation ``R = Rx Ry Rz`` and a size ``S``; it takes
  a point ``p`` to ``S^-1 R^T (p - t)`` (measured on a planar fill, where half of that plus a half is the place in the
  picture), and a motion ``(Q, q)`` of the surface makes ``t' = Q t + q`` and ``R' = Q R``.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import stream

FILL = "DataActionFill"
UV_PROJECTION = 0
UV_SET_TO_UV_SET = 9
SPACE_PROJECTIONS = (1, 2, 3, 4, 7)
#: The share of the surface that has to have moved as one rigid body for the frames to follow it.
MAJORITY = 0.6


def follow_layout(material, context):
    """Lay every fill that goes through the UV layout through the set that holds the layout it was painted in."""
    counts = Counter()
    for item in stream.walk(material):
        if item.type != FILL or item.number("projection") != UV_PROJECTION:
            continue
        item.set_number("projection", UV_SET_TO_UV_SET)
        item.set_number("uvTransferChannel", context.source_set)
        counts["fills"] += 1
    return dict(counts)


def _axis(kind, angle):
    c, s = np.cos(angle), np.sin(angle)
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[kind]


def _rotation(angles):
    """``Rx Ry Rz`` of the Euler angles (x, y, z) a frame keeps."""
    return _axis("x", angles[0]) @ _axis("y", angles[1]) @ _axis("z", angles[2])


def _angles(rotation):
    """The Euler angles (x, y, z) whose ``Rx Ry Rz`` is ``rotation``."""
    around_y = np.arcsin(np.clip(rotation[0, 2], -1.0, 1.0))
    if abs(np.cos(around_y)) < 1e-6:
        return (float(np.arctan2(rotation[2, 1], rotation[1, 1])), float(around_y), 0.0)
    return (float(np.arctan2(-rotation[1, 2], rotation[2, 2])), float(around_y), float(np.arctan2(-rotation[0, 1], rotation[0, 0])))


def follow_space(material, context):
    """Move the frame of every fill laid on the surface through a frame in space the way the surface moved, if it moved as one
    rigid body."""
    fills = [item for item in stream.walk(material) if item.type == FILL and item.number("projection") in SPACE_PROJECTIONS]
    if not fills:
        return {}
    motion, share = context.motion
    if share < MAJORITY:
        return {"not moved as one rigid body": len(fills)}
    off = {"share of the surface off that motion": round(1.0 - share, 4)} if share < 1.0 else {}
    if np.allclose(motion, np.eye(4), atol=1e-9):
        return {"fills": len(fills), "moved": 0, **off}
    counts = Counter()
    for fill in fills:
        fill.set_number("dddPosition", tuple(float(value) for value in motion[:3, :3] @ np.array(fill.number("dddPosition")) + motion[:3, 3]))
        fill.set_number("dddEulerAngles", _angles(motion[:3, :3] @ _rotation(fill.number("dddEulerAngles"))))
        symmetry = fill["symmetry"]
        if symmetry is not None and symmetry.number("enabled"):
            mirror = np.array(symmetry.number("matrix")).reshape(4, 4).T
            symmetry.set_number("matrix", tuple(float(value) for value in (motion @ mirror @ np.linalg.inv(motion)).T.reshape(-1)))
        counts["fills"] += 1
        counts["moved"] += 1
    return {**counts, **off}
