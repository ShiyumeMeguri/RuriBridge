# -*- coding: utf-8 -*-
"""Turning Blender's evaluated meshes into a GLB inside the shared arena.

The gather is one ``foreach_get`` per attribute straight out of Blender's C
arrays; no Python-level loop over corners ever runs. What comes back lives on the
corner domain, because split normals and UV seams are corner facts, so corners
are deduplicated into the minimal vertex set before anything is written.

Deduplication never materialises the values it compares. Two corners share a
position exactly when they share a vertex index, so the key is that index plus
the corner-domain attributes as they already sit in Blender's buffers -- compared
as raw 32-bit lanes, which is why it is one vectorised call rather than a
tolerance search. The result is an ordering, and every attribute is then gathered
through that ordering *directly into the mapped page*: one pass, one write, no
intermediate array.

Point-domain data stays off the key entirely: it is already implied by the vertex
index, and it is written by composing the two index arrays instead of expanding
it to the corner domain first.
"""

from __future__ import annotations

import numpy

from ruri_bridge import record as record_module
from ruri_bridge.glb import (AttributeLayout, GlbWriter, MeshLayout, PrimitiveLayout,
                             SceneLayout, SEMANTIC_COLOR_0, SEMANTIC_NORMAL,
                             SEMANTIC_POSITION, TEXCOORD_PREFIX)
from ruri_bridge.log import logger

LOG = logger("blender.mesh")

MAXIMUM_TEXCOORD_SETS = 8
NEGATIVE_ZERO_PATTERN = numpy.uint32(0x80000000)


class AttributeSource:
    """How one attribute reaches the page: take(values, order) with no copy first."""

    __slots__ = ("semantic", "values", "order")

    def __init__(self, semantic, values, order):
        self.semantic = semantic
        self.values = values
        self.order = order

    @property
    def component_count(self):
        return self.values.shape[1]


class ObjectData:
    """One source object, resolved down to index arrays and its own buffers."""

    __slots__ = ("name", "node_matrix", "vertex_count", "sources", "primitives",
                 "material_rows")

    def __init__(self, name, node_matrix, vertex_count, sources, primitives, material_rows):
        self.name = name
        self.node_matrix = node_matrix
        self.vertex_count = vertex_count
        self.sources = sources
        self.primitives = primitives
        self.material_rows = material_rows


def _column_major(matrix):
    return [matrix[row][column] for column in range(4) for row in range(4)]


def _material_row(material):
    """Whatever the producing side calls a material, carried verbatim.

    Custom properties are how a Blender-side generator stores a material data
    row, so they travel as they are. The bridge does not read them.
    """
    row = {"name": material.name}
    properties = {}
    for key in material.keys():
        value = material[key]
        try:
            properties[key] = value if isinstance(
                value, (int, float, str, bool)) else list(value)
        except TypeError:
            properties[key] = repr(value)
    if properties:
        row["properties"] = properties
    if material.use_nodes and material.node_tree is not None:
        groups = sorted({node.node_tree.name for node in material.node_tree.nodes
                         if node.type == "GROUP" and node.node_tree is not None})
        if groups:
            row["node_groups"] = groups
    return row


def _slot_material_names(object_reference):
    names = []
    for index, slot in enumerate(object_reference.material_slots):
        if slot.material is None:
            names.append("{0}_slot{1}".format(object_reference.name, index))
        else:
            names.append(slot.material.name)
    if not names:
        names.append(object_reference.name)
    return names


def _read_colors(mesh, corner_count, vertex_count):
    """The active colour attribute, with the domain it lives on."""
    layer = mesh.color_attributes.active_color
    if layer is None:
        return None, None
    expected = vertex_count if layer.domain == "POINT" else corner_count
    if len(layer.data) != expected:
        LOG.warning("colour attribute %r has %d entries for %d on domain %s; skipped",
                    layer.name, len(layer.data), expected, layer.domain)
        return None, None
    values = numpy.empty(expected * 4, dtype=numpy.float32)
    layer.data.foreach_get("color", values)
    return values.reshape(-1, 4), layer.domain


def _deduplicate(key_columns):
    """Order the minimal vertex set and map every corner onto it."""
    total_width = sum(column.shape[1] for column in key_columns)
    key = numpy.empty((key_columns[0].shape[0], total_width), dtype=numpy.uint32)
    cursor = 0
    for column in key_columns:
        width = column.shape[1]
        key[:, cursor:cursor + width] = column.view(numpy.uint32)
        cursor += width
    key[key == NEGATIVE_ZERO_PATTERN] = 0
    void_type = numpy.dtype((numpy.void, key.dtype.itemsize * total_width))
    rows = numpy.ascontiguousarray(key).view(void_type).reshape(key.shape[0])
    _values, order, inverse = numpy.unique(rows, return_index=True, return_inverse=True)
    return order, inverse.reshape(-1)


def gather_object(object_reference, depsgraph, include_colors=True):
    """Read one evaluated object into index arrays over its own buffers."""
    evaluated = object_reference.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    if mesh is None:
        return None
    try:
        mesh.calc_loop_triangles()
        triangle_count = len(mesh.loop_triangles)
        if triangle_count == 0:
            return None
        corner_count = len(mesh.loops)
        vertex_count = len(mesh.vertices)

        triangle_corners = numpy.empty(triangle_count * 3, dtype=numpy.int32)
        mesh.loop_triangles.foreach_get("loops", triangle_corners)
        triangle_material = numpy.empty(triangle_count, dtype=numpy.int32)
        mesh.loop_triangles.foreach_get("material_index", triangle_material)

        positions = numpy.empty(vertex_count * 3, dtype=numpy.float32)
        mesh.attributes["position"].data.foreach_get("vector", positions)
        positions = positions.reshape(-1, 3)

        corner_vertex = numpy.empty(corner_count, dtype=numpy.int32)
        mesh.loops.foreach_get("vertex_index", corner_vertex)

        corner_normal = numpy.empty(corner_count * 3, dtype=numpy.float32)
        mesh.corner_normals.foreach_get("vector", corner_normal)
        corner_normal = corner_normal.reshape(-1, 3)

        texcoords = []
        for layer in list(mesh.uv_layers)[:MAXIMUM_TEXCOORD_SETS]:
            values = numpy.empty(corner_count * 2, dtype=numpy.float32)
            layer.uv.foreach_get("vector", values)
            values = values.reshape(-1, 2)
            values[:, 1] = 1.0 - values[:, 1]
            texcoords.append(values)
        if not texcoords:
            LOG.warning("%s has no UV map; Painter will have to unwrap it",
                        object_reference.name)

        colors, color_domain = (_read_colors(mesh, corner_count, vertex_count)
                                if include_colors else (None, None))

        key_columns = [corner_vertex.reshape(-1, 1), corner_normal]
        key_columns.extend(texcoords)
        if colors is not None and color_domain == "CORNER":
            key_columns.append(colors)
        order, inverse = _deduplicate(key_columns)
        vertex_order = corner_vertex[order]

        sources = [AttributeSource(SEMANTIC_POSITION, positions, vertex_order),
                   AttributeSource(SEMANTIC_NORMAL, corner_normal, order)]
        for index, values in enumerate(texcoords):
            sources.append(AttributeSource(TEXCOORD_PREFIX + str(index), values, order))
        if colors is not None:
            sources.append(AttributeSource(
                SEMANTIC_COLOR_0, colors,
                vertex_order if color_domain == "POINT" else order))

        material_names = _slot_material_names(object_reference)
        triangle_by_material = triangle_corners.reshape(-1, 3)
        primitives = []
        for material_index in sorted(set(int(value) for value in triangle_material)):
            rows = triangle_by_material[
                numpy.flatnonzero(triangle_material == material_index)].reshape(-1)
            name = (material_names[material_index] if material_index < len(material_names)
                    else material_names[-1])
            primitives.append((name, inverse[rows]))

        rows = [_material_row(slot.material) for slot in object_reference.material_slots
                if slot.material is not None]
        return ObjectData(object_reference.name,
                          _column_major(object_reference.matrix_world),
                          int(order.shape[0]), sources, primitives, rows)
    finally:
        evaluated.to_mesh_clear()


def build_layout(objects):
    meshes = []
    for entry in objects:
        attributes = [AttributeLayout(source.semantic, source.component_count)
                      for source in entry.sources]
        primitives = [PrimitiveLayout(name, int(indices.shape[0]))
                      for name, indices in entry.primitives]
        meshes.append(MeshLayout(entry.name, entry.node_matrix, entry.vertex_count,
                                 attributes, primitives))
    return SceneLayout(meshes)


def _fill_attribute(window, source, vertex_count):
    """Gather one attribute straight into the mapped page. Returns its extents."""
    target = numpy.frombuffer(window, dtype=numpy.float32).reshape(
        vertex_count, source.component_count)
    numpy.take(source.values, source.order, axis=0, out=target)
    return target.min(axis=0), target.max(axis=0)


def _fill_indices(window, indices):
    target = numpy.frombuffer(window, dtype=numpy.uint32)
    numpy.copyto(target, indices, casting="unsafe")


def write_glb(arena, path, objects):
    """Lay the file out from counts, then fill its binary chunk in one pass."""
    layout = build_layout(objects)
    scene_description = []
    with GlbWriter.create(arena, path, layout) as writer:
        for mesh_index, entry in enumerate(objects):
            for source in entry.sources:
                with writer.attribute_window(mesh_index, source.semantic) as window:
                    minimum, maximum = _fill_attribute(window, source, entry.vertex_count)
                if source.semantic == SEMANTIC_POSITION:
                    writer.set_bounds(mesh_index, minimum, maximum)
                    bounds = (minimum, maximum)
            primitive_description = []
            for primitive_index, (name, indices) in enumerate(entry.primitives):
                with writer.index_window(mesh_index, primitive_index) as window:
                    _fill_indices(window, indices)
                primitive_description.append({
                    "material": name,
                    "triangle_count": int(indices.shape[0] // 3),
                })
            scene_description.append({
                "name": entry.name,
                "node_matrix": [float(value) for value in entry.node_matrix],
                "vertex_count": entry.vertex_count,
                "semantics": [source.semantic for source in entry.sources],
                "bounds_min": [float(value) for value in bounds[0]],
                "bounds_max": [float(value) for value in bounds[1]],
                "primitives": primitive_description,
            })
    return scene_description


def publish(arena, publisher, objects_to_send, depsgraph, intent, unit_scale,
            include_colors=True):
    """Gather, write and publish one mesh generation. Returns the generation."""
    gathered = []
    for object_reference in objects_to_send:
        entry = gather_object(object_reference, depsgraph, include_colors)
        if entry is None:
            LOG.warning("%s evaluated to no triangles and was skipped", object_reference.name)
            continue
        gathered.append(entry)
    if not gathered:
        raise RuntimeError("nothing to publish: no object in scope evaluated to triangles")

    with publisher.staging() as staging:
        scene_description = write_glb(
            arena, staging.path(record_module.SCENE_FILE_NAME), gathered)
        material_rows = []
        seen = set()
        for entry in gathered:
            for row in entry.material_rows:
                if row["name"] in seen:
                    continue
                seen.add(row["name"])
                material_rows.append(row)
        return staging.publish(record_module.mesh(
            source="blender",
            intent=intent,
            scene=scene_description,
            materials=material_rows,
            unit_scale=unit_scale,
            up_axis="Z"))
