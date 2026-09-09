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

from ...Kernel import record as record_module
from ...Kernel.glb import (AttributeLayout, GlbWriter, MeshLayout, PrimitiveLayout,
                             SceneLayout, SEMANTIC_COLOR_0, SEMANTIC_NORMAL,
                             SEMANTIC_POSITION, TEXCOORD_PREFIX)
from ...Kernel.log import logger
from . import texture_publish

LOG = logger("blender.mesh")

MAXIMUM_TEXCOORD_SETS = 8
IDENTITY_PROPERTY = "ruri_bridge_identity"
#: The custom property a material uses to say what its shading row is: which
#: shader vocabulary it speaks, which variant of it, and which property groups
#: hold the values under what spelling. Written by whatever generated the
#: material; the bridge only reads it.
SHADING_DECLARATION = "ruri_shading"
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
                 "identity", "include_colors")

    def __init__(self, name, node_matrix, vertex_count, sources, primitives, identity,
                 include_colors=True):
        self.name = name
        self.node_matrix = node_matrix
        self.vertex_count = vertex_count
        self.sources = sources
        self.primitives = primitives
        self.identity = identity
        #: What it was read WITH, so a send that asks for something different does
        #: not quietly get the previous answer.
        self.include_colors = include_colors


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


def _linear(value):
    """One authored channel, as the shader reads it.

    The engine these materials come from linearises a gamma-encoded property on
    upload, and this is that curve to the letter -- including the branch at one,
    which is a plain 2.2 power rather than the sRGB piece, and which is what
    carries an HDR colour's overbright range through instead of flattening it.
    Written out here because the value has to be identical to the one the
    producing side's own shader reads; a curve that agreed only below one would
    put every emissive colour somewhere else.
    """
    one = float(value)
    if one <= 0.04045:
        return one / 12.92
    if one < 1.0:
        return ((one + 0.055) / 1.055) ** 2.4
    return one ** 2.2


def _plain(value):
    """One custom property value as something that can cross."""
    if isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "to_list"):
        return value.to_list()
    return [_plain(entry) for entry in value]


def _declared_row(material):
    """The parameter row a material says it has, spelled the way its shader spells it.

    A generator stores a row in whatever shape suits it -- this one keeps three
    property groups by value type -- while a shader has one flat set of uniform
    names. The two differ, and the difference is not guessable: here it is a
    ``_ST`` suffix on one group of sixteen. So the material states it, and this
    reads the statement: which property groups hold the row, and how each group's
    keys spell out over there.

    Nothing here knows what those groups are called. A table of group names kept
    on this side would be a second copy of a rule that lives in the generator,
    and the failure it buys is silent: a new group is simply not sent, and the
    values that were in it look like values the far side chose not to expose.

    A group the material does not name is not part of the row. Materials carry
    other people's property groups -- an exporter's settings, a panel's fold
    state -- and those are not shading parameters just because they are nearby.
    """
    declaration = material.get(SHADING_DECLARATION)
    if declaration is None:
        return None
    row = {}
    for group_name, spelling in dict(declaration.get("values") or {}).items():
        group = material.get(group_name)
        if group is None:
            continue
        # One conversion of the whole group, not a lookup per name: asking for
        # the keys and then for each key's value walks the property tree once
        # per name, and a character's worth of materials is five thousand of
        # them on every live tick. The plain spelling is the common one and is
        # the key itself, so it skips the formatting too.
        plain = spelling == "{0}"
        for key, value in dict(group).items():
            row[key if plain else spelling.format(key)] = _plain(value)
    # Values the material has for its shader that are not in any of its property
    # groups, because on this side they are not parameters at all. The part a
    # material belongs to is the one that matters: this application compiles a
    # tree per part, so the part is structure here and a uniform over there, and
    # a shader that never hears it renders every material as part zero -- a
    # character whose face, hair and eyes are all shaded as plain surfaces, with
    # nothing reported anywhere.
    for name, value in dict(declaration.get("constants") or {}).items():
        row[name] = _plain(value)
    # An offer is what the far side's shader should READ, and a gamma-encoded
    # parameter is stored authored and read linear. This side converts on the way
    # into its own shader; a row handed over as it is stored arrives a whole
    # gamma curve away, with every name matching and nothing to report -- the
    # colours are simply wrong. Which names those are is the material's to say.
    for name in list(declaration.get("gamma") or []):
        value = row.get(str(name))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            row[str(name)] = _linear(value)
        elif isinstance(value, list) and len(value) >= 3:
            # The fourth component of a colour was never chromatic.
            row[str(name)] = [_linear(one) for one in value[:3]] + list(value[3:])
    return {"shader": str(declaration.get("shader") or ""),
            "name": str(declaration.get("name") or ""),
            "variant": str(declaration.get("variant") or ""),
            "parameters": row}


def _material_row(material, fresh=()):
    """Which material this is, and whose shading vocabulary it speaks.

    Not what it is set to. The values are a state that keeps changing while the
    model does not, they have a channel of their own that says so, and putting
    them in every mesh generation as well was a hundred and ninety kilobytes and
    ten milliseconds per live tick for a copy nobody read.
    """
    row = {"identity": identity_of(material), "name": material.name,
           "identity_is_new": material.name in fresh}
    declaration = material.get(SHADING_DECLARATION)
    if declaration is not None:
        row["shading"] = {"shader": str(declaration.get("shader") or ""),
                          "name": str(declaration.get("name") or ""),
                          "variant": str(declaration.get("variant") or "")}
    return row


def _worn_by_triangles(objects):
    """The materials some triangle in scope is actually rendered with.

    A model imported from a game arrives with slots nothing uses -- variant and
    detail-level leftovers -- and a slot no triangle points at is not shading
    anything. Offering its values means offering them for a material the other
    side has no Texture Set for, because it was never sent one, which reads in
    that application's log as the two sides disagreeing about the model.
    """
    worn = set()
    for object_reference in objects:
        data = getattr(object_reference, "data", None)
        attributes = getattr(data, "attributes", None)
        slots = object_reference.material_slots
        if attributes is None:
            worn.update(slot.material.name for slot in slots if slot.material)
            continue
        # Read the attribute, not the polygons. Both answer the same question and
        # one of them is free: asking each polygon costs 133 ms on a character
        # here -- on EVERY live tick -- while the attribute is one buffer copy.
        # An absent attribute is itself the answer: this application only stores
        # it once some face leaves slot zero.
        attribute = attributes.get("material_index")
        if attribute is None:
            used = (0,)
        else:
            indices = numpy.empty(len(attribute.data), dtype=numpy.int32)
            attribute.data.foreach_get("value", indices)
            used = numpy.unique(indices)
        for index in used:
            if index < len(slots) and slots[index].material is not None:
                worn.add(slots[index].material.name)
    return worn


def parameter_rows(objects, fresh=()):
    """Every material in scope that some triangle renders with, and what it is
    set to.

    Read off the slots rather than off a payload: this answers "what is the
    shading in scope", which is a question about the document and not about the
    last thing that was sent. Which slots count is the same question the mesh
    payload answers with :func:`materials_in`, and it has the same answer.
    """
    rows = []
    seen = set()
    worn = _worn_by_triangles(objects)
    for object_reference in objects:
        for slot in object_reference.material_slots:
            if slot.material is None or slot.material.name in seen:
                continue
            if slot.material.name not in worn:
                continue
            seen.add(slot.material.name)
            row = _material_row(slot.material, fresh)
            declared = _declared_row(slot.material)
            if declared is not None:
                row["properties"] = declared["parameters"]
            else:
                properties = {}
                for key in slot.material.keys():
                    if key == IDENTITY_PROPERTY:
                        continue
                    value = slot.material[key]
                    if hasattr(value, "keys"):
                        continue
                    properties[key] = _plain(value)
                if properties:
                    row["properties"] = properties
            rows.append(row)
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


#: What the last publish read out of each object, by object name. Reading an
#: object is 81% of a publish on a real scene, and most objects are the same as
#: they were -- so the expensive half is skipped for everything Blender did not
#: report as changed.
_GATHERED = {}


def forget_gathered(names=None):
    """Drop cached reads. Everything, or just the objects named."""
    if names is None:
        _GATHERED.clear()
        return
    for name in names:
        _GATHERED.pop(name, None)


def gather_scope(objects, depsgraph, include_colors=True, changed=None):
    """Read every object, reusing what has not changed since the last read.

    ``changed`` is the set of names Blender reported as updated; None means
    "assume everything" -- which is what a manual send does, because a manual
    send is somebody saying they want what is there now.
    """
    gathered = []
    reused = 0
    for object_reference in objects:
        name = object_reference.name
        entry = _GATHERED.get(name)
        matrix = _column_major(object_reference.matrix_world)
        data_name = getattr(object_reference.data, "name", "")
        stale = (entry is None
                 or changed is None
                 or name in changed
                 or (data_name and data_name in changed)
                 or entry.include_colors != include_colors)
        # A move is not a re-read: the same buffers at a different place. The
        # matrix rides on the entry and is refreshed either way.
        if stale:
            entry = gather_object(object_reference, depsgraph, include_colors)
            if entry is None:
                _GATHERED.pop(name, None)
                LOG.warning("%s evaluated to no triangles and was skipped", name)
                continue
            _GATHERED[name] = entry
        else:
            entry.node_matrix = matrix
            reused += 1
        gathered.append(entry)
    if reused:
        LOG.info("reused %d of %d object(s) unchanged since the last send",
                 reused, len(objects))
    return gathered


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
        # numpy, not a Python loop over every triangle: measured 134 ms against
        # 29 ms on the five heaviest objects of a real scene, for the same answer.
        for material_index in numpy.unique(triangle_material).tolist():
            rows = triangle_by_material[
                numpy.flatnonzero(triangle_material == material_index)].reshape(-1)
            identity = (material_identities[material_index]
                        if material_index < len(material_identities)
                        else material_identities[-1])
            primitives.append((identity, inverse[rows]))

        return ObjectData(object_reference.name,
                          _column_major(object_reference.matrix_world),
                          int(order.shape[0]), sources, primitives,
                          identity_of(object_reference), include_colors)
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


def materials_in(objects, gathered):
    """The materials the payload actually contains, in a stable order.

    A slot no triangle uses does not reach the other side: the GLB has no
    primitive for it, so the consumer builds nothing for it, and a row describing
    it is a row about something that was not sent. Worse than useless -- the
    consumer reports it as a material it has no place for, which reads as the two
    sides disagreeing about the model when nothing is wrong at all.
    """
    wanted = {identity for entry in gathered for identity, _indices in entry.primitives}
    found = {}
    for object_reference in objects:
        for slot in object_reference.material_slots:
            material = slot.material
            if material is None:
                continue
            identity = identity_of(material)
            if identity in wanted:
                found.setdefault(identity, material)
    return [found[identity] for identity in sorted(found)]


def publish(arena, publisher, objects_to_send, depsgraph, intent, unit_scale,
            include_colors=True, binding=None, changed=None):
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
    gathered = gather_scope(objects_to_send, depsgraph, include_colors, changed)
    if not gathered:
        raise RuntimeError("nothing to publish: no object in scope evaluated to triangles")

    with publisher.staging() as staging:
        scene_description = write_glb(
            arena, staging.path(record_module.SCENE_FILE_NAME), gathered)
        sent = materials_in(objects_to_send, gathered)
        # A live tick names what changed; a manual send names nothing. Only the
        # second one is somebody asking for everything, and the textures are the
        # expensive half of everything.
        materials = sent if changed is None else ()
        return staging.publish(record_module.mesh(
            source="Blender",
            intent=intent,
            scene=scene_description,
            materials=[_material_row(material, fresh) for material in sent],
            unit_scale=unit_scale,
            up_axis="Z",
            binding_record=binding,
            textures=texture_publish.publish_into(staging, materials)))
