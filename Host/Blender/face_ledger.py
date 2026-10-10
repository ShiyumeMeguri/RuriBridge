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
  event carried is a guest of the Texture Set holding that event's folder -- Painter states
  which one does, and from which Texture Set the paint came (``held``).
* ``ruri_bridge_painted_uv`` (face corner, vector): where the face lies in the layout of the
  Texture Set its paint lives in, as x and y.

What the record says of the faces in scope decides what a surface does (``survey``): a Texture
Set whose own faces all paint one name Painter does not have yet takes that name -- a material
renamed here -- every layer kept; faces whose paint lives in another Texture Set's stack are
carried there; and the faces of an event that now paint another Texture Set than the one
holding its folder take the folder along. The record changes only once Painter holds the surface
that changes it (``Pending``): Painter states the fingerprints of the surface it holds, and a
surface that did not go in leaves the record as it was, so the next one finds the same faces to
carry. A rename Painter made is written at once, everywhere (``rename``). A mesh from a library is
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
    """What the record says of the faces in scope: the Texture Sets that take a new name with
    this surface, ``{old: new}``; the faces that moved and no event carried yet, ``{(source,
    target): faces}``, sources by the names they take; the events whose faces leave the Texture Set
    holding their folder, ``{event: (holder, target)}``; the guests that stay, ``{target: {event:
    faces}}``; the Texture Sets some face paints into (``painting``); the faces laid out anew in
    place, by object, ``{object: faces}``; and what stands in the way of sending them, one line
    each."""

    __slots__ = ("renames", "moves", "rehomes", "guests", "painting", "relaid", "problems")

    def __init__(self):
        self.renames = {}
        self.moves = {}
        self.rehomes = {}
        self.guests = {}
        self.painting = set()
        self.relaid = {}
        self.problems = []

    @property
    def carries(self):
        """Whether Painter has to act before the surface goes: a rename, or paint to carry."""
        return bool(self.renames or self.moves or self.rehomes)


def _counted(pairs, into):
    """Add the rows of an (n, 2) array of integers, counted, into ``{(first, second): count}``."""
    if not len(pairs):
        return
    rows, counts = numpy.unique(pairs, axis=0, return_counts=True)
    for (first, second), count in zip(rows.tolist(), counts.tolist()):
        into[(first, second)] = into.get((first, second), 0) + count


def survey(objects, slot_texture_sets_of, held, names, render_coordinates_of, renames=None):
    """Read the record of every mesh the objects wear. ``slot_texture_sets_of`` names, for an
    object, the Texture Set each slot paints into; ``held`` gives every event Painter holds a
    folder for, ``{event: (Texture Set holding it, Texture Set its paint came from)}``; ``names``
    are the Texture Sets a moved face's paint may live in -- the ones the project has;
    ``render_coordinates_of`` gives a mesh's render UV per corner; ``renames`` are renames asked
    for besides the ones the faces make.

    A Texture Set takes a new name when every face of its own in scope paints into that name and
    none paints into the old one, and the project has no Texture Set of that name yet: a
    material renamed here. Of two taking one name, the one with more faces does. Paint that lives
    in a Texture Set the project no longer has cannot follow its faces: they arrive bare, as faces
    that never crossed, and the log says so. The faces of one event leave the Texture Set holding
    its folder together or not at all."""
    found = Survey()
    by_digest = {digest(name): name for name in names}
    own = {}
    carried = {}
    seen = set()
    for object_reference in objects:
        mesh = object_reference.data
        slots = slot_texture_sets_of(object_reference)
        now = current(mesh, slots)
        used = set(numpy.unique(now).tolist()) - {0}
        found.painting |= {name for name in slots if name and digest(name) in used}
        if mesh.as_pointer() in seen or not keeps(mesh):
            continue
        seen.add(mesh.as_pointer())
        names_of_slots = {digest(name): name for name in slots if name}
        values = painted(mesh)
        relaid = relaid_in_place(mesh, slots, render_coordinates_of(mesh))
        if relaid:
            found.relaid[object_reference.name] = relaid
        source, event = values[:, SOURCE], values[:, EVENT]
        crossed = (source != 0) & (now != 0)
        guest = crossed & (event != 0) & (source != now)
        _counted(numpy.stack((source, now), axis=1)[crossed & ~guest], own)
        pairs = {}
        _counted(numpy.stack((event, now), axis=1)[guest], pairs)
        for (number, target), count in pairs.items():
            listed = carried.setdefault(str(number), {})
            listed[names_of_slots[target]] = listed.get(names_of_slots[target], 0) + count
    named = {digest(name): name for name in found.painting}
    targets_of = {}
    lost = 0
    for (source, target), count in own.items():
        name = by_digest.get(source)
        if name is None:
            lost += count if source != target else 0
            continue
        listed = targets_of.setdefault(name, {})
        listed[named[target]] = listed.get(named[target], 0) + count
    found.renames = dict(renames or {})
    claims = {}
    for old, targets in sorted(targets_of.items()):
        if old in found.renames or old in found.painting or len(targets) != 1:
            continue
        (new, count), = targets.items()
        if new not in by_digest.values() and new not in found.renames.values():
            claims.setdefault(new, []).append((count, old))
    for new, claimants in sorted(claims.items()):
        found.renames[max(claimants)[1]] = new
    for old, targets in targets_of.items():
        source = found.renames.get(old, old)
        for target, count in targets.items():
            if target != source:
                found.moves[(source, target)] = found.moves.get((source, target), 0) + count
    for event, targets in sorted(carried.items()):
        if event not in held:
            found.problems.append("faces carried into {0} by an event this project holds no folder for; open "
                                  "the project they were carried in".format(", ".join(sorted(targets))))
            continue
        holder = found.renames.get(held[event][0], held[event][0])
        if set(targets) == {holder}:
            found.guests.setdefault(holder, {})[event] = targets[holder]
        elif len(targets) == 1:
            found.rehomes[event] = (holder, next(iter(targets)))
        else:
            found.problems.append("faces carried into {0} together now paint {1}; they can leave it only "
                                  "together".format(holder, ", ".join(sorted(targets))))
    if lost:
        LOG.warning("%d face(s) were painted in a Texture Set the project no longer has; they arrive bare", lost)
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


def rename(renames):
    """Texture Sets renamed, old name to new, all at once: every face of every mesh of the document
    whose paint lived in one lives in its new name."""
    digests = {digest(old): digest(new) for old, new in renames.items()}
    if not digests:
        return
    for mesh in bpy.data.meshes:
        if not keeps(mesh) or PAINTED_ATTRIBUTE not in mesh.attributes:
            continue
        values = painted(mesh)
        renamed = values[:, SOURCE].copy()
        for before, after in digests.items():
            renamed[values[:, SOURCE] == before] = after
        if (renamed != values[:, SOURCE]).any():
            values[:, SOURCE] = renamed
            mesh.attributes[PAINTED_ATTRIBUTE].data.foreach_set("value", values.reshape(-1))


def new_event(taken):
    """An event nobody in ``taken`` uses."""
    while True:
        event = random.randrange(1, 1 << 31)
        if str(event) not in taken:
            return event
