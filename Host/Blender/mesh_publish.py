# -*- coding: utf-8 -*-
"""Turning Blender's meshes into the surface a texturing project paints on.

**What crosses is the surface somebody paints on**, not the picture this
application renders. That is the base mesh in its rest shape, with the modelling
modifiers that make it -- a mirror, a triangulation -- and without the ones whose
output only exists for the render: a rig's pose, and whatever geometry nodes grow
at render time, outline shells and fur layers among them. A texturing tool given
those paints on a posed body inside twenty copies of itself.

**It lands in the project's own frame.** Painter places every 3D projection and
re-projects every stroke relative to the frame the project's surface first came
in, and it refuses to carry strokes onto a surface whose units or scale differ.
So the frame is a fact of the project, stated by Painter, and the surface is
written into it: a project the bridge starts measures in centimetres, and one that
began as somebody else's file keeps whatever frame that file had. A surface sent
in any other frame is, to Painter, a different object.

**It is an OBJ of the polygons as they are.** Painter triangulates on import, and
the triangulation it chose is part of what a stroke or a selection is recorded
against; handing it Blender's own triangles would be handing it a second opinion.
The format carries one UV set and no material description at all -- the UV set
Painter paints is the one Blender renders with, and a material description is
exactly what a glTF import turns into an unasked-for layer on every new Texture
Set.

**Which Texture Set a material paints into** is the one fact this side keeps
about the other. It is a name, written on the material the first time it crosses
and held still afterwards, so renaming the material here renames a label and not
the paint. Several materials may name the same Texture Set -- one material split
in two across one UV layout is still one surface to paint -- and a material may
name none, which keeps its faces out of the texturing tool entirely.
"""

from __future__ import annotations

import contextlib

import bpy
import mathutils
import numpy

from ...Kernel import arena as arena_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

LOG = logger("blender.mesh")

#: The Texture Set a material paints into. Absent until the material first
#: crosses; an empty string when it is deliberately kept out of the texturing
#: tool. A key written into .blend files, so it never changes spelling.
IDENTITY_PROPERTY = "ruri_bridge_identity"
#: The custom property a generated material uses to say what its shading row
#: is. Written by whatever generated the material; the bridge only reads it.
SHADING_DECLARATION = "ruri_shading"
#: Modifier types whose output belongs to the render and not to the surface: an
#: armature poses the surface, and geometry nodes in this toolchain grow render
#: geometry -- outline shells, fur layers -- on top of it.
RENDER_TIME_MODIFIERS = frozenset(("ARMATURE", "NODES"))
#: Blender's world turned the way a texturing tool stands, Y up: a quarter turn
#: about X, (x, y, z) becoming (x, z, -y). Written out rather than computed, so it
#: carries no rounding into a frame that has to match to the last bit.
PAINTER_AXES = numpy.array(((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)))


# -- which Texture Set ---------------------------------------------------------

def texture_set_of(material):
    """The Texture Set this material paints into; empty when it paints none.

    A material that has never crossed answers with its own name, which is also
    what it is settled to the first time it does.
    """
    if IDENTITY_PROPERTY in material.keys():
        return str(material[IDENTITY_PROPERTY])
    return material.name


def is_excluded(material):
    return IDENTITY_PROPERTY in material.keys() and not str(material[IDENTITY_PROPERTY])


def settle(material):
    """Hold the name still from here on, so a rename is a rename of the label."""
    if IDENTITY_PROPERTY not in material.keys():
        material[IDENTITY_PROPERTY] = material.name
    return str(material[IDENTITY_PROPERTY])


def paint_into(material, texture_set):
    """Make this material paint into that Texture Set, or into none when empty."""
    material[IDENTITY_PROPERTY] = texture_set


# -- which objects, which materials ---------------------------------------------

def scope(view_layer):
    """The objects the model is made of: every visible mesh in the view layer."""
    return [entry for entry in view_layer.objects
            if entry.type == "MESH" and entry.visible_get()]


def _worn_indices(object_reference):
    """The slot indices some face of this object is actually rendered with.

    Read off the attribute, not the polygons: both answer the same question and
    one of them is free. An absent attribute is itself the answer -- Blender only
    stores it once some face leaves slot zero.
    """
    attribute = object_reference.data.attributes.get("material_index")
    if attribute is None:
        return (0,)
    indices = numpy.empty(len(attribute.data), dtype=numpy.int32)
    attribute.data.foreach_get("value", indices)
    return tuple(numpy.unique(indices).tolist())


def wearers(objects):
    """Every material some face in scope wears, by name, in a stable order, with
    the objects some face of which wears it, in scope order.

    A slot no face points at is not shading anything -- a model imported from a
    game arrives with variant leftovers -- and a Texture Set for it would be a
    Texture Set for nothing.
    """
    found = {}
    for object_reference in objects:
        slots = object_reference.material_slots
        for index in _worn_indices(object_reference):
            if index < len(slots) and slots[index].material is not None:
                _, wearing = found.setdefault(
                    slots[index].material.name, (slots[index].material, []))
                if object_reference not in wearing:
                    wearing.append(object_reference)
    return {name: found[name] for name in sorted(found)}


def worn_materials(objects):
    return {name: material for name, (material, _) in wearers(objects).items()}


def ensure_materials(objects):
    """Give every object a real material before its name crosses the bridge.

    A Texture Set is named after the material it came from, and the return trip
    finds its way home by that same name. An object with no material has no name
    to give, so it gets one here, named after itself, and the log says so.
    """
    created = []
    for object_reference in objects:
        slots = list(object_reference.material_slots)
        if not slots:
            material = bpy.data.materials.new(object_reference.name)
            object_reference.data.materials.append(material)
            created.append(material.name)
            continue
        for index, slot in enumerate(slots):
            if slot.material is not None:
                continue
            material = bpy.data.materials.new(object_reference.name)
            object_reference.data.materials[index] = material
            created.append(material.name)
    if created:
        LOG.info("created %d material(s) so the paint has somewhere to come back to: %s",
                 len(created), ", ".join(created))
    return created


def texture_set_rows(objects):
    """Which Texture Set each worn material paints into, grouped by Texture Set."""
    rows = {}
    for material in worn_materials(objects).values():
        name = texture_set_of(material)
        if name:
            rows.setdefault(name, []).append(material.name)
    return [{"texture_set": name, "materials": sorted(materials)}
            for name, materials in sorted(rows.items())]


@contextlib.contextmanager
def surface_only(objects):
    """Evaluate these objects as the surface, with render-time modifiers off.

    Switched off for the duration of one read and back on in ``finally``, so the
    file holds exactly what it held before. Only modifiers that were on are
    touched -- one the user had switched off stays off.
    """
    switched = []
    try:
        for object_reference in objects:
            for modifier in object_reference.modifiers:
                if modifier.type in RENDER_TIME_MODIFIERS and modifier.show_viewport:
                    modifier.show_viewport = False
                    switched.append(modifier)
        yield
    finally:
        for modifier in switched:
            modifier.show_viewport = True


# -- reading one object ----------------------------------------------------------

class SurfacePart:
    """One object's paintable faces, already in the project's frame.

    The corner arrays index this object's own vertex, UV and normal lists; the
    writer moves them past whatever was written before. ``faces`` names, per
    Texture Set, the polygons that paint into it as slices of the corner arrays.
    """

    __slots__ = ("name", "positions", "texcoords", "normals",
                 "corner_vertex", "corner_texcoord", "corner_normal", "faces")

    def __init__(self, name, positions, texcoords, normals,
                 corner_vertex, corner_texcoord, corner_normal, faces):
        self.name = name
        self.positions = positions
        self.texcoords = texcoords
        self.normals = normals
        self.corner_vertex = corner_vertex
        self.corner_texcoord = corner_texcoord
        self.corner_normal = corner_normal
        self.faces = faces


def _render_uv_layer(mesh, name):
    """The UV map Blender renders with -- the one the far side paints in."""
    layers = list(mesh.uv_layers)
    if not layers:
        LOG.warning("%s has no UV map; Painter will have to unwrap it", name)
        return None
    return next((layer for layer in layers if layer.active_render), layers[0])


def _corner_order(starts, totals):
    """Every corner of the given polygons, polygon after polygon, in loop order."""
    offsets = numpy.cumsum(totals) - totals
    return (numpy.arange(int(totals.sum()), dtype=numpy.int64)
            - numpy.repeat(offsets, totals) + numpy.repeat(starts, totals))


def gather_object(object_reference, depsgraph, frame_of_project):
    """Read one evaluated object into the project's frame.

    None when nothing of it crosses: no faces, or every face wears a material that
    paints into no Texture Set.
    """
    texture_sets = [texture_set_of(slot.material) for slot in object_reference.material_slots]
    evaluated = object_reference.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    if mesh is None:
        return None
    try:
        polygon_count = len(mesh.polygons)
        if polygon_count == 0:
            return None
        polygon_material = numpy.empty(polygon_count, dtype=numpy.int32)
        mesh.polygons.foreach_get("material_index", polygon_material)
        slot_of_polygon = numpy.minimum(polygon_material, len(texture_sets) - 1)
        crossing = numpy.array([bool(name) for name in texture_sets])[slot_of_polygon]
        if not crossing.any():
            return None
        starts = numpy.empty(polygon_count, dtype=numpy.int64)
        mesh.polygons.foreach_get("loop_start", starts)
        totals = numpy.empty(polygon_count, dtype=numpy.int64)
        mesh.polygons.foreach_get("loop_total", totals)

        corner_count = len(mesh.loops)
        vertex_of_loop = numpy.empty(corner_count, dtype=numpy.int32)
        mesh.loops.foreach_get("vertex_index", vertex_of_loop)
        positions = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
        mesh.vertices.foreach_get("co", positions)
        normal_of_loop = numpy.empty(corner_count * 3, dtype=numpy.float32)
        mesh.corner_normals.foreach_get("vector", normal_of_loop)
        layer = _render_uv_layer(mesh, object_reference.name)

        kept = numpy.flatnonzero(crossing)
        corners = _corner_order(starts[kept], totals[kept])
        used, corner_vertex = numpy.unique(vertex_of_loop[corners], return_inverse=True)

        world = numpy.array(object_reference.matrix_world, dtype=numpy.float64)
        scale = float(frame_of_project["scale"])
        offset = numpy.array(frame_of_project["offset"], dtype=numpy.float64)
        local = positions.reshape(-1, 3)[used].astype(numpy.float64)
        placed = (local @ world[:3, :3].T + world[:3, 3]) @ PAINTER_AXES.T * scale + offset

        normal_matrix = numpy.array(
            object_reference.matrix_world.to_3x3().inverted_safe().transposed(),
            dtype=numpy.float64)
        turned = normal_of_loop.reshape(-1, 3)[corners].astype(numpy.float64) @ (
            PAINTER_AXES @ normal_matrix).T
        turned /= numpy.maximum(numpy.linalg.norm(turned, axis=1, keepdims=True), 1e-30)
        normals, corner_normal = numpy.unique(turned.astype(numpy.float32), axis=0,
                                              return_inverse=True)
        texcoords = numpy.empty((0, 2), dtype=numpy.float32)
        corner_texcoord = None
        if layer is not None:
            uv_of_loop = numpy.empty(corner_count * 2, dtype=numpy.float32)
            layer.uv.foreach_get("vector", uv_of_loop)
            texcoords, corner_texcoord = numpy.unique(uv_of_loop.reshape(-1, 2)[corners],
                                                      axis=0, return_inverse=True)
            corner_texcoord = corner_texcoord.reshape(-1)

        boundaries = numpy.concatenate(([0], numpy.cumsum(totals[kept])))
        slot_of_kept = slot_of_polygon[kept]
        faces = []
        # Two materials painting into one Texture Set are one surface over there,
        # so their faces cross under one name rather than two that happen to match.
        for name in sorted({name for name in texture_sets if name}):
            slots = [index for index, one in enumerate(texture_sets) if one == name]
            polygons = numpy.flatnonzero(numpy.isin(slot_of_kept, slots))
            if len(polygons):
                faces.append((name, boundaries[polygons], boundaries[polygons + 1]))
        return SurfacePart(object_reference.name, placed, texcoords, normals,
                           corner_vertex.reshape(-1), corner_texcoord,
                           corner_normal.reshape(-1), faces)
    finally:
        evaluated.to_mesh_clear()


# -- writing ---------------------------------------------------------------------

def _corner_tokens(part, vertex_base, texcoord_base, normal_base):
    """Every corner as an OBJ face token, numbered past the parts written before."""
    vertices = (part.corner_vertex + (vertex_base + 1)).tolist()
    normals = (part.corner_normal + (normal_base + 1)).tolist()
    if part.corner_texcoord is None:
        return ["{0}//{1}".format(vertex, normal) for vertex, normal in zip(vertices, normals)]
    texcoords = (part.corner_texcoord + (texcoord_base + 1)).tolist()
    return ["{0}/{1}/{2}".format(vertex, texcoord, normal)
            for vertex, texcoord, normal in zip(vertices, texcoords, normals)]


def write_material_library(path, names):
    """Name every material the surface uses, and say nothing else about them.

    The importer reports each name it cannot find in a library as an error, and
    anything a library did say -- a colour, a shininess -- is what an importer
    would turn into layers nobody painted.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join("newmtl {0}\n".format(name) for name in sorted(names)))


def write_obj(path, parts):
    """Write every part into one OBJ, beside the library naming its materials.

    Returns the scene the record describes it as.
    """
    library = path.with_name(record_module.SURFACE_MATERIALS_FILE_NAME)
    write_material_library(library, {name for part in parts for name, _firsts, _ends
                                     in part.faces})
    lines = ["mtllib " + library.name]
    scene = []
    vertex_base = texcoord_base = normal_base = 0
    for part in parts:
        lines.append("o " + part.name)
        lines.extend("v {0!r} {1!r} {2!r}".format(*row) for row in part.positions.tolist())
        lines.extend("vt {0!r} {1!r}".format(*row) for row in
                     part.texcoords.astype(numpy.float64).tolist())
        lines.extend("vn {0!r} {1!r} {2!r}".format(*row) for row in
                     part.normals.astype(numpy.float64).tolist())
        tokens = _corner_tokens(part, vertex_base, texcoord_base, normal_base)
        counts = {}
        for name, firsts, ends in part.faces:
            lines.append("usemtl " + name)
            lines.extend("f " + " ".join(tokens[first:end])
                         for first, end in zip(firsts.tolist(), ends.tolist()))
            counts[name] = len(firsts)
        scene.append({
            "name": part.name,
            "texture_sets": counts,
            "bounds_min": part.positions.min(axis=0).tolist(),
            "bounds_max": part.positions.max(axis=0).tolist(),
        })
        vertex_base += len(part.positions)
        texcoord_base += len(part.texcoords)
        normal_base += len(part.normals)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    return scene


def publish(publisher, objects, frame_of_project):
    """Gather, write and publish the surface in the project's frame. Returns the generation."""
    if ensure_materials(objects):
        bpy.context.view_layer.update()
    for material in worn_materials(objects).values():
        settle(material)
    with surface_only(objects):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph.update()
        parts = []
        for object_reference in objects:
            part = gather_object(object_reference, depsgraph, frame_of_project)
            if part is None:
                LOG.info("%s has no face that paints into a Texture Set; not sent",
                         object_reference.name)
                continue
            parts.append(part)
    if not parts:
        raise RuntimeError("nothing to send: no visible object has a face that paints "
                           "into a Texture Set")
    with publisher.staging() as staging:
        path = staging.path(record_module.SURFACE_FILE_NAME)
        scene = write_obj(path, parts)
        arena_module.keep_in_memory(path)
        arena_module.keep_in_memory(staging.path(record_module.SURFACE_MATERIALS_FILE_NAME))
        return staging.publish(record_module.mesh(
            source="Blender",
            scene_file=record_module.SURFACE_FILE_NAME,
            scene=scene,
            materials=texture_set_rows(objects),
            frame_of_project=frame_of_project))


# -- shading rows ------------------------------------------------------------------

def _linear(value):
    """One authored channel, as the shader reads it.

    The engine these materials come from linearises a gamma-encoded property on
    upload, and this is that curve to the letter -- including the branch at one,
    which is a plain 2.2 power rather than the sRGB piece, and which is what
    carries an HDR colour's overbright range through instead of flattening it.
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


def declared_row(material):
    """The parameter row a material says it has, spelled the way its shader spells it.

    A generator stores a row in whatever shape suits it while a shader has one
    flat set of uniform names, and the difference is not guessable. So the
    material states it, and this reads the statement: which property groups hold
    the row, how each group's keys spell out over there, which values are
    constants of the material rather than parameters, and which are authored
    gamma-encoded and read linear.
    """
    declaration = material.get(SHADING_DECLARATION)
    if declaration is None:
        return None
    row = {}
    for group_name, spelling in dict(declaration.get("values") or {}).items():
        group = material.get(group_name)
        if group is None:
            continue
        plain = spelling == "{0}"
        for key, value in dict(group).items():
            row[key if plain else spelling.format(key)] = _plain(value)
    for name, value in dict(declaration.get("constants") or {}).items():
        row[name] = _plain(value)
    for name in list(declaration.get("gamma") or []):
        value = row.get(str(name))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            row[str(name)] = _linear(value)
        elif isinstance(value, list) and len(value) >= 3:
            row[str(name)] = [_linear(one) for one in value[:3]] + list(value[3:])
    return {"shader": str(declaration.get("shader") or ""),
            "name": str(declaration.get("name") or ""),
            "identity": str(declaration.get("identity") or ""),
            "variant": str(declaration.get("variant") or ""),
            "parameters": row}


#: The names the far side's shader exposes for the object's axes. Three columns
#: rather than a matrix because a shader parameter is a vector.
OBJECT_BASIS_PARAMETERS = ("i_ObjectToWorld0", "i_ObjectToWorld1", "i_ObjectToWorld2")

#: What the shading language calls object space, relative to Blender's: Y and Z
#: swapped. A reflection, not a rotation -- the two handedness conventions differ.
_OBJECT_AXIS_SWAP = mathutils.Matrix(((1.0, 0.0, 0.0, 0.0),
                                      (0.0, 0.0, 1.0, 0.0),
                                      (0.0, 1.0, 0.0, 0.0),
                                      (0.0, 0.0, 0.0, 1.0)))


def shading_rows(objects):
    """What each Texture Set's shading is, as the material that speaks for it states it.

    The material named like the Texture Set speaks for it; a Texture Set painted by
    materials none of which carries that name is spoken for by the first of them
    in name order. Several materials painting into one Texture Set share one
    shader instance over there, so only one row can be its row, and the rule has
    to be one a person can predict. A material whose shading nobody declared has
    no row: its shader is not something this side knows how to describe.
    """
    speakers = {}
    for name, (material, wearing) in wearers(objects).items():
        texture_set = texture_set_of(material)
        if not texture_set:
            continue
        if texture_set not in speakers or name == texture_set:
            speakers[texture_set] = (material, wearing[0])
    rows = {}
    for texture_set, (material, wearer) in sorted(speakers.items()):
        declared = declared_row(material)
        if declared is None:
            continue
        for parameter, column in zip(OBJECT_BASIS_PARAMETERS, object_basis(wearer)):
            declared["parameters"][parameter] = column
        declared["material"] = material.name
        rows[texture_set] = declared
    return rows


def object_basis(object_reference):
    """The object's axes as the far side's world sees them, column by column."""
    root = mathutils.Matrix(PAINTER_AXES.tolist()).to_4x4()
    matrix = root @ object_reference.matrix_world @ _OBJECT_AXIS_SWAP
    return [[matrix[row][column] for row in range(3)] + [0.0] for column in range(3)]
