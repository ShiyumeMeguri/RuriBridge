# -*- coding: utf-8 -*-
"""Moving a Texture Set to another of its UV layouts, here and in Painter as one change.

The change goes from a source UV map to a target UV map, both of the same name on every
mesh wearing a material that paints the Texture Set. The source is the map the Texture
Set is laid out in now: the one those meshes render with, which the material samples and
Painter paints in. On the Texture Set's faces the two maps swap their coordinates -- the
source takes the new layout the target held, the target keeps the old one -- and the
Texture Set's table (``layouts``) states that the old layout lives in the target now. The
same change again swaps them back.

Painter is asked first (``begin``): it says which UV sets the Texture Set's content
reads and hands over its mesh maps, laid out in the layout of the surface it holds,
with that surface's fingerprint, and the normals of every fill laying tangent normals
through a chart. The answer completes the change in one step (``complete``) -- only
when the faces here are still the ones the fingerprint was taken of, since the mesh maps
were laid out on them: the mesh maps are laid out again in the new layout and carried
into its frames, so are the pictures of normals of fills where islands turn, the
coordinates swap, the table is written, and the surface goes to Painter carrying all of
it -- where every fill laid out in the old layout goes on reading it through the UV set
that now holds it. What a substance computes is never laid out again: it stays computed,
read through the old layout, and the line this returns names the fills whose normals
keep the old islands' directions where islands turn.

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

    __slots__ = ("texture_set", "source", "target")

    def __init__(self, texture_set, source, target):
        self.texture_set = texture_set
        self.source = source
        self.target = target


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
    """Every mesh object in a scene of this document some face of which wears one of the
    materials, with those faces: hidden ones too, since they show the same textures."""
    found = []
    seen = set()
    for scene in bpy.data.scenes:
        for object_reference in scene.objects:
            key = object_reference.as_pointer()
            if key in seen or object_reference.type != "MESH":
                continue
            seen.add(key)
            polygons = _texture_set_polygons(object_reference, materials)
            if len(polygons):
                found.append((object_reference, polygons))
    return found


def problems(texture_set, source, target):
    """Everything that stands in the way of moving the Texture Set from the layout ``source``
    holds to the one ``target`` holds, one short line each; empty when nothing does."""
    found = []
    if not source or not target:
        found.append("Pick a Source UV and a Target UV")
    elif source == target:
        found.append("Source UV and Target UV are the same map")
    if texture_set in waiting():
        found.append("{0} waits for Painter's answer about its layout".format(texture_set))
    wearers = wearing(painted_by(texture_set))
    if not wearers:
        found.append("No mesh in a scene wears {0}".format(texture_set))
    faces_of_mesh = {}
    for object_reference, polygons in wearers:
        mesh = object_reference.data
        if object_reference.library is not None or mesh.library is not None:
            found.append("{0}: comes from a library, its UV maps cannot change here".format(
                object_reference.name))
            continue
        missing = [name for name in (source, target) if name and name not in mesh.uv_layers]
        if missing:
            found.append("{0}: no UV map {1}".format(
                object_reference.name, " or ".join(repr(name) for name in missing)))
        render = mesh_publish.render_uv_layer(mesh)
        if source and source in mesh.uv_layers and render.name != source:
            found.append("{0}: renders with {1!r}, so its textures are laid out in {1!r}, not {2!r}".format(
                object_reference.name, render.name, source))
        key = mesh.as_pointer()
        if key in faces_of_mesh and not numpy.array_equal(faces_of_mesh[key], polygons):
            found.append("{0}: shares its mesh with faces of another Texture Set".format(
                object_reference.name))
        faces_of_mesh[key] = polygons
    return found


def begin(session, texture_set, source, target):
    """Ask Painter about moving a Texture Set from the layout ``source`` holds to the one
    ``target`` holds. Returns one line about it."""
    found = problems(texture_set, source, target)
    if found:
        raise RuntimeError("; ".join(found))
    generation = session.publisher(topic_module.REQUEST).publish_record(record_module.request(
        "Blender", record_module.ASK_FOR_LAYOUT, texture_set=texture_set))
    _pending[generation.number] = Pending(texture_set, source, target)
    return "asked Painter to prepare {0} to move from {1!r} to {2!r}".format(texture_set, source, target)


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
    replaces the map), and that blend commutes with turning green over. Painter rendered the
    exports with one leaning normal over the whole normal channel and one level over the
    whole height, so at every texel the OpenGL export of the combined normal is the blend of
    the stored lanes -- or that blend with its green turned over. Which one, is read off
    every texel that leans."""
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
    texels, _owners, _weights = chart_resample.rasterize(triangles.render, columns, rows)
    inside = numpy.zeros(rows * columns, dtype=bool)
    inside[texels] = True
    usable = inside.reshape(rows, columns) & (numpy.abs(predicted[..., 1]) > 0.05)
    if usable.sum() < 64:
        raise RuntimeError("no texel of {0} leans enough to show which way its stored normals point "
                           "their green, and its turning islands need it".format(record["texture_set"]))
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


def _restored_fills(record, layout):
    """The fills whose own normals, from before the bridge laid them out anew, are right in
    ``layout``: they take them back instead of being laid out again."""
    return sorted(int(fill["uid"]) for fill in record["fills"]
                  if fill["restorable"] is not None and fill["restorable"] == layout)


def _relaid_fills(record, generation, triangles, frames, green, table, texture_set, directory, restored):
    """The pictures of Painter's fills laying normals through a chart, wherever those bend
    under turning islands, written into ``directory`` under names their bytes give them: for
    a fill laying nothing but pictures, every picture laid out in the new layout, the normals
    carried into its frames; for any other, its picture of normals turned where each texel
    lies, in the chart the fill reads. A picture of normals is its own file, read the way
    Painter takes it, or Painter's render of it, both laid out in that chart, and is written
    the way Painter takes one it has never seen; any other picture is its own file, written
    as it was stored -- one past 0..1 in a channel that holds such values is refused, a
    picture file holding none. Normals a substance computes stay computed, a tile's stay laid
    by the tile, and pictures other Texture Sets show too stay as they are, their islands not
    moving: those bending
    under islands turning from the chart they read -- the layout they were made in -- are
    named, each with the most, in degrees, its normals point off.
    Returns the pictures by fill, those names, and the most, in degrees, a picture turned
    where its texels lie parts from itself at texels two islands of the chart share, which
    hold the later island's."""
    turning = frames.turning()
    replaced = {}
    kept = []
    apart = 0.0
    known = []

    def written():
        if not known:
            fresh = record["convention"]["fresh"]
            render, _wide = pixels.read(str(generation.path(fresh["render"])))
            known.append(green() * _taken(str(generation.path(fresh["picture"])), render))
        return known[0]

    for fill in record["fills"]:
        if int(fill["uid"]) in restored:
            continue
        declared = table["extra"].get(str(fill["index"])) if fill["index"] else None
        layout = triangles.extra[declared["layer"]] if declared else triangles.render
        values, _wide = pixels.read(fill["file"] or str(generation.path(fill["render"])))
        unturned = fill["untouched"] or bool(set(fill["members"]) - {texture_set})
        carried = triangles.reading(declared["layer"] if declared else "") if unturned else frames
        if not _bent(values, layout, carried.turning() if unturned else turning):
            continue
        taken = 1.0
        if fill["file"]:
            reading, _wide = pixels.read(str(generation.path(fill["reading"])))
            taken = _taken(fill["file"], reading)
        opaque = bool((values[..., 3] == 1.0).all())
        lanes = values[..., :3] if opaque else values
        if unturned:
            kept.append((fill["name"], chart_resample.turned_by(lanes, layout, carried, green() * taken,
                                                                fill["turn"])))
            continue
        if not directory:
            raise RuntimeError("this .blend has never been saved, so the normals of {0} turned into "
                               "the new frames have no textures folder to go to".format(texture_set))
        laid = chart_resample.relaid(lanes, layout, triangles.target if fill["pixels"] else layout, "tangent",
                                     frames, green() * taken, written())
        stem = (os.path.splitext(os.path.basename(fill["file"]))[0] if fill["file"]
                else "{0}_{1}".format(texture_set, fill["uid"]))
        pictures = {"Normal": _write_picture(laid, True, stem, directory)}
        if not fill["pixels"]:
            apart = max(apart, chart_resample.turned_apart(lanes, layout, frames, green() * taken))
        for picture in fill["pictures"]:
            values, wide = pixels.read(picture["file"])
            if picture["floating"] and (float(values.min()) < 0.0 or float(values.max()) > 1.0):
                raise RuntimeError("{0}: its {1} picture holds values past 0..1, which no picture file the "
                                   "bridge writes can carry".format(fill["name"], picture["channel"]))
            stem = os.path.splitext(os.path.basename(picture["file"]))[0]
            opaque = bool((values[..., 3] == 1.0).all())
            pictures[picture["channel"]] = _write_picture(chart_resample.relaid(
                values[..., :3] if opaque else values, layout, triangles.target, "value"), wide, stem, directory)
        replaced[str(fill["uid"])] = {"pictures": pictures, "pixels": bool(fill["pixels"])}
    return replaced, kept, apart


def _write_picture(values, wide, stem, directory):
    """Write lanes in 0..1 as a PNG into ``directory`` under a name its bytes give it."""
    data = pixels.png(numpy.clip(values, 0.0, 1.0), wide)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "{0}_{1}.png".format(stem, hashlib.sha1(data).hexdigest()[:12]))
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def _relaid_pictures(record, triangles, texture_set, directory):
    """The pictures Painter's effects read with no projection of their own, laid out again
    in the new layout -- labels from the nearest texel -- and written into ``directory``
    under names their bytes give them. Refuses, by name, a picture an effect other Texture
    Sets show too reads."""
    replaced = {}
    problems = []
    os.makedirs(directory, exist_ok=True)
    for entry in record["pictures"]:
        others = sorted(set(entry["members"]) - {texture_set})
        if others:
            problems.append("{0}: {1} show(s) it too, and their islands do not move with "
                            "these".format(entry["name"], ", ".join(others)))
            continue
        values, wide = pixels.read(entry["file"])
        laid = chart_resample.relaid(values, triangles.render, triangles.target,
                                     "label" if entry["label"] else "value")
        replaced[entry["key"]] = {"path": _write_picture(laid, wide, os.path.splitext(
            os.path.basename(entry["file"]))[0], directory)}
    if problems:
        raise RuntimeError("{0} cannot move to its new layout exactly: {1}".format(
            texture_set, "; ".join(problems)))
    return replaced


def _swap(wearers, render_names, target_layer):
    """Exchange the source map's -- the render map's -- and the target map's coordinates on
    the Texture Set's faces."""
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
    target_layer = pending.target
    found = problems(texture_set, pending.source, target_layer)
    if found:
        raise RuntimeError("{0} cannot move to {1!r} any more: {2}".format(
            texture_set, target_layer, "; ".join(found)))
    materials = painted_by(texture_set)
    wearers = wearing(materials)
    _held_by_painter(context, texture_set, record, frame_of_project)
    table = layouts.table_of_texture_set(texture_set, materials)
    retargeted = layout_module.retargeted(
        table, pending.source, target_layer,
        [(entry["uid"], int(entry["index"]), set(entry["members"]), set(entry["following"]))
         for entry in record["readers"]],
        record["tables"], texture_set)
    render_names = {object_reference.data.as_pointer(): pending.source for object_reference, _polygons in wearers}
    tangent_maps = any(entry["kind"] == "tangent" for entry in record["mesh_maps"].values())
    relaid, fills, pictures, kept, apart = {}, {}, {}, [], 0.0
    restored = _restored_fills(record, retargeted["layout"])
    if record["mesh_maps"] or record["fills"] or record["pictures"]:
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

        if retargeted["layout"] not in record["kept"]:
            relaid = _relaid_mesh_maps(record, generation, triangles, frames,
                                       green() if turning and tangent_maps else 1.0)
        if record["fills"]:
            fills, kept, apart = _relaid_fills(record, generation, triangles, frames, green, table,
                                               texture_set, directory, set(restored))
        if record["pictures"]:
            if not directory:
                raise RuntimeError("this .blend has never been saved, so the pictures of {0} laid out "
                                   "anew have no textures folder to go to".format(texture_set))
            pictures = _relaid_pictures(record, triangles, texture_set, directory)
    made = material_relayout.relay(materials, wearers, render_names, target_layer, directory)
    _swap(wearers, render_names, target_layer)
    layouts.write(materials, retargeted)
    installed = material_relayout.install(made)
    sent = mesh_publish.publish(
        session.publisher(topic_module.MESH), mesh_publish.scope(context.view_layer),
        frame_of_project, relaid={texture_set: {"chart": retargeted["layout"], "mesh_maps": relaid,
                                                "fills": fills, "pictures": pictures,
                                                "restored": restored}})
    LOG.info("%s now lays out in what %s held, in %s; the old layout lives in %s at UV set(s) %s",
             texture_set, target_layer, pending.source, target_layer,
             ", ".join(sorted(index for index, entry in retargeted["extra"].items()
                              if entry["layer"] == target_layer)))
    line = ("{0} moved from {1!r} to {2!r}: {3} mesh map(s), {4} effect picture(s) and {5} material "
            "picture(s) laid out again, {6} picture(s) of normals turned into the new frames, {7} fill(s) "
            "took their own normals back, surface generation {8} sent".format(
                texture_set, pending.source, target_layer, len(relaid), len(pictures), installed, len(fills),
                len(restored), sent.number))
    if kept:
        named = ", ".join("{0} (off by up to {1:.3g} degrees)".format(name, degrees) for name, degrees in kept)
        LOG.info("%s: where islands turned, the normals of %s keep the directions the old islands gave "
                 "them", texture_set, named)
        line += "; where islands turned, the normals of {0} keep the directions the old islands gave them".format(
            named)
    if apart:
        LOG.info("%s: texels two islands of the old layout share part by up to %.3g degrees once turned",
                 texture_set, apart)
        line += "; texels two islands of the old layout share part by up to {0:.3g} degrees once turned".format(
            apart)
    return line
