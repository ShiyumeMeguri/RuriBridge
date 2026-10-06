# -*- coding: utf-8 -*-
"""Laying a picture out again in another of its surface's UV layouts.

Every texel centre of the new layout that a triangle covers takes the picture's value
at the same point of the surface: its barycentric weights in the new triangle locate
it in the old one, where the picture is sampled. Values of a label (an id map) are
taken from the nearest texel, never blended; everything else bilinearly, the way a
texture is read. Texels no triangle covers are padded from the nearest covered one,
so filtering across an island border reads the island and not the background.

A tangent-space normal is a direction in the tangent frame the layout defines, and
that frame turns when an island turns, mirrors when it mirrors. At every texel the
normal is decoded in the frame the old layout gives that point of the surface and
encoded in the one the new layout gives it: the corners' normals and MikkTSpace
tangents interpolated across the triangle, the bitangent their cross product times
the interpolated sign -- what a renderer decodes in (``Frames``). ``green`` is +1 for
a picture stored the OpenGL way, -1 for the DirectX way. A normal picture that stays
in its own layout while the frames under it change is carried in place, texel by
texel, with no resampling at all (``reframed``).

Pure numpy, and nothing here knows a mesh, a material or an image datablock.
"""

from __future__ import annotations

import numpy

#: Samples handled per block, which bounds the peak memory of one call.
_BLOCK = 2_000_000
#: How far apart two carried normals of one texel may be before the texel is two
#: things at once.
_SAME_NORMAL = 1e-3


def rasterize(triangles, width, height, every=False):
    """The texel centres the triangles cover: flat texel indices, the triangle covering
    each, and its barycentric weights there. A texel two triangles cover goes to the
    later one, unless ``every`` asks for each of them."""
    triangles = numpy.asarray(triangles, dtype=numpy.float64)
    x = triangles[:, :, 0] * width - 0.5
    y = triangles[:, :, 1] * height - 0.5
    x_low = numpy.clip(numpy.ceil(x.min(axis=1)), 0, width - 1).astype(numpy.int64)
    x_high = numpy.clip(numpy.floor(x.max(axis=1)), 0, width - 1).astype(numpy.int64)
    y_low = numpy.clip(numpy.ceil(y.min(axis=1)), 0, height - 1).astype(numpy.int64)
    y_high = numpy.clip(numpy.floor(y.max(axis=1)), 0, height - 1).astype(numpy.int64)
    box_width = numpy.maximum(x_high - x_low + 1, 0)
    counts = box_width * numpy.maximum(y_high - y_low + 1, 0)
    kept = numpy.flatnonzero(counts > 0)
    texels, owners, weights = [], [], []
    cumulative = numpy.cumsum(counts[kept])
    starts = numpy.searchsorted(cumulative, numpy.arange(0, int(cumulative[-1]) if len(kept) else 0, _BLOCK),
                                side="right")
    bounds = list(starts) + [len(kept)]
    for low, high in zip(bounds[:-1], bounds[1:]):
        block = kept[low:high]
        if not len(block):
            continue
        repeat = numpy.repeat(block, counts[block])
        offset = numpy.arange(len(repeat)) - numpy.repeat(numpy.cumsum(counts[block]) - counts[block],
                                                          counts[block])
        column = x_low[repeat] + offset % box_width[repeat]
        row = y_low[repeat] + offset // box_width[repeat]
        ax, ay = x[repeat, 0], y[repeat, 0]
        e1x, e1y = x[repeat, 1] - ax, y[repeat, 1] - ay
        e2x, e2y = x[repeat, 2] - ax, y[repeat, 2] - ay
        determinant = e1x * e2y - e2x * e1y
        valid = determinant != 0
        inverse = numpy.zeros_like(determinant)
        numpy.divide(1.0, determinant, out=inverse, where=valid)
        px, py = column - ax, row - ay
        w1 = (px * e2y - e2x * py) * inverse
        w2 = (e1x * py - px * e1y) * inverse
        w0 = 1.0 - w1 - w2
        inside = valid & (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        texels.append(row[inside] * width + column[inside])
        owners.append(repeat[inside])
        weights.append(numpy.stack((w0[inside], w1[inside], w2[inside]), axis=1))
    if not texels:
        return (numpy.empty(0, dtype=numpy.int64), numpy.empty(0, dtype=numpy.int64),
                numpy.empty((0, 3)))
    texels = numpy.concatenate(texels)
    owners = numpy.concatenate(owners)
    weights = numpy.concatenate(weights)
    order = numpy.lexsort((owners, texels))
    texels, owners, weights = texels[order], owners[order], weights[order]
    if every:
        return texels, owners, weights
    last = numpy.ones(len(texels), dtype=bool)
    last[:-1] = texels[1:] != texels[:-1]
    return texels[last], owners[last], weights[last]


def _bilinear(picture, uv):
    height, width = picture.shape[:2]
    x = uv[:, 0] * width - 0.5
    y = uv[:, 1] * height - 0.5
    x0 = numpy.floor(x)
    y0 = numpy.floor(y)
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]
    x0 = x0.astype(numpy.int64)
    y0 = y0.astype(numpy.int64)
    x1 = numpy.clip(x0 + 1, 0, width - 1)
    y1 = numpy.clip(y0 + 1, 0, height - 1)
    x0 = numpy.clip(x0, 0, width - 1)
    y0 = numpy.clip(y0, 0, height - 1)
    top = picture[y0, x0] * (1.0 - fx) + picture[y0, x1] * fx
    bottom = picture[y1, x0] * (1.0 - fx) + picture[y1, x1] * fx
    return top * (1.0 - fy) + bottom * fy


def _nearest(picture, uv):
    height, width = picture.shape[:2]
    column = numpy.clip(numpy.floor(uv[:, 0] * width).astype(numpy.int64), 0, width - 1)
    row = numpy.clip(numpy.floor(uv[:, 1] * height).astype(numpy.int64), 0, height - 1)
    return picture[row, column]


class Frames:
    """Per triangle corner, the frames a tangent-space normal is decoded in before a
    layout change and encoded in after it: the corner ``normal`` and, per layout, the
    MikkTSpace ``tangent`` and bitangent ``sign``. Arrays of (triangles, 3, 3) and
    (triangles, 3)."""

    __slots__ = ("normal", "before_tangent", "before_sign", "after_tangent", "after_sign")

    def __init__(self, normal, before_tangent, before_sign, after_tangent, after_sign):
        self.normal = numpy.asarray(normal, dtype=numpy.float64)
        self.before_tangent = numpy.asarray(before_tangent, dtype=numpy.float64)
        self.before_sign = numpy.asarray(before_sign, dtype=numpy.float64)
        self.after_tangent = numpy.asarray(after_tangent, dtype=numpy.float64)
        self.after_sign = numpy.asarray(after_sign, dtype=numpy.float64)

    def turning(self):
        """Per triangle, whether the frame at any of its corners changes."""
        return ((numpy.abs(self.after_tangent - self.before_tangent).max(axis=(1, 2)) > 1e-6)
                | (self.after_sign != self.before_sign).any(axis=1))

    def change(self):
        """Whether any corner's frame changes."""
        return bool(self.turning().any())


def _unit(vectors):
    return vectors / numpy.maximum(numpy.linalg.norm(vectors, axis=1, keepdims=True), 1e-30)


def _carried_block(vectors, weights, owners, frames):
    def at(values):
        return numpy.einsum("kc,kc...->k...", weights, values[owners])

    normal = _unit(at(frames.normal))
    before = _unit(at(frames.before_tangent))
    after = _unit(at(frames.after_tangent))
    before_bitangent = at(frames.before_sign)[:, None] * numpy.cross(normal, before)
    after_bitangent = at(frames.after_sign)[:, None] * numpy.cross(normal, after)
    surface = (vectors[:, 0:1] * before + vectors[:, 1:2] * before_bitangent
               + vectors[:, 2:3] * normal)
    across = (numpy.cross(after_bitangent, normal), numpy.cross(normal, after),
              numpy.cross(after, after_bitangent))
    determinant = numpy.sum(after * across[0], axis=1)
    solvable = numpy.abs(determinant) > 1e-12
    carried = vectors.copy()
    for axis, reciprocal in enumerate(across):
        carried[solvable, axis] = (numpy.sum(surface[solvable] * reciprocal[solvable], axis=1)
                                   / determinant[solvable])
    length = numpy.linalg.norm(vectors, axis=1, keepdims=True)
    return _unit(carried) * length


def _carried(vectors, weights, owners, frames):
    """Tangent-space normals decoded in the frames before, encoded in the frames after,
    at the points ``weights`` place in the triangles ``owners``."""
    out = numpy.empty_like(vectors)
    for start in range(0, len(vectors), _BLOCK):
        span = slice(start, start + _BLOCK)
        out[span] = _carried_block(vectors[span], weights[span], owners[span], frames)
    return out


def _carried_lanes(values, weights, owners, frames, green, green_after=None):
    """Stored normal lanes -- the first three, premultiplied by a fourth when there is
    one -- carried between the frames: read with ``green``, written with ``green_after``
    (the same unless given)."""
    coverage = values[:, 3:4] if values.shape[1] > 3 else numpy.ones((len(values), 1))
    covered = coverage[:, 0] > 0.0
    vectors = numpy.zeros((len(values), 3))
    vectors[covered] = values[covered, :3] / coverage[covered] * 2.0 - 1.0
    vectors[:, 1] *= green
    moved = _carried(vectors[covered], weights[covered], owners[covered], frames)
    moved[:, 1] *= green if green_after is None else green_after
    out = values.copy()
    out[covered, :3] = (moved * 0.5 + 0.5) * coverage[covered]
    return out


def nearest(covered):
    """For every texel, the row and column of the nearest covered one (jump flooding
    over the whole picture)."""
    height, width = covered.shape
    rows = numpy.broadcast_to(numpy.arange(height)[:, None], (height, width))
    columns = numpy.broadcast_to(numpy.arange(width)[None, :], (height, width))
    seed_row = numpy.where(covered, rows, -1)
    seed_column = numpy.where(covered, columns, -1)
    distance = numpy.where(covered, 0, numpy.iinfo(numpy.int64).max)
    step = 1 << int(max(height, width) - 1).bit_length()
    while step >= 1:
        for delta_row in (-step, 0, step):
            for delta_column in (-step, 0, step):
                if not delta_row and not delta_column:
                    continue
                shifted_row = numpy.full_like(seed_row, -1)
                shifted_column = numpy.full_like(seed_column, -1)
                target = (slice(max(0, -delta_row), height - max(0, delta_row)),
                          slice(max(0, -delta_column), width - max(0, delta_column)))
                source = (slice(max(0, delta_row), height - max(0, -delta_row)),
                          slice(max(0, delta_column), width - max(0, -delta_column)))
                shifted_row[target] = seed_row[source]
                shifted_column[target] = seed_column[source]
                candidate = ((shifted_row - rows) ** 2 + (shifted_column - columns) ** 2)
                better = (shifted_row >= 0) & (candidate < distance)
                seed_row = numpy.where(better, shifted_row, seed_row)
                seed_column = numpy.where(better, shifted_column, seed_column)
                distance = numpy.where(better, candidate, distance)
        step >>= 1
    return seed_row, seed_column


def pad(picture, covered):
    """Fill every texel no triangle covers with the value of the nearest one that is."""
    if covered.all() or not covered.any():
        return picture
    seed_row, seed_column = nearest(covered)
    filled = picture.copy()
    uncovered = ~covered
    filled[uncovered] = picture[seed_row[uncovered], seed_column[uncovered]]
    return filled


def relaid(picture, old_triangles, new_triangles, kind, frames=None, green=1.0, size=None):
    """``picture`` (height, width, channels), laid out in ``old_triangles``, laid out
    again in ``new_triangles``; ``size`` is the (width, height) of the result, the
    picture's own by default. ``kind`` is ``value``, ``label`` or ``tangent``; a tangent
    picture needs the ``frames`` of both layouts and its ``green``. Its first three lanes
    are the normal; a fourth, when there is one, is a coverage the normal is
    premultiplied by, as Painter stores one."""
    picture = numpy.asarray(picture, dtype=numpy.float64)
    height, width = picture.shape[:2]
    out_width, out_height = size if size is not None else (width, height)
    texels, owners, weights = rasterize(new_triangles, out_width, out_height)
    old_triangles = numpy.asarray(old_triangles, dtype=numpy.float64)
    old_uv = numpy.einsum("kc,kcd->kd", weights, old_triangles[owners])
    values = (_nearest if kind == "label" else _bilinear)(picture, old_uv)
    if kind == "tangent":
        values = _carried_lanes(values, weights, owners, frames, green)
    out = numpy.zeros((out_height * out_width, picture.shape[2]))
    out[texels] = values
    covered = numpy.zeros(out_height * out_width, dtype=bool)
    covered[texels] = True
    return pad(out.reshape(out_height, out_width, -1), covered.reshape(out_height, out_width))


def reframed(picture, triangles, frames, green=1.0, green_after=None):
    """A tangent-space normal ``picture`` laid out in ``triangles``, left in that layout
    with every texel's normal carried from the frames before to the frames after -- no
    texel moves, so nothing is resampled. Its green is read with ``green`` and written
    with ``green_after``, for a picture that will be taken another way than it was. A
    texel no triangle covers is carried with the frames of the nearest one that is, which
    keeps an island's border filtering right. Raises ``ValueError`` when triangles
    overlapping in the layout would need one texel to hold two different normals."""
    picture = numpy.asarray(picture, dtype=numpy.float64)
    height, width = picture.shape[:2]
    flat = picture.reshape(-1, picture.shape[2])
    texels, owners, weights = rasterize(triangles, width, height, every=True)
    if not len(texels):
        return picture
    carried = _carried_lanes(flat[texels], weights, owners, frames, green, green_after)
    first = numpy.ones(len(texels), dtype=bool)
    first[1:] = texels[1:] != texels[:-1]
    group = numpy.cumsum(first) - 1
    leader = carried[first][group]
    disagreeing = numpy.abs(carried[:, :3] - leader[:, :3]).max(axis=1) > _SAME_NORMAL
    if disagreeing.any():
        raise ValueError("{0} texel(s) are shared by overlapping islands whose frames change "
                         "differently".format(int(numpy.unique(texels[disagreeing]).size)))
    texels, owners, weights = texels[first], owners[first], weights[first]
    out = flat.copy()
    out[texels] = carried[first]
    covered = numpy.zeros(height * width, dtype=bool)
    covered[texels] = True
    uncovered = numpy.flatnonzero(~covered)
    if len(uncovered):
        owner_of = numpy.full(height * width, -1, dtype=numpy.int64)
        owner_of[texels] = owners
        weights_of = numpy.zeros((height * width, 3))
        weights_of[texels] = weights
        seed_row, seed_column = nearest(covered.reshape(height, width))
        seed = (seed_row * width + seed_column).reshape(-1)[uncovered]
        out[uncovered] = _carried_lanes(flat[uncovered], weights_of[seed], owner_of[seed], frames, green,
                                        green_after)
    return out.reshape(picture.shape)
