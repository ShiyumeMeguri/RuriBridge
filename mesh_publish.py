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

import uuid

import bpy
import numpy

from ruri_bridge import record as record_module
from ruri_bridge.glb import (AttributeLayout, GlbWriter, MeshLayout, PrimitiveLayout,
                             SceneLayout, SEMANTIC_COLOR_0, SEMANTIC_NORMAL,
                             SEMANTIC_POSITION, TEXCOORD_PREFIX)
from ruri_bridge.log import logger

LOG = logger("blender.mesh")

MAXIMUM_TEXCOORD_SETS = 8
IDENTITY_PROPERTY = "ruri_bridge_identity"
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
                 "identity")

    def __init__(self, name, node_matrix, vertex_count, sources, primitives, identity):
        self.name = name
        self.node_matrix = node_matrix
        self.vertex_count = vertex_count
        self.sources = sources
        self.primitives = primitives
        self.identity = identity


def _column_major(matrix):
    return [matrix[row][column] for column in range(4) for row in range(4)]


def mint_identity(datablock):
    """The name the other side knows this by, and whether it was just settled on.

    It is the datablock's own name, taken once and then held still. Painter
    matches a Texture Set to the mesh material it came from by name, so what
    travels has to stay put across a rename here -- but it has to stay a *name*,
    because it is what somebody reads in Painter's Texture Set list. An
    identifier minted out of nothing satisfies the first half and fails the
    second: the list fills with hex.

    Whether it was just settled on is worth carrying. It only becomes durable
    when the file holding it is saved, so a caller that has just taken one is
    admitting it has no memory of previous sessions, and the receiving side can
    adopt rather than read it as proof that this is somebody else's work.
    """
    existing = datablock.get(IDENTITY_PROPERTY)
    if existing:
        return existing, False
    datablock[IDENTITY_PROPERTY] = datablock.name
    return datablock.name, True


def mint_scene_identity(scene):
    """The scene's identity, which nobody ever reads, so it is minted.

    Unlike a material, this never crosses into a name anyone sees: it lives in
    the Painter project's metadata and answers one question, whether this project
    belongs to this scene. Names cannot answer that -- two files both called
    Scene are the common case, and binding them together would be worse than
    having no binding at all.
    """
    existing = scene.get(IDENTITY_PROPERTY)
    if existing:
        return existing, False
    minted = uuid.uuid4().hex
    scene[IDENTITY_PROPERTY] = minted
    return minted, True


def identity_of(datablock):
    """The name Painter knows this by, which renaming here cannot move."""
    return mint_identity(datablock)[0]


def vertex_count_of(objects):
    """How big this scene is, as one number both sides can compare.

    Cheap enough to take on every send and specific enough to answer the only
    question a name match needs answered: is the project on the other side built
    from this model at all. Read from the stored meshes rather than the evaluated
    ones so the number does not move when a modifier is toggled.
    """
    total = 0
    for object_reference in objects:
        vertices = getattr(object_reference.data, "vertices", None)
        if vertices is not None:
            total += len(vertices)
    return total


def adopt_identities(objects):
    """Give every material its identity now, and name the ones that had none.

    An identity lives in the .blend, so a file that has not been saved since the
    bridge first touched it takes a fresh set every session -- and Painter, which
    matches Texture Sets by exactly that, then reads every material as new and
    builds a second set of Texture Sets beside the painted ones. Doing it in one
    pass before anything is written is what makes that visible while it can still
    be prevented, instead of after the paint is stranded.

    Two materials can want the same identity, because an identity is a name that
    stopped moving while the names around it did not: rename A to B and call the
    next material A, and both now answer to A. Painter would read one material and
    merge the paint, so the collision is broken here, the second one taking the
    next free suffix the way Blender numbers its own duplicates.
    """
    fresh = set()
    claimed = {}
    for object_reference in objects:
        for slot in object_reference.material_slots:
            material = slot.material
            if material is None:
                continue
            identity, is_new = mint_identity(material)
            other = claimed.get(identity)
            if other is not None and other is not material:
                identity = _next_free_identity(identity, claimed)
                LOG.warning("%r and %r both answer to %r over the bridge; %r takes %r",
                            other.name, material.name, material[IDENTITY_PROPERTY],
                            material.name, identity)
                material[IDENTITY_PROPERTY] = identity
                is_new = True
            claimed[identity] = material
            if is_new:
                fresh.add(material.name)
    return fresh


def _next_free_identity(identity, claimed):
    suffix = 1
    while "{0}.{1:03d}".format(identity, suffix) in claimed:
        suffix += 1
    return "{0}.{1:03d}".format(identity, suffix)


def _material_row(material, fresh=()):
    """Whatever the producing side calls a material, carried verbatim.

    Custom properties are how a Blender-side generator stores a material data
    row, so they travel as they are. The bridge does not read them.
    """
    row = {"identity": identity_of(material), "name": material.name,
           "identity_is_new": material.name in fresh}
    properties = {}
    for key in material.keys():
        if key == IDENTITY_PROPERTY:
            continue
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


def collect_material_rows(objects, fresh=()):
    """Every distinct material row across these objects, first use wins.

    One collection point, because a mesh publish and a shader push must offer the
    other side the same idea of what a material is.
    """
    rows = []
    seen = set()
    for object_reference in objects:
        for slot in object_reference.material_slots:
            if slot.material is None or slot.material.name in seen:
                continue
            seen.add(slot.material.name)
            rows.append(_material_row(slot.material, fresh))
    return rows


def ensure_materials(objects):
    """Give every object a real material before its name crosses the bridge.

    Painter names a Texture Set after the material it came from, and the return
    trip finds its way home by that same name. An object with no material has no
    name to give, so the old code invented one from the object -- and the paint
    then came back addressed to a material that had never existed, landing in
    image datablocks nothing referenced. Nothing failed; it simply never showed.

    Creating the material here is the smallest thing that makes the round trip
    closed by construction rather than by the user having remembered.
    """
    created = []
    for object_reference in objects:
        slots = list(object_reference.material_slots)
        if not slots:
            material = bpy.data.materials.new(object_reference.name)
            material.use_nodes = True
            object_reference.data.materials.append(material)
            created.append(material.name)
            continue
        for index, slot in enumerate(slots):
            if slot.material is not None:
                continue
            material = bpy.data.materials.new(object_reference.name)
            material.use_nodes = True
            object_reference.data.materials[index] = material
            created.append(material.name)
    if created:
        LOG.info("created %d material(s) so the paint has somewhere to come back to: %s",
                 len(created), ", ".join(created))
    return created


def _slot_material_identities(object_reference):
    identities = []
    for slot in object_reference.material_slots:
        if slot.material is None:
            raise RuntimeError(
                "{0} still has an empty material slot; ensure_materials should have "
                "filled it before the identities were read".format(object_reference.name))
        identities.append(identity_of(slot.material))
    return identities


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

        material_identities = _slot_material_identities(object_reference)
        triangle_by_material = triangle_corners.reshape(-1, 3)
        primitives = []
        for material_index in sorted(set(int(value) for value in triangle_material)):
            rows = triangle_by_material[
                numpy.flatnonzero(triangle_material == material_index)].reshape(-1)
            identity = (material_identities[material_index]
                        if material_index < len(material_identities)
                        else material_identities[-1])
            primitives.append((identity, inverse[rows]))

        return ObjectData(object_reference.name,
                          _column_major(object_reference.matrix_world),
                          int(order.shape[0]), sources, primitives,
                          identity_of(object_reference))
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
                "identity": entry.identity,
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
            include_colors=True, binding=None):
    """Gather, write and publish one mesh generation. Returns the generation."""
    created_materials = ensure_materials(objects_to_send)
    if created_materials:
        depsgraph.update()
    fresh = adopt_identities(objects_to_send)
    if fresh:
        LOG.warning(
            "%d material(s) had no identity and were given one just now. Save the "
            ".blend: an unsaved file mints different identities next session, and "
            "Painter then builds new Texture Sets beside the ones already painted",
            len(fresh))
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
        return staging.publish(record_module.mesh(
            source="blender",
            intent=intent,
            scene=scene_description,
            materials=collect_material_rows(objects_to_send, fresh),
            unit_scale=unit_scale,
            up_axis="Z",
            binding_record=binding))
