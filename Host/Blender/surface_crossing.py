# -*- coding: utf-8 -*-
"""The one way a surface goes to Painter, and the record of where every face's paint lives.

Every surface -- Update Mesh, a layout change, a carry -- goes through ``send``: it brings, for
every Texture Set holding faces whose paint lives in another one, a picture per event of where
those faces lie now (the masks of the folders carrying their paint, ``guest_state`` over there),
and every other picture the step made for Painter to take in -- all of them beside the record
(``Beside``), transport as the mesh maps are, never files in the document's folders -- and it
leaves the record ``face_ledger`` keeps waiting until Painter holds the surface
(``settle``): Painter states the fingerprints of the surface it holds, and only a surface that
went in changes the record.
"""

from __future__ import annotations

import hashlib

import bpy
import numpy

from ...Kernel import layout as layout_module
from ...Kernel import topic as topic_module
from ...Kernel.log import logger

from . import chart_resample, face_ledger, layout_triangles, layouts, mesh_publish, pixels

LOG = logger("blender.crossing")

#: The record a sent surface leaves, waiting for Painter to hold it.
_settling = [None]


def slot_texture_sets(object_reference):
    """The Texture Set each material slot of an object paints into, empty for none."""
    return [mesh_publish.texture_set_of(slot.material) if slot.material is not None else ""
            for slot in object_reference.material_slots]


def events_of(texture_set):
    """The events that carried faces into a Texture Set, ``{event: source}``."""
    return face_ledger.events_of_materials(mesh_publish.painted_by(texture_set))


def render_coordinates(mesh):
    """The render UV of a mesh per corner, (corners, 2)."""
    layer = mesh_publish.render_uv_layer(mesh)
    values = numpy.zeros(len(mesh.loops) * 2, dtype=numpy.float32)
    if layer is not None:
        layer.uv.foreach_get("vector", values)
    return values.reshape(-1, 2)


def project_names(painter):
    """The Texture Sets the project Painter has open holds, none when it has none open."""
    if not painter.get("document"):
        return []
    return [entry["name"] for entry in painter.get("texture_sets") or []]


# -- the surface as triangles ---------------------------------------------------------------------------

class Surface:
    """Every triangle of the faces in scope as they cross: the Texture Set it paints into
    (``target``, an index into ``names``, -1 for none), where its paint lives (``source``, a
    digest) and the event that carried it, its corners in the render UV (``render``) and where its
    paint is laid out (``painted``, the render UV for a face whose paint lives where it paints), and
    when asked its corner normals and the MikkTSpace frames both give, ``(tangent, sign)``."""

    __slots__ = ("names", "target", "source", "event", "render", "painted", "normal", "render_frames",
                 "painted_frames")

    def __init__(self, names, parts):
        self.names = names
        for key in self.__slots__[1:]:
            values = parts.get(key)
            if key.endswith("_frames"):
                setattr(self, key, tuple(numpy.concatenate(part) for part in zip(*values)) if values else None)
            else:
                setattr(self, key, numpy.concatenate(values) if values else None)

    def of(self, name):
        """Which triangles paint into a Texture Set."""
        if name not in self.names or self.target is None:
            return numpy.zeros(0 if self.target is None else len(self.target), dtype=bool)
        return self.target == self.names.index(name)

    def frames(self, chosen, before, after):
        """The frames a tangent normal at the chosen triangles is carried between, ``before`` and
        ``after`` each ``render`` or ``painted``."""
        first = getattr(self, before + "_frames")
        second = getattr(self, after + "_frames")
        return chart_resample.Frames(self.normal[chosen], first[0][chosen], first[1][chosen],
                                     second[0][chosen], second[1][chosen])


def gather(objects, frames):
    """The triangles of the surface as it crosses (``Surface``); ``frames`` asks for the frames."""
    names = []
    parts = {key: [] for key in Surface.__slots__[1:]}
    with mesh_publish.surface_only(objects):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph.update()
        for object_reference in objects:
            slots = slot_texture_sets(object_reference)
            if not any(slots):
                continue
            evaluated = object_reference.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                count = len(mesh.loop_triangles)
                if not count or mesh_publish.render_uv_layer(mesh) is None:
                    continue
                loops = numpy.empty(count * 3, dtype=numpy.int64)
                mesh.loop_triangles.foreach_get("loops", loops)
                loops = loops.reshape(-1, 3)
                polygons = numpy.empty(count, dtype=numpy.int64)
                mesh.loop_triangles.foreach_get("polygon_index", polygons)
                indices = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
                mesh.polygons.foreach_get("material_index", indices)
                slot = numpy.minimum(indices, len(slots) - 1)[polygons]
                for name in slots:
                    if name and name not in names:
                        names.append(name)
                slot_target = numpy.array([names.index(name) if name else -1 for name in slots], dtype=numpy.int64)
                values = face_ledger.painted(mesh)
                render = render_coordinates(mesh)
                painted = mesh_publish.painted_values(mesh, slots, render)
                parts["target"].append(slot_target[slot])
                parts["source"].append(values[polygons, face_ledger.SOURCE].astype(numpy.int64))
                parts["event"].append(values[polygons, face_ledger.EVENT].astype(numpy.int64))
                parts["render"].append(render[loops])
                parts["painted"].append(painted[loops])
                if frames:
                    normals = numpy.empty(len(mesh.loops) * 3, dtype=numpy.float32)
                    mesh.corner_normals.foreach_get("vector", normals)
                    normals = normals.reshape(-1, 3)
                    parts["normal"].append(normals[loops])
                    found = layout_triangles.frames_of(mesh, loops, normals, {"render": render, "painted": painted})
                    parts["render_frames"].append(found["render"])
                    parts["painted_frames"].append(found["painted"])
            finally:
                evaluated.to_mesh_clear()
    return Surface(names, parts)


# -- the pictures a surface brings ----------------------------------------------------------------------

class Beside:
    """The pictures the bridge makes for Painter to take in -- masks, paint laid out as pixels,
    pictures laid out anew -- crossing beside the record of the surface they go with, under names
    their bytes give them. They are transport, as the mesh maps are: Painter holds them until the
    project embeds them, and nothing of them lands in the document's folders."""

    __slots__ = ("files",)

    def __init__(self):
        self.files = {}

    def picture(self, values, wide, stem):
        """Lanes in 0..1 as a PNG, 16 bits a lane when ``wide``; returns the reference a record
        keeps, ``{"file", "hash"}``."""
        data = pixels.png(numpy.clip(values, 0.0, 1.0), wide)
        digest = hashlib.sha1(data).hexdigest()
        name = "{0}_{1}.png".format(stem, digest[:12])
        self.files[name] = data
        return {"file": name, "hash": digest}


# -- the masks ------------------------------------------------------------------------------------------

#: How far past an island a mask carries the island's value: the texels on its border that the
#: island only partly covers, and one more.
_MASK_RIM = 2


def mask(render, chosen, others, size):
    """A picture of where the chosen triangles lie in their Texture Set's layout, ``size`` (width,
    height): white on them, black on the rest of the Texture Set's faces, and a rim past every
    island taking the island's value, so a texel an island only partly covers reads it."""
    width, height = size
    values = numpy.zeros(width * height)
    covered = numpy.zeros(width * height, dtype=bool)
    texels, _owners, _weights = chart_resample.rasterize(render[others], width, height)
    covered[texels] = True
    texels, _owners, _weights = chart_resample.rasterize(render[chosen], width, height)
    values[texels] = 1.0
    covered[texels] = True
    return chart_resample.pad_near(values.reshape(height, width, 1), covered.reshape(height, width), _MASK_RIM)


def guests(surface, painter, beside, events, resolutions):
    """For every Texture Set holding faces whose paint lives in another one once this surface is in,
    its painted UV set and a mask per event, ``{target: {"index", "events": {event: {"source",
    "mask"}}}}``: the faces each event carried, ``events`` ``{(source, target): event}`` naming the
    ones this surface carries, each mask a picture ``beside`` the record. An event whose faces are
    all gone keeps a black mask while Painter holds its folder. ``resolutions`` gives the size of a
    Texture Set Painter does not have yet."""
    known = {entry["name"]: entry for entry in painter.get("texture_sets") or []}
    held = {name: set(listed) for name, listed in dict(painter.get("guests") or {}).items()}
    payload = {}
    if surface.target is None:
        return payload
    after = surface.event.copy()
    for (source, target), event in events.items():
        movers = (surface.event == 0) & (surface.source == face_ledger.digest(source)) & surface.of(target)
        after[movers] = event
    for target in surface.names:
        here = surface.of(target)
        registry = events_of(target)
        listed = {str(event) for event in numpy.unique(after[here]) if event}
        listed |= {event for event in registry if event in held.get(target, set())}
        listed |= {str(event) for (_source, carried_into), event in events.items() if carried_into == target}
        if not listed:
            continue
        table = layouts.table_of_texture_set(target, mesh_publish.painted_by(target))
        index = layout_module.painted_index(table)
        if index is None:
            raise RuntimeError("{0} holds faces whose paint lives in another Texture Set, and its table has no "
                               "UV set for them".format(target))
        size = tuple(known[target]["resolution"]) if target in known else tuple(resolutions[target])
        sources = dict(registry)
        sources.update({str(event): source for (source, carried_into), event in events.items()
                        if carried_into == target})
        entry = {"index": index, "events": {}}
        for event in sorted(listed):
            chosen = here & (after == int(event))
            if event not in sources:
                raise RuntimeError("faces of {0} were carried by an event it has no record of".format(target))
            picture = mask(surface.render, chosen, here & ~chosen, size)
            entry["events"][event] = {"source": sources[event],
                                      "mask": beside.picture(picture, False, "{0}_guests_{1}".format(target, event))}
        payload[target] = entry
    return payload


# -- sending, and the record once Painter holds it --------------------------------------------------------

def send(session, context, frame_of_project, painter, beside, relaid=None, carry=None, events=None,
         resolutions=None):
    """Send the surface in the project's frame with the masks of its guests and the pictures
    ``beside`` it (``Beside``), and keep the record it leaves until Painter holds it. ``events`` are
    the events this surface carries faces by, ``{(source, target): event}``; ``resolutions`` the
    sizes of Texture Sets Painter does not have yet. Returns the generation."""
    objects = mesh_publish.scope(context.view_layer)
    events = dict(events or {})
    names = project_names(painter)
    payload = {}
    if names:
        surface = gather(objects, frames=False)
        if surface.target is not None and (events or (surface.event != 0).any() or painter.get("guests")):
            payload = guests(surface, painter, beside, events, dict(resolutions or {}))
    generation = mesh_publish.publish(session.publisher(topic_module.MESH), objects, frame_of_project,
                                      beside.files, relaid=relaid, guests=payload, carry=carry)
    meshes = face_ledger.settled(objects, slot_texture_sets, render_coordinates, events, names, fresh=not names)
    _settling[0] = face_ledger.Pending(generation.record["fingerprints"], meshes)
    return generation


def settle(painter):
    """Painter stated what it holds: once that is the surface the waiting record describes, write the
    record. Returns one line about it, empty while it waits."""
    pending = _settling[0]
    if pending is None:
        return ""
    surface = dict(painter.get("surface") or {})
    if any(surface.get(name) != fingerprint for name, fingerprint in pending.fingerprints.items()):
        return ""
    _settling[0] = None
    written = face_ledger.commit(pending)
    LOG.info("Painter holds the surface: the record of where %d mesh(es) are painted follows it", written)
    return "Painter holds the surface; {0} mesh(es) remember where their faces are painted".format(written)


def waiting():
    """Whether a sent surface's record waits for Painter to hold it."""
    return _settling[0] is not None
