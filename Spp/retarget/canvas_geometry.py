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


def _array(values, columns):
    return numpy.frombuffer(values, dtype=numpy.float32).reshape(-1, columns)


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
