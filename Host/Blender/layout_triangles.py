# -*- coding: utf-8 -*-
"""The faces a layout change moves, as triangles, in both UV maps that swap.

Each corner of each triangle is read in the render map and in the target map, with
the tangent frame each map gives it: the tangent and bitangent sign MikkTSpace
computes for that map -- Blender's own ``calc_tangents``, the same frames its
renderer interpolates, and the standard Painter computes its own by -- and the
corner's normal. A tangent-space normal laid out again is carried from one frame
to the other texel by texel (``chart_resample``).

Blender's ``calc_tangents`` takes triangles and quads only, while its renderer hands
MikkTSpace a larger face as the triangles it draws it with: such a mesh is read through
a copy built that way -- its triangles and quads as they are, every larger face as its
triangles, each corner keeping its normal and coordinates.

Everything is in the object's own space: the frames a renderer decodes in are the
object's frames turned and scaled with it, so a normal's coordinates in them do not
depend on where the object stands.
"""

from __future__ import annotations

import bpy
import numpy

from . import chart_resample, mesh_publish


class Triangles:
    """The triangles of some materials' faces: their corners' coordinates in the render
    and the target map, ``(triangles, 3, 2)``, in any further maps asked for (``extra``,
    by name), and when asked for, their normals and the MikkTSpace tangent and bitangent
    sign the render and the target map give them."""

    __slots__ = ("render", "target", "normal", "render_tangent", "render_sign",
                 "target_tangent", "target_sign", "extra")

    def __init__(self, parts, extra):
        for name in self.__slots__[:-1]:
            values = parts.get(name)
            setattr(self, name, numpy.concatenate(values) if values else None)
        self.extra = {name: numpy.concatenate(values) for name, values in extra.items()}

    def frames(self, layer):
        """The frames a tangent normal decoded in ``layer`` -- ``""`` for the render map,
        anything else for the target map -- is carried between by the swap."""
        if layer == "":
            return chart_resample.Frames(self.normal, self.render_tangent, self.render_sign,
                                         self.target_tangent, self.target_sign)
        return chart_resample.Frames(self.normal, self.target_tangent, self.target_sign,
                                     self.render_tangent, self.render_sign)


def _coordinates(mesh, uv_map):
    values = numpy.empty(len(mesh.loops) * 2, dtype=numpy.float32)
    mesh.uv_layers[uv_map].uv.foreach_get("vector", values)
    return values.reshape(-1, 2)


def _tangents(mesh, uv_map):
    mesh.calc_tangents(uvmap=uv_map)
    tangent = numpy.empty(len(mesh.loops) * 3, dtype=numpy.float32)
    mesh.loops.foreach_get("tangent", tangent)
    sign = numpy.empty(len(mesh.loops), dtype=numpy.float32)
    mesh.loops.foreach_get("bitangent_sign", sign)
    return tangent.reshape(-1, 3), sign


def _split_copy(mesh, triangle_loops, normals, uv_maps):
    """The mesh with every face of more than four corners split into its triangles, as a
    temporary mesh, and for each of the mesh's triangles the corners it became there."""
    polygon_count = len(mesh.polygons)
    starts = numpy.empty(polygon_count, dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_start", starts)
    totals = numpy.empty(polygon_count, dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    polygon_of_triangle = numpy.empty(len(triangle_loops), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("polygon_index", polygon_of_triangle)
    small = totals <= 4
    kept = numpy.flatnonzero(small)
    kept_loops = (numpy.concatenate([numpy.arange(starts[one], starts[one] + totals[one]) for one in kept])
                  if len(kept) else numpy.empty(0, dtype=numpy.int64))
    split = numpy.flatnonzero(~small[polygon_of_triangle])
    source = numpy.concatenate((kept_loops, triangle_loops[split].reshape(-1)))
    copy_of_loop = numpy.full(len(mesh.loops), -1, dtype=numpy.int64)
    copy_of_loop[kept_loops] = numpy.arange(len(kept_loops))
    corners = copy_of_loop[triangle_loops]
    corners[split] = len(kept_loops) + numpy.arange(len(split) * 3).reshape(-1, 3)
    vertex_of_loop = numpy.empty(len(mesh.loops), dtype=numpy.int64)
    mesh.loops.foreach_get("vertex_index", vertex_of_loop)
    positions = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
    mesh.vertices.foreach_get("co", positions)
    faces = [vertex_of_loop[starts[one]:starts[one] + totals[one]].tolist() for one in kept]
    faces.extend(vertex_of_loop[triangle_loops[one]].tolist() for one in split)
    copy = bpy.data.meshes.new("ruri_bridge_frames")
    copy.from_pydata(positions.reshape(-1, 3).tolist(), [], faces)
    for name in uv_maps:
        values = _coordinates(mesh, name)[source]
        copy.uv_layers.new(name=name).uv.foreach_set("vector", values.reshape(-1))
    copy.normals_split_custom_set(normals[source])
    return copy, corners


def _frames_of(mesh, triangle_loops, normals, uv_maps):
    """Per corner of each of the mesh's triangles, the MikkTSpace tangent and bitangent
    sign every one of ``uv_maps`` gives it, as Blender's renderer computes them."""
    totals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    if (totals <= 4).all():
        return {name: tuple(values[triangle_loops] for values in _tangents(mesh, name)) for name in uv_maps}
    copy, corners = _split_copy(mesh, triangle_loops, normals, uv_maps)
    try:
        return {name: tuple(values[corners] for values in _tangents(copy, name)) for name in uv_maps}
    finally:
        bpy.data.meshes.remove(copy)


def gather(objects, materials, render_names, target_layer, frames, extra_layers=()):
    """The triangles of the faces of ``objects`` that wear one of ``materials``.

    ``render_names`` names each mesh's render map, by the pointer of its mesh. The
    surface is read as Painter is given it (``mesh_publish.surface_only``). With
    ``frames`` the tangent frames come too; ``extra_layers`` are further maps read by
    name. None when no such face is there."""
    wanted = {material.as_pointer() for material in materials}
    parts = {name: [] for name in Triangles.__slots__}
    extra = {name: [] for name in extra_layers}
    with mesh_publish.surface_only(objects):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph.update()
        for object_reference in objects:
            slots = [slot.material is not None and slot.material.as_pointer() in wanted
                     for slot in object_reference.material_slots]
            if not any(slots):
                continue
            evaluated = object_reference.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                count = len(mesh.loop_triangles)
                if not count:
                    continue
                loops = numpy.empty(count * 3, dtype=numpy.int64)
                mesh.loop_triangles.foreach_get("loops", loops)
                loops = loops.reshape(-1, 3)
                indices = numpy.empty(count, dtype=numpy.int32)
                mesh.loop_triangles.foreach_get("material_index", indices)
                chosen = numpy.array(slots)[numpy.minimum(indices, len(slots) - 1)]
                corners = loops[chosen]
                if not len(corners):
                    continue
                maps = (("render", render_names[object_reference.data.as_pointer()]),
                        ("target", target_layer))
                for key, name in maps:
                    parts[key].append(_coordinates(mesh, name)[corners])
                for name in extra:
                    extra[name].append(_coordinates(mesh, name)[corners])
                if frames:
                    normals = numpy.empty(len(mesh.loops) * 3, dtype=numpy.float32)
                    mesh.corner_normals.foreach_get("vector", normals)
                    normals = normals.reshape(-1, 3)
                    parts["normal"].append(normals[corners])
                    found = _frames_of(mesh, loops, normals, [name for _key, name in maps])
                    for key, name in maps:
                        tangent, sign = found[name]
                        parts[key + "_tangent"].append(tangent[chosen])
                        parts[key + "_sign"].append(sign[chosen])
            finally:
                evaluated.to_mesh_clear()
    if not parts["render"]:
        return None
    return Triangles(parts, extra)
