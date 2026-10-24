# -*- coding: utf-8 -*-
"""Which polygons a polygon fill hit chooses (what Painter does, measured polygon by polygon on a test model).

A hit finds *seed* polygons, then its granularity widens them:

* a click through a camera seeds the front-most polygon of the material under it; a click in the UV view the polygon of the
  material whose triangle in the layout holds the point;
* a rectangle through a camera seeds every polygon whose triangle on the screen overlaps the rectangle -- hidden ones too;
  a rectangle in the UV view every polygon whose triangle in the layout overlaps it;
* granularity 0 and 1 choose the seeds themselves, 2 every polygon of the element each seed belongs to (the mesh pieces
  that share vertex positions), 3 every polygon of the UV chunk (the pieces that share position and UV).

A hit with a symmetry duplicate chooses what it chooses through the camera and what it chooses through the camera
mirrored by the duplicate matrix.
"""

from __future__ import annotations

import numpy as np

ELEMENT = 2
CHUNK = 3
CENTRE = np.full((1, 3), 1.0 / 3.0)


def _labels(surface, keys):
    """The piece each triangle of ``surface`` belongs to when vertices with the same ``keys`` row are one."""
    _unique, vertex = np.unique(keys, axis=0, return_inverse=True)
    vertex = vertex.reshape(-1)
    parent = np.arange(len(_unique))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for first, second, third in vertex[surface.triangles]:
        for other in (second, third):
            left, right = find(first), find(other)
            if left != right:
                parent[right] = left
    roots = np.array([find(node) for node in vertex[surface.triangles[:, 0]]])
    return np.unique(roots, return_inverse=True)[1].reshape(-1)


def elements(surface):
    """For every triangle of ``surface`` its element: triangles joined through vertices at the same place in space."""
    return _labels(surface, np.round(surface.positions, 6))


def chunks(surface):
    """For every triangle of ``surface`` its UV chunk: triangles joined through vertices at the same place and the same UV."""
    return _labels(surface, np.round(np.concatenate([surface.positions, surface.uvs], axis=1), 6))


def overlapping(corners, low, high):
    """Which of the 2D triangles ``corners`` (n, 3, 2) share a point with the rectangle ``low..high``."""
    corners = np.asarray(corners, dtype=np.float64)
    inside = ((corners >= low) & (corners <= high)).all(axis=2).any(axis=1)
    rectangle = np.array([[low[0], low[1]], [high[0], low[1]], [high[0], high[1]], [low[0], high[1]]])
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]

    def side(p, q, r):
        return (q[..., 0] - p[..., 0]) * (r[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (r[..., 0] - p[..., 0])

    holds = np.zeros(len(corners), dtype=bool)
    for point in rectangle:
        signs = np.stack([side(a, b, point), side(b, c, point), side(c, a, point)], axis=1)
        holds |= (signs >= 0).all(axis=1) | (signs <= 0).all(axis=1)
    crossing = np.zeros(len(corners), dtype=bool)
    for first, second in ((a, b), (b, c), (c, a)):
        for index in range(4):
            third, fourth = rectangle[index], rectangle[(index + 1) % 4]
            crossing |= (side(first, second, third) * side(first, second, fourth) < 0) & (side(third, fourth, first) * side(third, fourth, second) < 0)
    return inside | holds | crossing


def on_screen(surface, camera, low, high):
    """The triangles of ``surface`` whose projection through ``camera`` overlaps the rectangle ``low..high`` (``0..1`` across
    and up); a triangle with a corner behind the camera is left out."""
    clip = camera.clip(surface.positions)
    in_front = clip[:, 3] > 1e-9
    screen = np.where(in_front[:, None], clip[:, :2] / np.where(in_front, clip[:, 3], 1.0)[:, None], 0.0) * 0.5 + 0.5
    corners = screen[surface.triangles]
    possible = in_front[surface.triangles].all(axis=1) & (corners.max(axis=1) >= low).all(axis=1) & (corners.min(axis=1) <= high).all(axis=1)
    found = np.zeros(len(surface.triangles), dtype=bool)
    found[possible] = overlapping(corners[possible], low, high)
    return found


def widened(seeds, granularity, labels_of):
    """The polygons ``granularity`` chooses from ``seeds`` (a boolean per triangle); ``labels_of(kind)`` gives the
    element or chunk labels of every triangle."""
    if granularity not in (ELEMENT, CHUNK):
        return seeds
    labels = labels_of(granularity)
    return np.isin(labels, np.unique(labels[seeds]))


def centroid_uv(surface, triangles):
    """The UV of the middle of each of ``triangles``."""
    return surface.uv(np.asarray(triangles, dtype=np.int64), np.repeat(CENTRE, len(triangles), axis=0))
