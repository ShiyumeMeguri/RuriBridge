# -*- coding: utf-8 -*-
"""Points on a triangle mesh, found from a camera, from a place in the UV layout and from a place in space.

A ``Surface`` is the mesh as numpy arrays. The three indexes each answer one question about it: which triangle
a camera sees at a point of the screen (``ScreenIndex``), which triangle lies at a place in the UV layout
(``UvIndex``), and which triangle is nearest a point in space (``SpaceIndex``). Each is a uniform grid of the
triangles' bounding boxes, built with array operations and queried a batch at a time.
"""

from __future__ import annotations

import numpy as np

_TOLERANCE = 1e-7
_BATCH = 40000
_PERMUTATIONS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


class Surface:
    """A triangle mesh: where its vertices are and lie in the UV layout, and what each triangle paints into."""

    def __init__(self, positions, uvs, triangles, materials, names):
        self.positions = positions
        self.uvs = uvs
        self.triangles = triangles
        self.materials = materials
        self.names = list(names)

    @classmethod
    def of(cls, scene, uv_set=0):
        """The surface of a ``Spp.scene.Scene``, its meshes joined, with the UVs of set ``uv_set``."""
        positions, uvs, triangles, materials = [], [], [], []
        offset = 0
        for mesh in scene.meshes:
            vertices = np.frombuffer(mesh.positions, dtype=np.float32).reshape(-1, 3).astype(np.float64)
            positions.append(vertices)
            uvs.append(np.frombuffer(mesh.uvs[uv_set][1], dtype=np.float32).reshape(-1, 3)[:, :2].astype(np.float64))
            indices = np.frombuffer(mesh.indices, dtype=np.uint32).reshape(-1, 3).astype(np.int64)
            triangles.append(indices + offset)
            materials.append(np.full(len(indices), mesh.material, dtype=np.int64))
            offset += len(vertices)
        return cls(np.concatenate(positions), np.concatenate(uvs), np.concatenate(triangles), np.concatenate(materials),
                   scene.materials)

    def mask(self, name):
        """Which triangles paint into the material called ``name``."""
        return self.materials == self.names.index(name)

    def point(self, triangles, weights):
        """The position at ``weights`` of each of ``triangles``."""
        return np.einsum("nk,nkd->nd", weights, self.positions[self.triangles[triangles]])

    def uv(self, triangles, weights):
        """The UV position at ``weights`` of each of ``triangles``."""
        return np.einsum("nk,nkd->nd", weights, self.uvs[self.triangles[triangles]])

    def frames(self, triangles):
        """For each of ``triangles`` the 3x2 matrix taking a step in the UV layout to the step in space it makes,
        and whether the triangle has a layout to speak of (``False`` where it is flat in UV)."""
        corners = self.positions[self.triangles[triangles]]
        layout = self.uvs[self.triangles[triangles]]
        steps = np.stack([corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]], axis=2)
        around = np.stack([layout[:, 1] - layout[:, 0], layout[:, 2] - layout[:, 0]], axis=2)
        determinant = around[:, 0, 0] * around[:, 1, 1] - around[:, 0, 1] * around[:, 1, 0]
        usable = np.abs(determinant) > 1e-18
        safe = np.where(usable, determinant, 1.0)
        inverse = np.stack([np.stack([around[:, 1, 1], -around[:, 0, 1]], axis=1),
                            np.stack([-around[:, 1, 0], around[:, 0, 0]], axis=1)], axis=1) / safe[:, None, None]
        return np.einsum("nij,njk->nik", steps, inverse), usable


class Camera:
    """A view and a projection as Painter stores them: sixteen floats each, column by column."""

    def __init__(self, view, projection):
        self.matrix = (np.array(projection, dtype=np.float64).reshape(4, 4).T
                       @ np.array(view, dtype=np.float64).reshape(4, 4).T)

    @classmethod
    def from_inverse(cls, inverse):
        """The camera whose projection and view together are the inverse of the sixteen floats ``inverse``."""
        camera = cls.__new__(cls)
        camera.matrix = np.linalg.inv(np.array(inverse, dtype=np.float64).reshape(4, 4).T)
        return camera

    def following(self, transform):
        """The camera that sees the world moved by ``transform`` (4x4) the way this one sees the world."""
        camera = Camera.__new__(Camera)
        camera.matrix = self.matrix @ np.linalg.inv(transform)
        return camera

    def clip(self, points):
        """The homogeneous clip-space position of each of ``points``."""
        homogeneous = np.concatenate([points, np.ones((len(points), 1))], axis=1)
        return homogeneous @ self.matrix.T

    def screen(self, points):
        """Where ``points`` fall on the screen, as Painter's strokes hold it: ``0..1`` across and up."""
        clip = self.clip(points)
        return (clip[:, :2] / clip[:, 3:4]) * 0.5 + 0.5


def length_ratio(old, old_triangles, new, new_triangles):
    """How many times longer, on average, a step in the UV layout is across ``new`` than across ``old``, at the
    places the two lists of triangles are (geometric mean over the triangles that have a layout); ``None`` where none has."""
    old_frames, old_usable = old.frames(old_triangles)
    new_frames, new_usable = new.frames(new_triangles)
    usable = old_usable & new_usable
    if not usable.any():
        return None

    def log_area(frames):
        return np.log(np.abs(np.linalg.det(np.einsum("nji,njk->nik", frames, frames))) + 1e-300)

    return float(np.exp(0.25 * np.mean(log_area(new_frames[usable]) - log_area(old_frames[usable]))))


def storage(matrix):
    """The sixteen floats Painter keeps for the 4x4 ``matrix``: column by column."""
    return tuple(float(value) for value in matrix.T.reshape(-1))


def look_at(eye, target, up):
    """The 4x4 view of a camera at ``eye`` looking at ``target`` with ``up`` above it: it looks down its own -z."""
    eye, target, up = (np.asarray(value, dtype=np.float64) for value in (eye, target, up))
    forward = target - eye
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, up)
    right = right / np.linalg.norm(right)
    view = np.eye(4)
    view[0, :3], view[1, :3], view[2, :3] = right, np.cross(right, forward), -forward
    view[:3, 3] = -view[:3, :3] @ eye
    return view


def perspective(focal, near, far):
    """The 4x4 projection of a camera with ``focal`` (1 / tan of half its field of view) seeing between ``near`` and ``far``."""
    matrix = np.zeros((4, 4))
    matrix[0, 0] = matrix[1, 1] = focal
    matrix[2, 2] = (near + far) / (near - far)
    matrix[2, 3] = 2.0 * near * far / (near - far)
    matrix[3, 2] = -1.0
    return matrix


def _cells(low, high, origin, size, resolution):
    first = np.clip(np.floor((low - origin) / size).astype(np.int64), 0, resolution - 1)
    last = np.clip(np.floor((high - origin) / size).astype(np.int64), 0, resolution - 1)
    return first, last


def _grid(first, last, resolution, ids):
    """Boxes covering cells ``first..last`` filed by cell: ``(start of each cell, the ids of the boxes by cell)``."""
    dimensions = first.shape[1]
    spans = last - first + 1
    counts = spans.prod(axis=1)
    total = int(counts.sum())
    owner = np.repeat(np.arange(len(ids)), counts)
    local = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    cell = np.zeros(total, dtype=np.int64)
    stride = 1
    for axis in range(dimensions):
        width = spans[owner, axis]
        cell += (first[owner, axis] + local % width) * stride
        local = local // width
        stride *= resolution
    order = np.argsort(cell, kind="stable")
    starts = np.searchsorted(cell[order], np.arange(resolution ** dimensions + 1))
    return starts, ids[owner[order]]


def _candidates(starts, entries, cells):
    """The (query, triangle) pairs of the triangles filed under each query's cell."""
    first = starts[cells]
    counts = starts[cells + 1] - first
    query = np.repeat(np.arange(len(cells)), counts)
    local = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    return query, entries[first[query] + local]


def _barycentric(corners, points):
    """Weights of ``points`` in the 2D triangles ``corners`` (n, 3, 2), and whether each lies inside."""
    a = corners[:, 0]
    v0 = corners[:, 1] - a
    v1 = corners[:, 2] - a
    v2 = points - a
    d00 = np.einsum("nk,nk->n", v0, v0)
    d01 = np.einsum("nk,nk->n", v0, v1)
    d11 = np.einsum("nk,nk->n", v1, v1)
    d20 = np.einsum("nk,nk->n", v2, v0)
    d21 = np.einsum("nk,nk->n", v2, v1)
    denominator = d00 * d11 - d01 * d01
    usable = np.abs(denominator) > 1e-30
    safe = np.where(usable, denominator, 1.0)
    v = (d11 * d20 - d01 * d21) / safe
    w = (d00 * d21 - d01 * d20) / safe
    weights = np.stack([1.0 - v - w, v, w], axis=1)
    return weights, usable & (weights >= -_TOLERANCE).all(axis=1)


def closest(points, corners):
    """The nearest place of each triangle of ``corners`` (n, 3, 3) to its point of ``points`` (n, 3).

    ``(weights, distance)``: the barycentric weights of that place and how far it is from the point.
    """
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    ab, ac = b - a, c - a
    ap = points - a
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    bp = points - b
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    cp = points - c
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    count = len(points)
    weights = np.zeros((count, 3))
    done = np.zeros(count, dtype=bool)

    def take(condition, values):
        nonlocal done
        condition = condition & ~done
        weights[condition] = values[condition]
        done = done | condition

    one, zero = np.ones(count), np.zeros(count)
    take((d1 <= 0) & (d2 <= 0), np.stack([one, zero, zero], axis=1))
    take((d3 >= 0) & (d4 <= d3), np.stack([zero, one, zero], axis=1))
    along_ab = np.divide(d1, d1 - d3, out=np.zeros(count), where=(d1 - d3) != 0)
    take((vc <= 0) & (d1 >= 0) & (d3 <= 0), np.stack([1.0 - along_ab, along_ab, zero], axis=1))
    take((d6 >= 0) & (d5 <= d6), np.stack([zero, zero, one], axis=1))
    along_ac = np.divide(d2, d2 - d6, out=np.zeros(count), where=(d2 - d6) != 0)
    take((vb <= 0) & (d2 >= 0) & (d6 <= 0), np.stack([1.0 - along_ac, zero, along_ac], axis=1))
    span = (d4 - d3) + (d5 - d6)
    along_bc = np.divide(d4 - d3, span, out=np.zeros(count), where=span != 0)
    take((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), np.stack([zero, 1.0 - along_bc, along_bc], axis=1))
    total = va + vb + vc
    inverse = np.divide(1.0, total, out=np.zeros(count), where=total != 0)
    v, w = vb * inverse, vc * inverse
    take(~done, np.stack([1.0 - v - w, v, w], axis=1))
    place = np.einsum("nk,nkd->nd", weights, corners)
    return weights, np.linalg.norm(place - points, axis=1)


def _dot(left, right):
    return np.einsum("nk,nk->n", left, right)


def _segment_hits(starts, ends, corners):
    """Whether each segment from ``starts`` to ``ends`` (n, 3) passes through its triangle ``corners`` (n, 3, 3)."""
    first, second = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
    direction = ends - starts
    across = np.cross(direction, second)
    determinant = _dot(first, across)
    usable = np.abs(determinant) > 1e-30
    safe = np.where(usable, determinant, 1.0)
    offset = starts - corners[:, 0]
    beside = np.cross(offset, first)
    u = _dot(offset, across) / safe
    v = _dot(direction, beside) / safe
    along = _dot(second, beside) / safe
    return usable & (u >= -_TOLERANCE) & (v >= -_TOLERANCE) & (u + v <= 1.0 + _TOLERANCE) & (along >= -_TOLERANCE) & (along <= 1.0 + _TOLERANCE)


def _first_by_query(query, score, count):
    """For each of ``count`` queries the position among the pairs of its lowest score, ``-1`` where it has none."""
    order = np.lexsort((score, query))
    present, first = np.unique(query[order], return_index=True)
    best = np.full(count, -1, dtype=np.int64)
    best[present] = order[first]
    return best


class ScreenIndex:
    """The triangles of a surface as a camera sees them, findable by a point of the screen.

    It covers the window of the screen ``low..high`` (``-1..1`` is the view itself): a stroke may run past the
    edge of the view, and the model is still under it there. Only the triangles over the window are kept, and
    its cells are about as wide as those triangles.
    """

    def __init__(self, surface, camera, low=(-1.0, -1.0), high=(1.0, 1.0), resolution=256):
        self.surface = surface
        clip = camera.clip(surface.positions)
        self.w = clip[:, 3]
        usable = np.abs(self.w) > 1e-12
        self.ndc = np.where(usable[:, None], clip[:, :3] / np.where(usable, self.w, 1.0)[:, None], 0.0)
        triangles = surface.triangles
        corners = self.ndc[triangles]
        box_low = corners[:, :, :2].min(axis=1)
        box_high = corners[:, :, :2].max(axis=1)
        window_low = np.asarray(low, dtype=np.float64)
        window_high = np.maximum(np.asarray(high, dtype=np.float64), window_low + 1e-9)
        seen = ((self.w[triangles] > 1e-9).all(axis=1) & (box_high >= window_low).all(axis=1)
                & (box_low <= window_high).all(axis=1))
        self.ids = np.nonzero(seen)[0]
        extent = np.maximum(box_high[self.ids] - box_low[self.ids], 1e-12)
        typical = float(np.median(extent)) if len(self.ids) else 1.0
        self.resolution = int(np.clip(np.ceil((window_high - window_low).max() / typical), 4, resolution))
        self.low = window_low
        self.size = np.maximum((window_high - window_low) / self.resolution, 1e-12)
        first, last = _cells(box_low[self.ids], box_high[self.ids], self.low, self.size, self.resolution)
        self.starts, self.entries = _grid(first, last, self.resolution, self.ids)

    def query(self, points, mask=None):
        """What the camera sees at each of ``points`` (n, 2), in the units of the screen window.

        ``(triangle, weights, depth)``: the nearest triangle covering the point -- among those ``mask`` allows,
        when given -- ``-1`` where none does, the weights of the point in it, and how far the point is.
        """
        found = np.full(len(points), -1, dtype=np.int64)
        weights = np.zeros((len(points), 3))
        depth = np.full(len(points), np.inf)
        for begin in range(0, len(points), _BATCH):
            end = begin + _BATCH
            self._query(points[begin:end], mask, found[begin:end], weights[begin:end], depth[begin:end])
        return found, weights, depth

    def _query(self, points, mask, found, weights, depth):
        cells = np.floor((points - self.low) / self.size).astype(np.int64)
        inside = ((cells >= 0) & (cells < self.resolution)).all(axis=1)
        cells = np.clip(cells, 0, self.resolution - 1)
        cells = cells[:, 1] * self.resolution + cells[:, 0]
        query, triangle = _candidates(self.starts, self.entries, cells)
        keep = inside[query]
        if mask is not None:
            keep &= mask[triangle]
        query, triangle = query[keep], triangle[keep]
        corners = self.ndc[self.surface.triangles[triangle]]
        screen, hit = _barycentric(corners[:, :, :2], points[query])
        query, triangle, screen, corners = query[hit], triangle[hit], screen[hit], corners[hit]
        distance = np.einsum("nk,nk->n", screen, corners[:, :, 2])
        best = _first_by_query(query, distance, len(points))
        have = best >= 0
        chosen = best[have]
        found[have] = triangle[chosen]
        depth[have] = distance[chosen]
        vertex_w = self.w[self.surface.triangles[triangle[chosen]]]
        corrected = screen[chosen] / vertex_w
        weights[have] = corrected / corrected.sum(axis=1, keepdims=True)


class UvIndex:
    """The triangles of a surface as the UV layout holds them, findable by a place in it."""

    def __init__(self, surface, mask, reach=0.0, resolution=512):
        self.surface = surface
        self.reach = reach
        self.ids = np.nonzero(mask)[0]
        corners = surface.uvs[surface.triangles[self.ids]]
        low = corners.min(axis=1) - reach
        high = corners.max(axis=1) + reach
        self.origin = low.min(axis=0)
        extent = float((high.max(axis=0) - self.origin).max())
        self.resolution = int(np.clip(np.ceil(extent / max(2.0 * reach, extent / resolution, 1e-12)), 8, resolution))
        self.size = np.maximum((high.max(axis=0) - self.origin) / self.resolution, 1e-12)
        first, last = _cells(low, high, self.origin, self.size, self.resolution)
        self.starts, self.entries = _grid(first, last, self.resolution, self.ids)

    def query(self, points, near=None):
        """The triangle holding each of ``points`` (n, 2) in the UV layout.

        ``(triangle, weights, offset)``: ``-1`` where there is none. Where several triangles hold a point --
        islands lying over one another -- the one whose place in space is nearest ``near`` (n, 3) is taken, the
        first when there is no ``near``. A point held by none but within the index's ``reach`` of some belongs to
        the nearest: its weights are those of the nearest place inside that triangle and ``offset`` is the step in
        the UV layout from there to the point.
        """
        found = np.full(len(points), -1, dtype=np.int64)
        weights = np.zeros((len(points), 3))
        offset = np.zeros((len(points), 2))
        for begin in range(0, len(points), _BATCH):
            end = begin + _BATCH
            self._query(points[begin:end], None if near is None else near[begin:end], found[begin:end],
                        weights[begin:end], offset[begin:end])
        return found, weights, offset

    def holding(self, points):
        """Every triangle of the index that holds one of ``points`` (n, 2) in the UV layout: ``(point, triangle)`` pairs,
        several for a point where islands lie over one another."""
        cells = np.floor((points - self.origin) / self.size).astype(np.int64)
        inside = ((cells >= 0) & (cells < self.resolution)).all(axis=1)
        cells = np.clip(cells, 0, self.resolution - 1)
        cells = cells[:, 1] * self.resolution + cells[:, 0]
        query, triangle = _candidates(self.starts, self.entries, cells)
        keep = inside[query]
        query, triangle = query[keep], triangle[keep]
        _weights, held = _barycentric(self.surface.uvs[self.surface.triangles[triangle]], points[query])
        return query[held], triangle[held]

    def _query(self, points, near, found, weights, offset):
        cells = np.floor((points - self.origin) / self.size).astype(np.int64)
        inside = ((cells >= 0) & (cells < self.resolution)).all(axis=1)
        cells = np.clip(cells, 0, self.resolution - 1)
        cells = cells[:, 1] * self.resolution + cells[:, 0]
        query, triangle = _candidates(self.starts, self.entries, cells)
        keep = inside[query]
        query, triangle = query[keep], triangle[keep]
        corners = self.surface.uvs[self.surface.triangles[triangle]]
        barycentric, held = _barycentric(corners, points[query])
        gap = np.zeros(len(query))
        step = np.zeros((len(query), 2))
        if self.reach > 0.0:
            flat = np.concatenate([corners, np.zeros(corners.shape[:2] + (1,))], axis=2)
            lifted = np.concatenate([points[query], np.zeros((len(query), 1))], axis=1)
            clamped, distance = closest(lifted, flat)
            outside = ~held & (distance <= self.reach)
            barycentric = np.where(outside[:, None], clamped, barycentric)
            gap = np.where(held, 0.0, distance)
            step = points[query] - np.einsum("nk,nkd->nd", clamped, corners)
            candidate = held | outside
        else:
            candidate = held
        query, triangle, barycentric, gap, step, held = (query[candidate], triangle[candidate], barycentric[candidate],
                                                         gap[candidate], step[candidate], held[candidate])
        if near is None:
            tie = np.zeros(len(query))
        else:
            tie = np.linalg.norm(self.surface.point(triangle, barycentric) - near[query], axis=1)
        score = np.where(held, tie, 1e3 + gap)
        best = _first_by_query(query, score, len(points))
        have = best >= 0
        found[have] = triangle[best[have]]
        weights[have] = barycentric[best[have]]
        offset[have] = np.where(held[best[have]][:, None], 0.0, step[best[have]])


class Correspondence:
    """The triangles of one surface found again in another surface that lies in the same places.

    A triangle of ``old`` counterparts the triangle of ``new`` with the same corners in space and, with
    ``same_material``, the same material, however the corners are numbered. That is exact where the nearest
    triangle to a point on a UV seam is not: across the seam the surface continues in a triangle that is
    elsewhere in the layout. Without ``same_material`` a triangle is found again when it was given another material.
    """

    def __init__(self, old, new, tolerance, same_material=True):
        self.old = old
        self.new = new
        self.tolerance = tolerance
        self.same_material = same_material
        self._material = np.array([new.names.index(name) if name in new.names else -1 for name in old.names], dtype=np.int64)
        self._lookup = {tuple(key): index for index, key in enumerate(self._keys(new))}

    def _keys(self, surface):
        names = np.array([hash(name) & 0x7FFFFFFF for name in surface.names] or [0], dtype=np.int64)
        quantized = np.round(surface.positions / self.tolerance).astype(np.int64)
        vertex = (quantized[:, 0] * 73856093) ^ (quantized[:, 1] * 19349663) ^ (quantized[:, 2] * 83492791)
        corners = np.sort(vertex[surface.triangles], axis=1)
        return np.concatenate([corners, names[surface.materials][:, None]], axis=1) if self.same_material else corners

    def carry(self, triangles, weights):
        """For each of ``triangles`` of the old surface with its ``weights``, the counterpart triangle of the new one
        and the weights of the same place in it; ``-1`` where there is none.

        The triangle with the same number is taken when it lies in the same place, which keeps the corners in order
        where two of them coincide; otherwise the one the corners find, with its corners put in order by the least
        total distance.
        """
        found = np.full(len(triangles), -1, dtype=np.int64)
        order = np.tile(np.arange(3), (len(triangles), 1))
        same = triangles < len(self.new.triangles)
        old_corners = self.old.positions[self.old.triangles[triangles]]
        if same.any():
            candidate = np.where(same, triangles, 0)
            new_corners = self.new.positions[self.new.triangles[candidate]]
            matches = (np.abs(old_corners - new_corners).max(axis=(1, 2)) <= self.tolerance) & same
            if self.same_material:
                matches &= self._material[self.old.materials[triangles]] == self.new.materials[candidate]
            found[matches] = candidate[matches]
        keys = self._keys(self.old)[triangles]
        for index in np.nonzero(found < 0)[0]:
            found[index] = self._lookup.get(tuple(keys[index]), -1)
            if found[index] >= 0:
                corners = self.new.positions[self.new.triangles[found[index]]]
                costs = [sum(np.linalg.norm(old_corners[index][corner] - corners[permutation[corner]]) for corner in range(3))
                         for permutation in _PERMUTATIONS]
                order[index] = _PERMUTATIONS[int(np.argmin(np.round(np.array(costs) / self.tolerance)))]
        carried = np.zeros_like(weights)
        have = np.nonzero(found >= 0)[0]
        for corner in range(3):
            carried[have, order[have, corner]] = weights[have, corner]
        return found, carried


class SpaceIndex:
    """The triangles of a surface as they lie in space, findable by a point near them."""

    def __init__(self, surface, mask, reach, resolution=48):
        self.surface = surface
        self.resolution = resolution
        self.reach = reach
        self.ids = np.nonzero(mask)[0]
        corners = surface.positions[surface.triangles[self.ids]]
        low = corners.min(axis=1) - reach
        high = corners.max(axis=1) + reach
        self.origin = low.min(axis=0)
        self.size = np.maximum((high.max(axis=0) - self.origin) / resolution, 1e-12)
        first, last = _cells(low, high, self.origin, self.size, resolution)
        self.starts, self.entries = _grid(first, last, resolution, self.ids)

    def query(self, points):
        """The nearest triangle to each of ``points`` (n, 3) within the index's reach, and the weights of the nearest
        place in it; ``-1`` where there is none."""
        found = np.full(len(points), -1, dtype=np.int64)
        weights = np.zeros((len(points), 3))
        for begin in range(0, len(points), _BATCH):
            end = begin + _BATCH
            self._query(points[begin:end], found[begin:end], weights[begin:end])
        return found, weights

    def crossed(self, starts, ends, others):
        """Whether the segment from each of ``starts`` to its place in ``ends`` (n, 3) passes through a triangle of the index other
        than ``others`` (n), the one it is meant to end on: a boolean per segment. The index's reach has to be at least half of
        the longest segment."""
        blocked = np.zeros(len(starts), dtype=bool)
        for begin in range(0, len(starts), _BATCH):
            end = begin + _BATCH
            blocked[begin:end] = self._crossed(starts[begin:end], ends[begin:end], others[begin:end])
        return blocked

    def _crossed(self, starts, ends, others):
        cells = np.floor((0.5 * (starts + ends) - self.origin) / self.size).astype(np.int64)
        inside = ((cells >= 0) & (cells < self.resolution)).all(axis=1)
        cells = np.clip(cells, 0, self.resolution - 1)
        cells = (cells[:, 2] * self.resolution + cells[:, 1]) * self.resolution + cells[:, 0]
        query, triangle = _candidates(self.starts, self.entries, cells)
        keep = inside[query] & (triangle != others[query])
        query, triangle = query[keep], triangle[keep]
        through = _segment_hits(starts[query], ends[query], self.surface.positions[self.surface.triangles[triangle]])
        blocked = np.zeros(len(starts), dtype=bool)
        blocked[query[through]] = True
        return blocked

    def _query(self, points, found, weights):
        cells = np.floor((points - self.origin) / self.size).astype(np.int64)
        inside = ((cells >= 0) & (cells < self.resolution)).all(axis=1)
        cells = np.clip(cells, 0, self.resolution - 1)
        cells = (cells[:, 2] * self.resolution + cells[:, 1]) * self.resolution + cells[:, 0]
        query, triangle = _candidates(self.starts, self.entries, cells)
        keep = inside[query]
        query, triangle = query[keep], triangle[keep]
        corners = self.surface.positions[self.surface.triangles[triangle]]
        barycentric, distance = closest(points[query], corners)
        near = distance <= self.reach
        query, triangle, barycentric, distance = query[near], triangle[near], barycentric[near], distance[near]
        best = _first_by_query(query, distance, len(points))
        have = best >= 0
        found[have] = triangle[best[have]]
        weights[have] = barycentric[best[have]]
