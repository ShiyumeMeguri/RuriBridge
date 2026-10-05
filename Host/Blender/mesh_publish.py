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
#: What the generated shading stacks state about the scene's main light, each time
#: they pick it: on the scene, the direction toward it in Blender's world (w = 0; a
#: position when w = 1); on every light, whether it is the one (the last component).
#: Written by the stacks; the bridge only reads them, so it shades with the light
#: Blender shades with rather than choosing one of its own.
MAIN_LIGHT_VECTOR = "ruri_main_light_vector"
LIGHT_STATE = "ruri_light_state"
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


def _material_at(slots, index):
    """The material a face with this slot index renders with. Past the last slot
    Blender uses the last one; an object with no slot renders with none."""
    if not len(slots):
        return None
    return slots[min(index, len(slots) - 1)].material


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
            material = _material_at(slots, index)
            if material is not None:
                _, wearing = found.setdefault(material.name, (material, []))
                if object_reference not in wearing:
                    wearing.append(object_reference)
    return {name: found[name] for name in sorted(found)}


def worn_materials(objects):
    return {name: material for name, (material, _) in wearers(objects).items()}


def bare_objects(objects):
    """Objects some of whose faces wear no material. Those faces have no Texture Set
    to paint into, so they stay out of the texturing tool.

    Nothing is made up for them: a material named after the object would be a name
    nobody chose, in Blender and as a Texture Set, and filling a slot behind the
    user's back changes what the object renders with.
    """
    found = []
    for object_reference in objects:
        slots = object_reference.material_slots
        if any(_material_at(slots, index) is None for index in _worn_indices(object_reference)):
            found.append(object_reference.name)
    return sorted(found)


def declared_shader(material):
    """The generated shader a material says it runs, and that shader's identity; two
    empty strings for a material whose shading nobody declared."""
    declaration = material.get(SHADING_DECLARATION)
    if declaration is None:
        return "", ""
    return str(declaration.get("name") or ""), str(declaration.get("identity") or "")


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
    texture_sets = [texture_set_of(slot.material) if slot.material is not None else ""
                    for slot in object_reference.material_slots]
    if not texture_sets:
        return None
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
    bare = bare_objects(objects)
    if bare:
        LOG.info("faces that wear no material stay out of Painter: %s", ", ".join(bare))
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


def _authored(value):
    """The way back from ``_linear``: a channel as the shader read it, as it is authored."""
    one = float(value)
    if one <= 0.04045 / 12.92:
        return one * 12.92
    if one < 1.0:
        return 1.055 * one ** (1.0 / 2.4) - 0.055
    return one ** (1.0 / 2.2)


def _plain(value):
    """One custom property value as something that can cross."""
    if isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "to_list"):
        return value.to_list()
    return [_plain(entry) for entry in value]


def _shaped_like(held, offered):
    """An offered value in the shape the material holds that property in."""
    if isinstance(held, list):
        if not isinstance(offered, (list, tuple)):
            raise ValueError("it holds {0} numbers and was offered one".format(len(held)))
        taken = [float(one) for one in offered][:len(held)]
        return taken + [float(one) for one in held[len(taken):]]
    if isinstance(offered, (list, tuple)):
        raise ValueError("it holds one number and was offered {0}".format(len(offered)))
    if isinstance(held, bool):
        return bool(offered)
    if isinstance(held, int):
        return int(round(float(offered)))
    return float(offered)


def _same(held, offered):
    """Whether a value already holds what is offered, across a 32-bit round trip."""
    if isinstance(held, list):
        return len(held) == len(offered) and all(_same(one, other) for one, other in zip(held, offered))
    return abs(float(held) - float(offered)) <= 1e-6 * max(1.0, abs(float(held)), abs(float(offered)))


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


def write_row(material, values):
    """The way back from ``declared_row``: values spelled the shader's way, written into
    the property groups the declaration names, authored the way the material keeps them.

    Only what the material holds is written, and only where it differs: a constant of
    the material, or a name it has no property for, is left as it is. Returns the names
    written, the names it holds no property for, and the ones offered in a shape it
    cannot hold, with why.
    """
    declaration = material.get(SHADING_DECLARATION)
    if declaration is None:
        raise RuntimeError("{0} declares no shading".format(material.name))
    gamma = {str(name) for name in declaration.get("gamma") or []}
    constants = set(dict(declaration.get("constants") or {}))
    places = {}
    for group_name, spelling in dict(declaration.get("values") or {}).items():
        group = material.get(group_name)
        if group is None:
            continue
        for key in group.keys():
            places[key if spelling == "{0}" else spelling.format(key)] = (group_name, key)
    groups = {}
    written, unheld, refused = [], [], {}
    for name, value in sorted(values.items()):
        if name in constants:
            continue
        place = places.get(name)
        if place is None:
            unheld.append(name)
            continue
        group_name, key = place
        group = groups.setdefault(group_name, {one: _plain(held)
                                               for one, held in material[group_name].items()})
        if name in gamma:
            value = ([_authored(one) for one in value[:3]] + list(value[3:])
                     if isinstance(value, (list, tuple)) else _authored(value))
        try:
            shaped = _shaped_like(group[key], value)
        except (TypeError, ValueError) as error:
            refused[name] = str(error)
            continue
        if _same(group[key], shaped):
            continue
        group[key] = shaped
        written.append(name)
    for group_name in sorted({places[name][0] for name in written}):
        material[group_name] = groups[group_name]
    return written, unheld, refused


#: The names the far side's shader exposes for the object's axes. Three columns
#: rather than a matrix because a shader parameter is a vector.
OBJECT_BASIS_PARAMETERS = ("i_ObjectToWorld0", "i_ObjectToWorld1", "i_ObjectToWorld2")
#: The names the far side's shader exposes for the main light: the direction toward
#: it in the far side's world, its colour, and its strength.
MAIN_LIGHT_PARAMETERS = ("v_MainLightDirection", "v_MainLightColor", "f_MainLightIntensity")

#: What the shading language calls object space, relative to Blender's: Y and Z
#: swapped. A reflection, not a rotation -- the two handedness conventions differ.
_OBJECT_AXIS_SWAP = mathutils.Matrix(((1.0, 0.0, 0.0, 0.0),
                                      (0.0, 0.0, 1.0, 0.0),
                                      (0.0, 1.0, 0.0, 0.0),
                                      (0.0, 0.0, 0.0, 1.0)))


def speakers(objects):
    """The material that speaks for each Texture Set, and one object wearing it.

    The material named like the Texture Set speaks for it; a Texture Set painted by
    materials none of which carries that name is spoken for by the first of them
    in name order. Several materials painting into one Texture Set share one
    shader instance over there, so only one row can be its row, and the rule has
    to be one a person can predict -- the same rule both ways.
    """
    found = {}
    for name, (material, wearing) in wearers(objects).items():
        texture_set = texture_set_of(material)
        if not texture_set:
            continue
        if texture_set not in found or name == texture_set:
            found[texture_set] = (material, wearing[0])
    return dict(sorted(found.items()))


def shading_rows(objects):
    """What each Texture Set's shading is, as the material that speaks for it states it.

    A material whose shading nobody declared has no row: its shader is not something
    this side knows how to describe.
    """
    rows = {}
    light = None
    scene = bpy.context.scene
    for texture_set, (material, wearer) in speakers(objects).items():
        declared = declared_row(material)
        if declared is None:
            continue
        for parameter, column in zip(OBJECT_BASIS_PARAMETERS, object_basis(wearer)):
            declared["parameters"][parameter] = column
        light = light or main_light(scene)
        declared["parameters"].update(zip(MAIN_LIGHT_PARAMETERS, light))
        declared["parameters"].update(engine_state(scene, material))
        declared["material"] = material.name
        rows[texture_set] = declared
    return rows


def engine_state(scene, material):
    """The engine globals a material's shader reads from the scene, as they stand in it now.

    The material declares them with their defaults; the scene's world holds what a level or a
    volume stack states for each as its difference from the default, and the shader reads the
    sum -- so a scene with nothing stated answers the defaults. They travel with every row
    because the far side keeps a value for each on every shader instance and keeps it across
    shader generations: left alone, it shades with whatever defaults the instance was first
    given."""
    declaration = material[SHADING_DECLARATION]
    if "engine" not in declaration:
        raise RuntimeError("{0} was built by a shading stack that does not declare the engine globals its "
                           "shader reads; it is rebuilt with the current one when the file is opened again"
                           .format(material.name))
    declared = dict(declaration["engine"])
    world = scene.world
    values = {}
    for name, base in declared.items():
        delta = world.get(name) if world is not None else None
        value = [float(component) for component in base]
        if delta is not None:
            value = [component + float(offset) for component, offset in zip(value, delta)]
        values[str(name)] = value[0] if len(value) == 1 else value
    return values


def object_basis(object_reference):
    """The object's axes as the far side's world sees them, column by column."""
    root = mathutils.Matrix(PAINTER_AXES.tolist()).to_4x4()
    matrix = root @ object_reference.matrix_world @ _OBJECT_AXIS_SWAP
    return [[matrix[row][column] for row in range(3)] + [0.0] for column in range(3)]


def main_light(scene):
    """The main light the shading stacks shade this scene with, as the far side takes it: the
    direction toward it in the far side's world, its colour, its strength.

    The far side takes a direction only. A main light stated as a position -- a lamp, not a
    sun -- has a different direction at every point it lights, so it is refused rather than
    flattened into one."""
    vector = scene.get(MAIN_LIGHT_VECTOR)
    chosen = [obj for obj in scene.objects
              if obj.type == "LIGHT" and len(obj.get(LIGHT_STATE) or ()) == 4 and obj[LIGHT_STATE][3] == 1.0]
    if vector is None or len(chosen) != 1:
        raise RuntimeError("the shading stacks have stated no main light for this scene yet "
                           "({0} light(s) marked as it)".format(len(chosen)))
    if float(vector[3]) != 0.0:
        raise RuntimeError("the main light {0} is a {1} light; Painter's shader takes only a direction "
                           "toward a sun".format(chosen[0].name, chosen[0].data.type.lower()))
    toward = PAINTER_AXES @ numpy.array([float(value) for value in vector[:3]])
    toward /= numpy.linalg.norm(toward)
    data = chosen[0].data
    if data.specular_factor != 1.0:
        LOG.warning("the main light %s scales its specular by %.3f; Painter's shader has no such "
                    "factor and lights at 1", chosen[0].name, data.specular_factor)
    return [float(value) for value in toward], [float(value) for value in data.color], float(data.energy)
