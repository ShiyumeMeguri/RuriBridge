# -*- coding: utf-8 -*-
"""Where each face's paint lives in the texturing project, kept on the faces themselves.

Painter's layers belong to a Texture Set, so a face painted in one Texture Set that paints into
another now -- its material changed, or its material paints elsewhere -- shows the other stack
and leaves its paint behind. To carry the paint along (``guest_carry``), this side keeps, for
every face, which Texture Set its paint lives in and where in that layout it lies: the surface
Painter holds, as the bridge sent it. Two attributes on every mesh that crossed, so the record
goes wherever the face goes -- through joins, separations, duplicates, deformation, modifiers:

* ``ruri_bridge_painted`` (face, two integers): the Texture Set the face's paint lives in, as a
  32-bit digest of its name -- zero for a face that never crossed -- and the **event** that
  carried the face into the Texture Set it paints into now, zero while its paint lives where it
  paints. A face whose paint lives elsewhere and that no event carried yet has moved; one an
  event carried is a guest of the Texture Set that event belongs to (``GUESTS_PROPERTY``).
* ``ruri_bridge_painted_uv`` (face corner, vector): where the face lies in the layout of the
  Texture Set its paint lives in, as x and y.

The record changes only once Painter holds the surface that changes it (``Pending``): Painter
states the fingerprints of the surface it holds, and a surface that did not go in leaves the
record as it was, so the next one finds the same faces to carry. A mesh from a library is
never written; its faces count as never crossed.
"""

from __future__ import annotations

import random
import zlib

import bpy
import numpy

from ...Kernel.log import logger

LOG = logger("blender.ledger")

#: Keys written into .blend files, so they never change spelling.
PAINTED_ATTRIBUTE = "ruri_bridge_painted"
PAINTED_UV_ATTRIBUTE = "ruri_bridge_painted_uv"
#: On every material painting a Texture Set: the events that carried faces into it and the
#: source each brought them from, ``{event: source Texture Set}``.
GUESTS_PROPERTY = "ruri_bridge_guests"

#: Where a face's paint lives (digest), and the event that carried it.
SOURCE = 0
EVENT = 1


def digest(name):
    """A Texture Set's name as the record keeps it: the 32 bits of its CRC-32, signed."""
    value = zlib.crc32(name.encode("utf-8"))
    if value == 0:
        raise RuntimeError("the Texture Set name {0!r} digests to zero, which the bridge keeps for faces that "
                           "never crossed; rename it".format(name))
    return value - (1 << 32) if value >= (1 << 31) else value


def keeps(mesh):
    """Whether this mesh can keep a record: one of this document, not a library's."""
    return mesh.library is None and mesh.override_library is None


def _checked(attribute, data_type, domain):
    if attribute is not None and (attribute.data_type != data_type or attribute.domain != domain):
        raise RuntimeError("{0} is a {1} attribute on the {2} domain; the bridge keeps it as {3} on {4}".format(
            attribute.name, attribute.data_type, attribute.domain, data_type, domain))
    return attribute


def painted(mesh):
    """Per face, where its paint lives and the event that carried it, (faces, 2) int32; zero where
    it never crossed."""
    values = numpy.zeros(len(mesh.polygons) * 2, dtype=numpy.int32)
    attribute = _checked(mesh.attributes.get(PAINTED_ATTRIBUTE), "INT32_2D", "FACE")
    if attribute is not None:
        attribute.data.foreach_get("value", values)
    return values.reshape(-1, 2)


def painted_coordinates(mesh):
    """Per corner, where the face lies in the layout its paint is laid out in, (corners, 2) float32."""
    values = numpy.zeros(len(mesh.loops) * 3, dtype=numpy.float32)
    attribute = _checked(mesh.attributes.get(PAINTED_UV_ATTRIBUTE), "FLOAT_VECTOR", "CORNER")
    if attribute is not None:
        attribute.data.foreach_get("vector", values)
    return values.reshape(-1, 3)[:, :2]


def current(mesh, slot_texture_sets):
    """Per face, the digest of the Texture Set it paints into now; zero where it paints into none.
    ``slot_texture_sets`` names it per material slot, empty for a slot kept out."""
    indices = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", indices)
    if not slot_texture_sets:
        return numpy.zeros(len(indices), dtype=numpy.int32)
    digests = numpy.array([digest(name) if name else 0 for name in slot_texture_sets], dtype=numpy.int32)
    return digests[numpy.minimum(indices, len(digests) - 1)]


def guests(painted_values, now):
    """Per face, whether its paint lives in another Texture Set than the one it paints into."""
    return (painted_values[:, SOURCE] != 0) & (painted_values[:, SOURCE] != now) & (now != 0)


#: How far apart, in UV units, two coordinates whole tiles apart may lie and still be the same
#: point of the picture: the rounding of a 32-bit float at a few tiles out.
_SAME_POINT = 1e-5


def tile_shifted(mesh, slot_texture_sets, render):
    """Per face, whether it paints where its paint lives and lies where it lay when it last crossed
    but for whole UV tiles. A picture repeats across UV space, so such a face shows the same texels;
    only paint Painter casts at the UV coordinates it was made at -- strokes made in the 2D view --
    tells the tiles apart, and it lies on the face only where the face crossed before."""
    values = painted(mesh)
    now = current(mesh, slot_texture_sets)
    recorded = (values[:, SOURCE] != 0) & (values[:, SOURCE] == now) & (values[:, EVENT] == 0)
    if not recorded.any():
        return recorded
    delta = render.astype(numpy.float64) - painted_coordinates(mesh)
    offset = numpy.round(delta)
    faces = corner_faces(mesh)
    starts = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_start", starts)
    same_point = numpy.abs(delta - offset).max(axis=1) <= _SAME_POINT
    same_tile = (offset == offset[starts[faces]]).all(axis=1)
    whole = numpy.ones(len(mesh.polygons), dtype=bool)
    numpy.logical_and.at(whole, faces, same_point & same_tile)
    moved = numpy.zeros(len(mesh.polygons), dtype=bool)
    numpy.logical_or.at(moved, faces, (offset != 0).any(axis=1))
    return recorded & whole & moved


def crossing(mesh, slot_texture_sets, render):
    """The coordinates a mesh's faces cross with in its render UV, per corner: where it lies now,
    but at the tiles it lay at when it last crossed for a face moved by whole tiles only
    (``tile_shifted``)."""
    shifted = tile_shifted(mesh, slot_texture_sets, render)
    if not shifted.any():
        return render
    corners = shifted[corner_faces(mesh)]
    values = render.copy()
    values[corners] = painted_coordinates(mesh)[corners]
    return values


def relaid_in_place(mesh, slot_texture_sets, render):
    """How many faces paint where their paint lives and lie elsewhere than where it was laid out,
    other than by whole tiles: laid out anew in place, not through Retarget Layout."""
    values = painted(mesh)
    now = current(mesh, slot_texture_sets)
    recorded = (values[:, SOURCE] != 0) & (values[:, SOURCE] == now) & (values[:, EVENT] == 0)
    if not recorded.any():
        return 0
    delta = numpy.abs(render.astype(numpy.float64) - painted_coordinates(mesh)).max(axis=1)
    moved = numpy.zeros(len(mesh.polygons), dtype=bool)
    numpy.logical_or.at(moved, corner_faces(mesh), delta > _SAME_POINT)
    return int((recorded & moved & ~tile_shifted(mesh, slot_texture_sets, render)).sum())


# -- which faces moved --------------------------------------------------------------------------------

class Survey:
    """What the record says of the faces in scope: the faces that moved and no event carried yet,
    ``{(source, target): faces}``; the guests, ``{target: {event: faces}}``; the faces laid out
    anew in place, by object, ``{object: faces}``; and what stands in the way of sending them, one
    line each."""

    __slots__ = ("moves", "guests", "relaid", "problems")

    def __init__(self):
        self.moves = {}
        self.guests = {}
        self.relaid = {}
        self.problems = []


def survey(objects, slot_texture_sets_of, events_of, names, render_coordinates_of):
    """Read the record of every mesh the objects wear. ``slot_texture_sets_of`` names, for an
    object, the Texture Set each slot paints into; ``events_of`` gives a Texture Set's events;
    ``names`` are the Texture Sets a moved face's paint may live in -- the ones the project has;
    ``render_coordinates_of`` gives a mesh's render UV per corner. Paint that lives in a Texture
    Set the project no longer has cannot follow its faces: they arrive bare, as faces that never
    crossed, and the log says so."""
    found = Survey()
    by_digest = {digest(name): name for name in names}
    lost = {}
    seen = set()
    for object_reference in objects:
        mesh = object_reference.data
        if mesh.as_pointer() in seen or not keeps(mesh):
            continue
        seen.add(mesh.as_pointer())
        slots = slot_texture_sets_of(object_reference)
        names_of_slots = {digest(name): name for name in slots if name}
        now = current(mesh, slots)
        values = painted(mesh)
        relaid = relaid_in_place(mesh, slots, render_coordinates_of(mesh))
        if relaid:
            found.relaid[object_reference.name] = relaid
        for face in numpy.flatnonzero(guests(values, now)):
            source, event = int(values[face, SOURCE]), int(values[face, EVENT])
            target = names_of_slots[int(now[face])]
            if event:
                if str(event) in events_of(target):
                    found.guests.setdefault(target, {}).setdefault(event, 0)
                    found.guests[target][event] += 1
                else:
                    found.problems.append(
                        "{0}: faces carried into another Texture Set moved again, into {1}; move them back or "
                        "keep them where they were carried".format(object_reference.name, target))
                continue
            name = by_digest.get(source)
            if name is None:
                lost[object_reference.name] = lost.get(object_reference.name, 0) + 1
                continue
            found.moves[(name, target)] = found.moves.get((name, target), 0) + 1
    for name, count in sorted(lost.items()):
        LOG.warning("%s: %d face(s) were painted in a Texture Set the project no longer has; they arrive bare",
                    name, count)
    found.problems = sorted(set(found.problems))
    return found


# -- the record once Painter holds the surface ---------------------------------------------------------

class Pending:
    """The record as it stands once Painter holds a surface: per mesh, by name, its face and corner
    counts when the surface was sent and the values to write, and the fingerprints Painter states
    once it holds that surface."""

    __slots__ = ("fingerprints", "meshes")

    def __init__(self, fingerprints, meshes):
        self.fingerprints = dict(fingerprints)
        self.meshes = meshes


def settled(objects, slot_texture_sets_of, render_coordinates_of, events, names, fresh=False):
    """The record of every mesh the objects wear once Painter holds the surface: a face that paints
    where its paint lives -- or never crossed -- lives there, where it crosses now (``crossing``:
    a face moved by whole tiles keeps the tiles it lay at); one that moved is
    carried by the event this surface makes for its source and target, ``events`` ``{(source,
    target): event}``, and keeps where it lay; a guest stays as it is; a face kept out of the
    texturing project, or whose paint lives in a Texture Set the project no longer has (``names``
    are the ones it has), starts over -- every face does, ``fresh``, for a project the surface
    starts. ``render_coordinates_of`` gives a mesh's render UV per corner. Returns ``{mesh name:
    (faces, corners, painted, coordinates)}``."""
    by_digest = {digest(name): name for name in names}
    found = {}
    for object_reference in objects:
        mesh = object_reference.data
        if mesh.name in found or not keeps(mesh):
            continue
        slots = slot_texture_sets_of(object_reference)
        names_of_slots = {digest(name): name for name in slots if name}
        now = current(mesh, slots)
        values = painted(mesh).copy()
        coordinates = painted_coordinates(mesh).copy()
        moved = guests(values, now) & (values[:, EVENT] == 0)
        for face in numpy.flatnonzero(moved):
            name = by_digest.get(int(values[face, SOURCE]))
            if name is None:
                moved[face] = False
                continue
            values[face, EVENT] = events[(name, names_of_slots[int(now[face])])]
        staying = ~guests(values, now) | ((values[:, EVENT] == 0) & ~moved)
        if fresh:
            staying[:] = True
        values[staying, SOURCE] = now[staying]
        values[staying, EVENT] = 0
        laid_now = staying[corner_faces(mesh)]
        coordinates[laid_now] = crossing(mesh, slots, render_coordinates_of(mesh))[laid_now]
        found[mesh.name] = (len(mesh.polygons), len(mesh.loops), values, coordinates)
    return found


def corner_faces(mesh):
    """The face each corner of the mesh belongs to."""
    starts = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_start", starts)
    totals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    offsets = numpy.cumsum(totals) - totals
    within = numpy.arange(int(totals.sum()), dtype=numpy.int64) - numpy.repeat(offsets, totals)
    found = numpy.empty(len(mesh.loops), dtype=numpy.int64)
    found[numpy.repeat(starts, totals) + within] = numpy.repeat(numpy.arange(len(starts)), totals)
    return found


def commit(pending):
    """Write the record a surface Painter now holds leaves. A mesh whose faces or corners changed
    since keeps its record, and the log says so: its faces are surveyed again with the next
    surface."""
    written = 0
    for name, (faces, corners, values, coordinates) in sorted(pending.meshes.items()):
        mesh = bpy.data.meshes.get(name)
        if mesh is None or len(mesh.polygons) != faces or len(mesh.loops) != corners or not keeps(mesh):
            LOG.warning("%s changed since the surface was sent; its record stays as it was", name)
            continue
        attribute = _checked(mesh.attributes.get(PAINTED_ATTRIBUTE), "INT32_2D", "FACE")
        if attribute is None:
            attribute = mesh.attributes.new(PAINTED_ATTRIBUTE, "INT32_2D", "FACE")
        attribute.data.foreach_set("value", numpy.ascontiguousarray(values, dtype=numpy.int32).reshape(-1))
        layer = _checked(mesh.attributes.get(PAINTED_UV_ATTRIBUTE), "FLOAT_VECTOR", "CORNER")
        if layer is None:
            layer = mesh.attributes.new(PAINTED_UV_ATTRIBUTE, "FLOAT_VECTOR", "CORNER")
        vectors = numpy.zeros((corners, 3), dtype=numpy.float32)
        vectors[:, :2] = coordinates
        layer.data.foreach_set("vector", vectors.reshape(-1))
        mesh.update()
        written += 1
    return written


def rename(old, new):
    """A Texture Set renamed: every face whose paint lives in it lives in the new name, and every
    event that brought faces from it brought them from the new name."""
    before, after_name = digest(old), digest(new)
    for mesh in bpy.data.meshes:
        if not keeps(mesh) or PAINTED_ATTRIBUTE not in mesh.attributes:
            continue
        values = painted(mesh)
        hit = values[:, SOURCE] == before
        if hit.any():
            values[hit, SOURCE] = after_name
            mesh.attributes[PAINTED_ATTRIBUTE].data.foreach_set("value", values.reshape(-1))
    for material in bpy.data.materials:
        events = material.get(GUESTS_PROPERTY)
        if material.library is None and events is not None and old in dict(events).values():
            material[GUESTS_PROPERTY] = {str(event): new if str(source) == old else str(source)
                                         for event, source in dict(events).items()}


# -- the events of a Texture Set ------------------------------------------------------------------------

def events_of_materials(materials):
    """The events that carried faces into the Texture Set these materials paint, ``{event: source}``."""
    found = {}
    for material in materials:
        for event, source in dict(material.get(GUESTS_PROPERTY) or {}).items():
            found[str(event)] = str(source)
    return found


def write_events(materials, events):
    """Make every material painting one Texture Set state these events."""
    for material in materials:
        if events:
            material[GUESTS_PROPERTY] = {str(event): str(source) for event, source in sorted(events.items())}
        elif GUESTS_PROPERTY in material.keys():
            del material[GUESTS_PROPERTY]


def new_event(taken):
    """An event nobody in ``taken`` uses."""
    while True:
        event = random.randrange(1, 1 << 31)
        if str(event) not in taken:
            return event
