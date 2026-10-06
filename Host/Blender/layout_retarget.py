# -*- coding: utf-8 -*-
"""Moving a Texture Set to another of its UV layouts, here and in Painter as one change.

Every mesh wearing a material that paints the Texture Set holds the new layout in a
second UV map of the same name. A retarget swaps that map's coordinates with the
render map's on the Texture Set's faces -- the render map, which the material samples
and Painter paints in, now holds the new layout, and the other map holds the old one
-- and states in the Texture Set's table (``layouts``) that the old layout lives in
that map now. Doing it again swaps them back.

Painter is asked first (``begin``): it says which UV sets the Texture Set's content
reads and hands over its mesh maps, laid out in the layout of the surface it holds,
with that surface's fingerprint, and the fills laying pictures of tangent normals. The
answer completes the change in one step (``complete``) -- only when the faces here are
still the ones the fingerprint was taken of, since the mesh maps were laid out on them:
the mesh maps are laid out again in the new layout, the normals of those fills are
carried in place into the frames of the new layout where islands turn, the coordinates
swap, the table is written, and the surface goes to Painter carrying all of it -- where
every fill laid out in the old layout goes on reading it through the UV set that now
holds it.

Pictures the material itself samples through the swapped maps are laid out again
beside it (``material_relayout``), so the material looks here as it looked before.
"""

from __future__ import annotations

import hashlib
import os

import bpy
import numpy

from ...Kernel import layout as layout_module
from ...Kernel import record as record_module
from ...Kernel import topic as topic_module
from ...Kernel.log import logger

from . import chart_resample, layout_triangles, layouts, material_relayout, mesh_publish, pixels

LOG = logger("blender.layout")

#: Kept only for the length of one exchange, by the generation of the ask.
_pending = {}

#: The share of comparable texels that must agree before a stored normal map's green
#: is taken to point one way.
_AGREEMENT = 0.99
#: Painter's ``additionalNormalMapBlending`` of a Texture Set whose normal channel
#: replaces the normal mesh map instead of combining with it.
_REPLACING = "DataBlendingMode_Replace"
#: How far from flat, in decoded tangent units, a stored normal may lean and still be
#: flat: two steps of eight bits.
_FLAT = 2.0 / 255.0


class Pending:
    """A layout change Blender asked Painter about and is waiting to complete."""

    __slots__ = ("texture_set", "target_layer")

    def __init__(self, texture_set, target_layer):
        self.texture_set = texture_set
        self.target_layer = target_layer


def waiting():
    """The Texture Sets whose layout change waits for Painter's answer."""
    return sorted(pending.texture_set for pending in _pending.values())


def painted_by(texture_set):
    """Every material of this document that paints into the Texture Set."""
    return [material for material in bpy.data.materials
            if material.library is None and mesh_publish.texture_set_of(material) == texture_set]


def _texture_set_polygons(object_reference, materials):
    """The polygons of an object's own mesh that wear one of these materials."""
    mesh = object_reference.data
    wanted = {material.as_pointer() for material in materials}
    slots = [slot.material is not None and slot.material.as_pointer() in wanted
             for slot in object_reference.material_slots]
    if not slots or not any(slots):
        return numpy.empty(0, dtype=numpy.int64)
    indices = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", indices)
    chosen = numpy.array(slots)[numpy.minimum(indices, len(slots) - 1)]
    return numpy.flatnonzero(chosen)


def wearing(materials):
    """Every mesh object of this document some face of which wears one of the materials,
    with those faces."""
    found = []
    for object_reference in bpy.data.objects:
        if object_reference.type != "MESH":
            continue
        polygons = _texture_set_polygons(object_reference, materials)
        if len(polygons):
            found.append((object_reference, polygons))
    return found


def target_layers(material):
    """The UV maps a retarget of this material's Texture Set can take its new layout from:
    every one each mesh wearing it has, other than the one it renders with."""
    texture_set = mesh_publish.texture_set_of(material)
    if not texture_set:
        return []
    shared = None
    for object_reference, _polygons in wearing(painted_by(texture_set)):
        mesh = object_reference.data
        render = mesh_publish.render_uv_layer(mesh)
        names = {layer.name for layer in mesh.uv_layers if render is None or layer.name != render.name}
        shared = names if shared is None else shared & names
    return sorted(shared or [])


def _check(texture_set, wearers, target_layer):
    """Refuse, by name, anything a swap of these meshes could not carry."""
    if not wearers:
        raise RuntimeError("no mesh wears a material that paints {0}".format(texture_set))
    faces_of_mesh = {}
    for object_reference, polygons in wearers:
        mesh = object_reference.data
        if mesh.library is not None or object_reference.library is not None:
            raise RuntimeError("{0} comes from a library and its UV maps cannot change here".format(
                object_reference.name))
        render = mesh_publish.render_uv_layer(mesh)
        if render is None:
            raise RuntimeError("{0} has no UV map".format(object_reference.name))
        if target_layer not in mesh.uv_layers:
            raise RuntimeError("{0} has no UV map {1!r}; every mesh wearing {2} needs one".format(
                object_reference.name, target_layer, texture_set))
        if render.name == target_layer:
            raise RuntimeError("{0} renders with {1!r}; the new layout has to be another UV map".format(
                object_reference.name, target_layer))
        key = mesh.as_pointer()
        if key in faces_of_mesh and not numpy.array_equal(faces_of_mesh[key], polygons):
            raise RuntimeError("{0} shares its mesh with an object whose faces paint other Texture "
                               "Sets; a swap of the mesh cannot follow both".format(object_reference.name))
        faces_of_mesh[key] = polygons


def begin(session, material, target_layer):
    """Ask Painter about a layout change of the Texture Set this material paints. Returns
    one line about it."""
    texture_set = mesh_publish.texture_set_of(material)
    if not texture_set:
        raise RuntimeError("{0} paints into no Texture Set".format(material.name))
    if texture_set in waiting():
        raise RuntimeError("{0} is already waiting for Painter's answer about its layout".format(
            texture_set))
    _check(texture_set, wearing(painted_by(texture_set)), target_layer)
    generation = session.publisher(topic_module.REQUEST).publish_record(record_module.request(
        "Blender", record_module.ASK_FOR_LAYOUT, texture_set=texture_set))
    _pending[generation.number] = Pending(texture_set, target_layer)
    return "asked Painter to prepare {0} for its layout in {1!r}".format(texture_set, target_layer)


def _held_by_painter(context, texture_set, record, frame_of_project):
    """Refuse unless the faces here are the ones Painter's mesh maps are laid out on."""
    parts, _tables, _count = mesh_publish.gather(mesh_publish.scope(context.view_layer),
                                                 frame_of_project)
    if mesh_publish.layout_fingerprints(parts).get(texture_set) != record["fingerprint"]:
        raise RuntimeError(
            "{0}'s faces or their coordinates are not the ones the surface Painter holds was "
            "sent with, and its mesh maps are laid out on that surface; Update Mesh, then "
            "change the layout".format(texture_set))


def _visible_wearers(view_layer, wearers):
    visible = {object_reference.as_pointer() for object_reference in mesh_publish.scope(view_layer)}
    return [object_reference for object_reference, _polygons in wearers
            if object_reference.as_pointer() in visible]


def _unpack(values):
    """Painter's ``normalUnpack`` of opaque lanes before any green coefficient: a unit
    vector, z kept above 1e-3."""
    vectors = values[..., :3].astype(numpy.float64) * 2.0 - 1.0
    vectors[..., 2] = numpy.maximum(vectors[..., 2], 1e-3)
    return vectors / numpy.linalg.norm(vectors, axis=-1, keepdims=True)


def _oriented(base, over):
    """Painter's ``normalBlendOriented``."""
    base = base.copy()
    base[..., 2] += 1.0
    over = over.copy()
    over[..., :2] *= -1.0
    blended = base * numpy.sum(base * over, axis=-1, keepdims=True) - over * base[..., 2:3]
    return blended / numpy.maximum(numpy.linalg.norm(blended, axis=-1, keepdims=True), 1e-30)


def _green(record, generation, triangles):
    """Which way the project's stored tangent normals point their green, +1 OpenGL or -1
    DirectX: its mesh maps and every picture a fill lays in the normal channel alike.

    Painter's shader library combines the stored normal map with the normal channel
    (``getTSNormal``: the oriented blend, or the channel alone where the Texture Set
    replaces the map), and that blend commutes with turning green over. So wherever the
    height is locally flat, the OpenGL export of the combined normal is the blend of the
    stored lanes -- or that blend with its green turned over. Which one, is read off every
    such texel."""
    convention = record["convention"]
    combined, _wide = pixels.read(str(generation.path(convention["probe_normal_gl"])))
    rows, columns = combined.shape[:2]

    def picture(name):
        values, _wide = pixels.read(str(generation.path(name)), size=(columns, rows))
        return values

    predicted = numpy.zeros((rows, columns, 3))
    predicted[..., 2] = 1.0
    if "Normal" in record["mesh_maps"]:
        predicted = _unpack(picture(record["mesh_maps"]["Normal"]["file"]))
    if "probe_channel_normal" in convention:
        channel = _unpack(picture(convention["probe_channel_normal"]))
        predicted = channel if convention.get("blending") == _REPLACING else _oriented(predicted, channel)
    flat = numpy.zeros((rows, columns), dtype=bool)
    flat[1:-1, 1:-1] = True
    if "probe_height" in convention:
        level = picture(convention["probe_height"])[..., 0]
        middle = level[1:-1, 1:-1]
        flat[1:-1, 1:-1] = ((middle == level[:-2, 1:-1]) & (middle == level[2:, 1:-1])
                            & (middle == level[1:-1, :-2]) & (middle == level[1:-1, 2:]))
    texels, _owners, _weights = chart_resample.rasterize(triangles.render, columns, rows)
    inside = numpy.zeros(rows * columns, dtype=bool)
    inside[texels] = True
    usable = inside.reshape(rows, columns) & flat & (numpy.abs(predicted[..., 1]) > 0.05)
    if usable.sum() < 64:
        raise RuntimeError("no texel of {0} shows which way its stored normals point their green -- "
                           "its height is bumped or its normals are flat wherever its faces lie -- "
                           "and its turning islands need it".format(record["texture_set"]))
    agree = (numpy.sign(predicted[..., 1]) == numpy.sign(combined[..., 1] * 2.0 - 1.0))[usable].mean()
    if agree >= _AGREEMENT:
        return 1.0
    if agree <= 1.0 - _AGREEMENT:
        return -1.0
    raise RuntimeError("Painter's combined normal of {0} agrees with its stored normals on {1:.1%} "
                       "of the texels that tell; which way their green points is undecided".format(
                           record["texture_set"], agree))


def _relaid_mesh_maps(record, generation, triangles, frames, green):
    """Painter's mesh maps laid out again in the new layout, as PNG files by usage."""
    relaid = {}
    for usage, entry in sorted(record["mesh_maps"].items()):
        values, _wide = pixels.read(str(generation.path(entry["file"])))
        grey = bool((values[..., 0] == values[..., 1]).all() and (values[..., 0] == values[..., 2]).all())
        opaque = bool((values[..., 3] == 1.0).all())
        lanes = values[..., :1] if grey and opaque else values[..., :3] if opaque else values
        if lanes.min() < 0.0 or lanes.max() > 1.0:
            raise RuntimeError("Painter's {0} mesh map of {1} holds values outside 0..1, which a "
                               "laid-out PNG cannot keep".format(usage, record["texture_set"]))
        laid = chart_resample.relaid(lanes, triangles.render, triangles.target, entry["kind"],
                                     frames, green)
        relaid[usage] = ("{0}_{1}.png".format(record["texture_set"], entry["file"].rsplit(".", 1)[0]),
                         pixels.png(numpy.clip(laid, 0.0, 1.0), True))
    return relaid


def _bent(values, triangles, turning):
    """Whether a picture of tangent normals laid out in ``triangles`` bends anywhere a
    turning triangle covers."""
    rows, columns = values.shape[:2]
    texels, owners, _weights = chart_resample.rasterize(triangles, columns, rows, every=True)
    chosen = texels[turning[owners]]
    if not len(chosen):
        return False
    lanes = values.reshape(-1, values.shape[2])[chosen, :2] * 2.0 - 1.0
    return float(numpy.abs(lanes).max()) > _FLAT


def _taken(picture, render):
    """+1 when Painter takes a picture's green as stored, -1 when turned over, read off a
    render of a fill laying nothing but that picture, in the project's own convention."""
    rows, columns = render.shape[:2]
    stored, _wide = pixels.read(picture, size=(columns, rows))
    lean = stored[..., 1] * 2.0 - 1.0
    usable = numpy.abs(lean) > 0.1
    if usable.sum() < 16:
        raise RuntimeError("{0} leans nowhere enough to tell which way Painter takes its green".format(
            os.path.basename(picture)))
    agree = (numpy.sign(render[..., 1] * 2.0 - 1.0) == numpy.sign(lean))[usable].mean()
    if agree >= _AGREEMENT:
        return 1.0
    if agree <= 1.0 - _AGREEMENT:
        return -1.0
    raise RuntimeError("Painter's render of {0} agrees with its green on {1:.1%} of the texels; "
                       "which way it takes it is undecided".format(os.path.basename(picture), agree))


def _turned_fills(record, generation, triangles, frames, green, table, texture_set, directory):
    """The pictures of tangent normals Painter's fills read through a chart, carried in
    place into the new frames wherever the frames turn under bent normals, written into
    ``directory`` under names their bytes give them. Refuses, by name, whatever cannot be:
    a picture that is no file on this computer any more, normals that are computed, a
    fill other Texture Sets show too."""
    turning = frames.turning()
    replaced = {}
    problems = []
    os.makedirs(directory, exist_ok=True)
    fresh = record["convention"].get("fresh")
    known = []

    def written():
        if not known:
            render, _wide = pixels.read(str(generation.path(fresh["render"])))
            known.append(green() * _taken(str(generation.path(fresh["picture"])), render))
        return known[0]

    for fill in record["fills"]:
        declared = table["extra"].get(str(fill["index"])) if fill["index"] else None
        layout = triangles.extra[declared["layer"]] if declared else triangles.render
        values = None
        if fill["file"]:
            values, _wide = pixels.read(fill["file"])
            if not _bent(values, layout, turning):
                continue
        elif fill["render"]:
            rendered, _wide = pixels.read(str(generation.path(fill["render"])))
            if not _bent(rendered, triangles.render, turning):
                continue
        if values is None:
            problems.append("{0}: its normals are {1}, which cannot be turned with the islands "
                            "exactly".format(fill["name"], "computed" if fill["source"] == "procedural"
                                             else "a picture that is no file on this computer any more"))
            continue
        others = sorted(set(fill["members"]) - {texture_set})
        if others:
            problems.append("{0}: {1} show(s) it too, and their islands do not turn with "
                            "these".format(fill["name"], ", ".join(others)))
            continue
        reading, _wide = pixels.read(str(generation.path(fill["reading"])))
        try:
            turned = chart_resample.reframed(values[..., :3], layout, frames,
                                             green() * _taken(fill["file"], reading), written())
        except ValueError as error:
            problems.append("{0}: {1}".format(fill["name"], error))
            continue
        opaque = bool((values[..., 3] == 1.0).all())
        lanes = turned if opaque else numpy.concatenate((turned, values[..., 3:]), axis=-1)
        data = pixels.png(numpy.clip(lanes, 0.0, 1.0), True)
        stem = os.path.splitext(os.path.basename(fill["file"]))[0]
        target = os.path.join(directory, "{0}_{1}.png".format(stem, hashlib.sha1(data).hexdigest()[:12]))
        with open(target, "wb") as handle:
            handle.write(data)
        replaced[str(fill["uid"])] = {"path": target}
    if problems:
        raise RuntimeError("{0} cannot move to its new layout exactly: {1}".format(
            texture_set, "; ".join(problems)))
    return replaced


def _swap(wearers, render_names, target_layer):
    """Exchange the render map's and the target map's coordinates on the Texture Set's faces."""
    done = set()
    for object_reference, polygons in wearers:
        mesh = object_reference.data
        if mesh.as_pointer() in done:
            continue
        done.add(mesh.as_pointer())
        starts = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
        mesh.polygons.foreach_get("loop_start", starts)
        totals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
        mesh.polygons.foreach_get("loop_total", totals)
        corners = numpy.concatenate([numpy.arange(starts[polygon], starts[polygon] + totals[polygon])
                                     for polygon in polygons])
        render = mesh.uv_layers[render_names[mesh.as_pointer()]]
        target = mesh.uv_layers[target_layer]
        first = numpy.empty(len(mesh.loops) * 2, dtype=numpy.float32)
        second = numpy.empty(len(mesh.loops) * 2, dtype=numpy.float32)
        render.uv.foreach_get("vector", first)
        target.uv.foreach_get("vector", second)
        first = first.reshape(-1, 2)
        second = second.reshape(-1, 2)
        held = first[corners].copy()
        first[corners] = second[corners]
        second[corners] = held
        render.uv.foreach_set("vector", first.reshape(-1))
        target.uv.foreach_set("vector", second.reshape(-1))
        mesh.update()


def complete(context, session, generation, frame_of_project, directory):
    """Finish a layout change with Painter's answer; pictures laid out or turned again go
    into ``directory``. Returns one line about it."""
    record = generation.record
    pending = _pending.pop(int(record["request"]), None)
    if pending is None:
        return "Painter answered about {0}, which nothing here is waiting for".format(
            record.get("texture_set"))
    if record.get("refused"):
        raise RuntimeError("Painter cannot change the layout of {0}: {1}".format(
            pending.texture_set, record["refused"]))
    texture_set = pending.texture_set
    target_layer = pending.target_layer
    materials = painted_by(texture_set)
    wearers = wearing(materials)
    _check(texture_set, wearers, target_layer)
    _held_by_painter(context, texture_set, record, frame_of_project)
    table = layouts.table_of_texture_set(texture_set, materials)
    retargeted = layout_module.retargeted(table, target_layer, record["uv_sets_used"],
                                          record["uv_sets_taken"])
    render_names = {object_reference.data.as_pointer(): mesh_publish.render_uv_layer(object_reference.data).name
                    for object_reference, _polygons in wearers}
    tangent_maps = any(entry["kind"] == "tangent" for entry in record["mesh_maps"].values())
    relaid, fills = {}, {}
    if record["mesh_maps"] or record["fills"]:
        extra_layers = sorted({table["extra"][str(fill["index"])]["layer"] for fill in record["fills"]
                               if str(fill["index"]) in table["extra"]})
        triangles = layout_triangles.gather(
            _visible_wearers(context.view_layer, wearers), materials, render_names, target_layer,
            frames=tangent_maps or bool(record["fills"]), extra_layers=extra_layers)
        if triangles is None:
            raise RuntimeError("no visible mesh shows a face of {0}, so there is no surface to lay "
                               "its mesh maps out on".format(texture_set))
        frames = triangles.frames("") if triangles.normal is not None else None
        turning = frames is not None and frames.change()
        known = []

        def green():
            if not known:
                known.append(_green(record, generation, triangles))
            return known[0]

        relaid = _relaid_mesh_maps(record, generation, triangles, frames,
                                   green() if turning and tangent_maps else 1.0)
        if turning and record["fills"]:
            if not directory:
                raise RuntimeError("this .blend has never been saved, so the turned normals of "
                                   "{0} have no textures folder to go to".format(texture_set))
            fills = _turned_fills(record, generation, triangles, frames, green, table, texture_set,
                                  directory)
    made = material_relayout.relay(materials, wearers, render_names, target_layer, directory)
    _swap(wearers, render_names, target_layer)
    layouts.write(materials, retargeted)
    pictures = material_relayout.install(made)
    sent = mesh_publish.publish(
        session.publisher(topic_module.MESH), mesh_publish.scope(context.view_layer),
        frame_of_project, relaid={texture_set: {"chart": retargeted["layout"], "mesh_maps": relaid,
                                                "fills": fills}})
    LOG.info("%s now lays out in what %s held; the old layout lives in %s at UV set(s) %s",
             texture_set, target_layer, target_layer,
             ", ".join(sorted(index for index, entry in retargeted["extra"].items()
                              if entry["layer"] == target_layer)))
    return ("{0} moved to its new layout: {1} mesh map(s), {2} fill normal(s) and {3} material "
            "picture(s) laid out again, surface generation {4} sent".format(
                texture_set, len(relaid), len(fills), pictures, sent.number))
