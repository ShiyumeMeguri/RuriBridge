# -*- coding: utf-8 -*-
"""The surfaces of canvases, put into the file of the surface a project is to have.

Painter takes a surface from one file, and keeps the Texture Sets whose material names the file holds: so a canvas, which is
a Texture Set that no modelling application paints into, stays in a project only while every surface sent in carries
its surface along. The surface of a canvas is read out of the project's own scene, where Painter kept it
as the meshes the canvas's material is on, and written back in the frame of the file: Painter put a surface it was given
into the scene through one uniform scale and one offset (``scene.import_transform``), and keeps them across every
reload that preserves strokes, so a position of the scene goes back into a file through the inverse of the two.

Each mesh of a canvas goes in as it is -- every vertex with its normal and its coordinates in each UV set, every triangle as
it was numbered -- under the name it had, which geometry masks of the canvas's layers are written against.
"""

from __future__ import annotations

import numpy

from .. import fbx, scene
from .canvas import CanvasError


#: How far, as a share of the surface's diagonal, two positions of a corner may lie apart and be the same, and how far two coordinates of a UV
#: layout may.
SAME_PLACE = 1e-5
SAME_COORDINATE = 1e-6


def _array(values, columns):
    return numpy.frombuffer(values, dtype=numpy.float32).reshape(-1, columns)


#: How many corners of one surface may have no equal in the other, because the rounding of a coordinate fell on its other side, before the
#: surfaces count as different without further look.
LEFTOVER_LIMIT = 8192
#: How many corners are matched to the leftovers of the other surface at a time.
_CHUNK = 256


def _corner_keys(positions, sets, step):
    """The corners of a surface as a set of rows, each where the corner lies and its coordinates in each UV set, rounded."""
    rows = numpy.concatenate([numpy.round(positions / step)] + [numpy.round(each / SAME_COORDINATE) for each in sets], axis=1).astype(numpy.int32)
    return set(map(tuple, rows.tolist()))


def _same_keys(first, second):
    """Whether two sets of corner rows are one, but for rows that differ by one step in some coordinate: a position the float arithmetic
    of two ways of getting it left either side of a rounding."""
    only_first, only_second = first - second, second - first
    if not only_first and not only_second:
        return True
    if not only_first or not only_second or max(len(only_first), len(only_second)) > LEFTOVER_LIMIT:
        return False
    one, other = numpy.array(sorted(only_first), dtype=numpy.int32), numpy.array(sorted(only_second), dtype=numpy.int32)
    for each, against in ((one, other), (other, one)):
        for start in range(0, len(each), _CHUNK):
            near = numpy.abs(each[start:start + _CHUNK, None, :] - against[None, :, :]).max(axis=2).min(axis=1) <= 1
            if not near.all():
                return False
    return True


def unchanged(surface, found, material):
    """Whether the surface ``surface`` (the parts of an FBX) paints into the material called ``material`` with the very meshes
    -- names, corners, coordinates in every UV set -- the scene ``found`` holds on it: paint replayed on one is replayed on the other.
    Corners are compared to the grain of ``SAME_PLACE``; where one falls on the other side of a rounding the surface counts as changed,
    which costs a canvas and no more."""
    transform = scene.import_transform(found)
    meshes = [mesh for mesh in found.meshes if found.materials[mesh.material] == material]
    parts = [(part, index) for part in surface for index, name in enumerate(part.texture_sets) if name == material]
    if transform is None or not meshes or not parts:
        return False
    if sorted(mesh.name for mesh in meshes) != sorted(part.name for part, _index in parts):
        return False
    count = {len(mesh.uvs) for mesh in meshes}
    if len(count) != 1 or count != {len(part.uv_sets) for part, _index in parts}:
        return False
    scale, offset = transform
    corners = [numpy.frombuffer(mesh.indices, dtype=numpy.uint32) for mesh in meshes]
    old_positions = numpy.concatenate([((_array(mesh.positions, 3).astype(numpy.float64) - numpy.array(offset)) / scale)[each]
                                       for mesh, each in zip(meshes, corners)])
    step = SAME_PLACE * float(numpy.linalg.norm(old_positions.max(axis=0) - old_positions.min(axis=0)))
    if step <= 0.0:
        return False
    old_sets = [numpy.concatenate([_array(mesh.uvs[number][1], 3)[:, :2][each] for mesh, each in zip(meshes, corners)]).astype(numpy.float64)
                for number in range(count.pop())]
    new_positions, new_sets, triangles = [], [[] for _number in old_sets], 0
    for part, index in parts:
        chosen = numpy.repeat(part.polygon_texture_set == index, part.polygon_totals)
        new_positions.append(part.positions.astype(numpy.float64)[part.corner_vertex][chosen])
        for number, coordinates in enumerate(part.uv_sets):
            new_sets[number].append(coordinates[chosen].astype(numpy.float64))
        triangles += int(numpy.maximum(part.polygon_totals[part.polygon_texture_set == index] - 2, 0).sum())
    if triangles != sum(len(each) // 3 for each in corners):
        return False
    return _same_keys(_corner_keys(old_positions, old_sets, step),
                      _corner_keys(numpy.concatenate(new_positions), [numpy.concatenate(each) for each in new_sets], step))


def part(mesh, name, scale, offset, uv_set_count):
    """The part of an FBX that puts ``mesh`` of the scene back under the material called ``name``, with ``uv_set_count`` UV sets:
    the ones the mesh has, and for those it lacks a copy of its first."""
    if mesh.normals is None or not mesh.uvs:
        raise CanvasError("the mesh {0} of the project's scene has no normals or no UV set".format(mesh.name))
    positions = ((_array(mesh.positions, 3).astype(numpy.float64) - numpy.array(offset)) / scale).astype(numpy.float32)
    corners = numpy.frombuffer(mesh.indices, dtype=numpy.uint32).astype(numpy.int64)
    sets = [_array(values, 3)[:, :2][corners] for _components, values in mesh.uvs]
    if len(sets) > uv_set_count:
        raise CanvasError("the mesh {0} has {1} UV sets and the surface that goes in {2}: a layer of a canvas may read one of those it would "
                          "lose".format(mesh.name, len(sets), uv_set_count))
    sets += [sets[0]] * (uv_set_count - len(sets))
    return fbx.Part(mesh.name, positions, corners, numpy.full(len(corners) // 3, 3, dtype=numpy.int64), _array(mesh.normals, 3)[corners], sets,
                    numpy.zeros(len(corners) // 3, dtype=numpy.int64), [name])


def compose(surface, uv_names, found, placements):
    """``(parts, uv_names)`` of the file that holds the surface ``surface`` (the parts of its FBX, read by ``fbx.read``) and, after it,
    the meshes of the scene ``found`` that are on the materials ``placements`` names -- ``{material of the scene: name in the file}``.

    Raises ``CanvasError`` for a material the scene has no mesh on."""
    transform = scene.import_transform(found)
    if transform is None:
        raise CanvasError("the project's scene says nothing of the transform its surface was imported through")
    scale, offset = transform
    parts = list(surface)
    for material, name in placements.items():
        meshes = [mesh for mesh in found.meshes if found.materials[mesh.material] == material]
        if not meshes:
            raise CanvasError("the project's scene has no mesh on {0}".format(material))
        parts.extend(part(mesh, name, scale, offset, len(uv_names)) for mesh in meshes)
    return parts, uv_names
