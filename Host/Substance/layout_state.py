# -*- coding: utf-8 -*-
"""Keeping everything laid out in UV space reading the coordinates it was laid out in.

A fill whose source is a picture or a substance placed one to one on the UVs -- an
identity UV transformation -- holds content made for one layout: a flattened mask, a
baked or imported texture, the bridge's own layer of the Blender material. When the
Texture Set moves to another layout it keeps its pixels and reads them through the
old coordinates instead, with Painter's own UV-set-to-UV-set projection, so nothing
is resampled into a picture that is not the one somebody made. What does not depend
on the layout is left to Painter: strokes and polygon fills are re-applied in 3D when
the surface comes in, and the 3D projections never read UVs at all.

Tangent-space normals are directions in the frame an island's UVs give the surface, and
Painter decodes the normal channel in the frames of set 0, copying normals read through
another UV set as they are: where an island turns or mirrors in the new layout, values
read through the old chart point elsewhere on the surface. What a substance computes
stays computed -- it reads the old chart like every other channel, and where islands
turn its normals keep the directions the old islands gave them; nothing renders it into
a picture. A picture is pixels, and pixels move: a picture of normals goes to Blender as
its own file where Painter still knows it, else as Painter reads it (``fills``). A fill
laying nothing but pictures and uniform colours through set 0, every other picture of it
still a file on disk, takes every picture laid out in the new layout -- its normals
carried from their old frames into the new ones -- and goes on reading set 0: islands
that shared texels in the old layout take each their own. Any other fill keeps reading
the chart it reads, and takes its picture of normals turned where each texel lies, from
the frame the island reading it had into the one it has now: what Painter renders of a
picture is the picture only for normals, so nothing else is laid out from a render. A fill whose normals a full Replace at the bottom
of its content covers, or whose normal channel is disabled, lays none of its own. The
pictures a fill laid first are remembered with the layout they are right in
(``NORMALS_KEY``): moving back to that layout gives the fill those pictures back --
nothing laid out twice.

A picture that is a Texture Set's own mesh map is not laid out in a chart at all, in
that Texture Set: Painter shows it there as whatever the mesh map is, and swaps it when
the map is swapped -- so it follows the layout, as the mesh maps do (``following``).

An effect that reads a picture with no projection of its own -- a colour selection's id
mask, an image input of a generator or a filter -- reads it in set 0, laid out in the
layout the Texture Set has. A Texture Set's own mesh map it reads follows the map, as
a fill's does; any other picture goes to Blender, comes back laid out in the new layout,
and takes its place (``pictures``).

A fill whose UV transformation tiles, turns or offsets its source is a tile: a pattern
laid across UV space. It reads the old layout like any other fill, its transformation
kept, so the pattern lies where it lay on the surface; its normals are left as they are.
Painter turns the normals of a turned tile laid through set 0 and not of one read through
another UV set, so a turned tile laying normals that other Texture Sets show too stays
laid through set 0: reading another UV set would turn its normals in them as well.

A generator in a mask computes from the mesh maps of the layout it computes in. One that
shows every image input its package declares -- none of them bound by Painter itself from
the layout of set 0, as a UV island mask is -- and that no other Texture Set shows goes on
computing in the old layout: the bridge lays in its place a fill of the same substance,
with its inputs, parameters, blending and opacity, read through the old layout, its mesh
map inputs bound to the old layout's maps (``CARRIED_KEY``). Moving back to that layout
makes it a generator again. Any other generator computes in the layout of set 0.

Each Texture Set's chart table (``Kernel.layout``) comes from Blender with every
surface; the tables applied last live in the project, beside the layers they
describe. Before a surface goes in, ``plan`` works out where every chart-addressed
fill reads from afterwards -- a fill shared through instance layers is one node read
by several Texture Sets, and has to keep each of them where it was -- and refuses,
naming the layers, when that is impossible. The mesh maps belong to set 0, so a
Texture Set that changes layout takes the maps laid out in its new chart: the ones
Blender sends with the surface, or the ones the project kept for that chart from
before. Maps of a chart the project no longer reads are not kept.
"""

from __future__ import annotations

import json
import lzma
import os
import struct
import zlib

import substance_painter.colormanagement as colormanagement
import substance_painter.export
import substance_painter.js
import substance_painter.layerstack as layerstack
import substance_painter.resource
import substance_painter.source as source_module
import substance_painter.textureset as textureset

from ...Kernel import layout as layout_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

from . import held_imports, project_facts, project_imports, substance_package

LOG = logger("painter.layout")

#: Per Texture Set: the chart table applied last, and per chart the mesh maps laid out
#: in it, ``{usage: {"name": ..., "version": ...}}``.
LAYOUTS_KEY = "layouts"

#: Per Texture Set, the fingerprint of the surface the project holds (see the mesh
#: record): what its mesh maps are laid out on.
SURFACE_KEY = "surface"

#: Per fill whose pictures were laid out anew, by uid, the pictures it laid first and the
#: layout they are right in, ``{"layout", "pixels", "pictures": {channel: {"name",
#: "version"}}}`` -- ``pixels`` when the fill took them laid out in the new layout and reads
#: set 0 for them.
NORMALS_KEY = "normals"

#: Per fill the bridge laid in a generator's place, by uid, the Texture Set it shows in and
#: the layout set 0 held when it was a generator, ``{"texture_set", "layout"}``: moving back
#: to that layout makes it a generator again.
CARRIED_KEY = "carried"

#: Painter's mesh maps: the name its JavaScript export knows each by, and how its
#: values change when the layout under them changes -- a tangent-space normal turns
#: with the tangent frame, an id is a label no texel may blend, the rest are values.
MESH_MAPS = {
    textureset.MeshMapUsage.Normal: ("normal_base", "tangent"),
    textureset.MeshMapUsage.BentNormals: ("bent_normals", "tangent"),
    textureset.MeshMapUsage.WorldSpaceNormal: ("world_space_normals", "value"),
    textureset.MeshMapUsage.ID: ("id", "label"),
    textureset.MeshMapUsage.AO: ("ambient_occlusion", "value"),
    textureset.MeshMapUsage.Curvature: ("curvature", "value"),
    textureset.MeshMapUsage.Position: ("position", "value"),
    textureset.MeshMapUsage.Thickness: ("thickness", "value"),
    textureset.MeshMapUsage.Height: ("height", "value"),
    textureset.MeshMapUsage.Opacity: ("opacity", "value"),
}

#: A normal leaning along both axes, so neither can be mistaken for the other: the one a
#: picture Painter has never seen holds, to read off how Painter takes a fresh picture of
#: normals (``_readings``), and the one laid over the whole normal channel while the
#: project's convention is read (``_convention_maps``).
_LEANING_NORMAL = (0.65, 0.8, 0.87)
#: The height laid over the whole height channel while the project's convention is read.
_LEVEL_HEIGHT = 0.5

_SPATIAL_SOURCES = (source_module.SourceBitmap, source_module.SourceSubstance,
                    source_module.SourceVectorial, source_module.SourceFont)
_TOLERANCE = 1e-6


# -- which fills read which chart ------------------------------------------------------

class Addressed:
    """One fill that reads its source through a chart, every Texture Set showing it, the
    pictures it reads, where its tangent normals come from (``normal_content``) when it
    lays any and whether it lays nothing but pictures and uniform colours through set 0
    (``pixels``), the Texture Sets whose own mesh map it reads (``following``), whether it
    is a generator the bridge carries as a fill (``generator``), and for a tile the degrees
    its transformation turns its source by (``tiled``, ``turn``)."""

    __slots__ = ("uid", "name", "index", "members", "layer", "normal", "pixels", "pictures", "following",
                 "generator", "tiled", "turn")

    def __init__(self, uid, name, index, layer, normal, pixels, pictures, generator, tiled, turn):
        self.uid = uid
        self.name = name
        self.index = index
        self.members = set()
        self.layer = layer
        self.normal = normal
        self.pixels = pixels
        self.pictures = pictures
        self.following = set()
        self.generator = generator
        self.tiled = tiled
        self.turn = turn


def _turn(transformation):
    """The degrees a UV transformation turns its source by, in [-180, 180)."""
    return ((transformation.rotation or 0.0) + 180.0) % 360.0 - 180.0


def _identity(transformation):
    return (transformation.scale_mode == layerstack.ScaleMode.Factors
            and all(abs(value - 1.0) <= _TOLERANCE for value in transformation.scale)
            and abs(_turn(transformation)) <= _TOLERANCE
            and all(abs(value) <= _TOLERANCE for value in transformation.offset))


def _sources(node):
    mode = node.source_mode
    if mode == source_module.SourceMode.Material:
        return [node.get_material_source()]
    if mode == source_module.SourceMode.Split:
        return [node.get_source(channel) for channel in node.active_channels]
    return [node.get_source(None)]


def _reads_stack(source):
    """Whether a source reads the layer stack itself, which is laid out in set 0."""
    if isinstance(source, source_module.SourceReference):
        return True
    if isinstance(source, source_module.SourceSubstance):
        return any(_reads_stack(source.get_source(identifier)) for identifier in source.image_inputs)
    return False


def _pictures(source):
    """The pictures a source reads, by name and version, a substance's inputs included."""
    if isinstance(source, source_module.SourceBitmap):
        return {(source.resource_id.name, source.resource_id.version)}
    if isinstance(source, source_module.SourceSubstance):
        found = set()
        for identifier in source.image_inputs:
            inner = source.get_source(identifier)
            if inner is not None:
                found |= _pictures(inner)
        return found
    return set()


def chart_index(node):
    """The UV set a fill reads its source through, or None when it reads no chart: a 3D
    projection, a fill of uniform colours, or one reading the stack itself."""
    mode = node.get_projection_mode()
    if mode == layerstack.ProjectionMode.UVSetToUVSet:
        index = int(node.get_projection_parameters().source_uv_set or 0)
    elif mode == layerstack.ProjectionMode.UV:
        index = 0
    else:
        return None
    sources = [source for source in _sources(node) if source is not None]
    if not any(isinstance(source, _SPATIAL_SOURCES) for source in sources):
        return None
    if any(_reads_stack(source) for source in sources):
        return None
    return index


def normal_content(node):
    """Where a fill in a channel stack takes tangent normals from: ``("bitmap", url)`` for a
    picture, ``("procedural", "")`` for anything computed, None when it lays no normal or
    a uniform one."""
    if textureset.ChannelType.Normal not in set(node.active_channels):
        return None
    if node.source_mode == source_module.SourceMode.Material:
        return ("procedural", "")
    source = node.get_source(textureset.ChannelType.Normal)
    if source is None or isinstance(source, source_module.SourceUniformColor):
        return None
    if isinstance(source, source_module.SourceBitmap):
        return ("bitmap", source.resource_id.url())
    return ("procedural", "")


class Unprojected:
    """A picture an effect reads in set 0 with no projection of its own -- a colour
    selection's id mask (``slot`` empty), or the image input ``slot`` of a generator or a
    filter -- whether its values are labels no texel may blend, and the Texture Sets showing
    the effect."""

    __slots__ = ("uid", "name", "slot", "url", "label", "members")

    def __init__(self, uid, name, slot, url, label):
        self.uid = uid
        self.name = name
        self.slot = slot
        self.url = url
        self.label = label
        self.members = set()

    @property
    def key(self):
        return "{0}:{1}".format(self.uid, self.slot)


def _unprojected_of(node):
    """The pictures an effect reads with no projection of its own."""
    if isinstance(node, layerstack.ColorSelectionEffectNode):
        mask = node.get_parameters().id_mask
        return [] if mask is None else [Unprojected(node.uid(), node.get_name(), "", mask.url(), True)]
    if not isinstance(node, (layerstack.GeneratorEffectNode, layerstack.FilterEffectNode)):
        return []
    source = node.get_source()
    if not isinstance(source, source_module.SourceSubstance):
        return []
    found = []
    for identifier in source.image_inputs:
        inner = source.get_source(identifier)
        if isinstance(inner, source_module.SourceBitmap):
            found.append(Unprojected(node.uid(), node.get_name(), identifier, inner.resource_id.url(), False))
    return found


def _covered_normal(node):
    """Whether the bottom of a fill layer's content lays normals in place of the fill's own,
    at full strength, so the fill shows none of its own."""
    effects = node.content_effects()
    if not effects:
        return False
    bottom = effects[-1]
    return (isinstance(bottom, layerstack.FillEffectNode) and bottom.is_visible()
            and textureset.ChannelType.Normal in set(bottom.active_channels)
            and bottom.get_blending_mode(textureset.ChannelType.Normal) == layerstack.BlendingMode.Replace
            and abs(bottom.get_opacity(textureset.ChannelType.Normal) - 1.0) <= _TOLERANCE)


def _laid_normal(node, in_mask):
    """Where a fill takes the normals it shows from, or None when it shows none of its own."""
    if in_mask:
        return None
    normal = normal_content(node)
    if normal is None:
        return None
    if node.get_blending_mode(textureset.ChannelType.Normal) == layerstack.BlendingMode.Disable:
        return None
    if isinstance(node, layerstack.FillLayerNode) and _covered_normal(node):
        return None
    return normal


def _carriable(node):
    """Whether a generator can go on computing in another layout: Painter shows every image
    input its package declares, binding none of them itself from the layout of set 0, and
    none of them reads the stack."""
    source = node.get_source()
    if not isinstance(source, source_module.SourceSubstance) or _reads_stack(source):
        return False
    path = _file_of(source.resource_id.url())
    if not path:
        return False
    try:
        declared = substance_package.image_inputs(path, source.resource_id.name.rsplit("/", 1)[-1])
    except (ValueError, lzma.LZMAError):
        return False
    return declared <= set(source.image_inputs)


def _pictures_only(node):
    """Whether a fill lays nothing but pictures and uniform colours: nothing it lays is computed."""
    return all(isinstance(source, (source_module.SourceBitmap, source_module.SourceUniformColor))
               for source in _sources(node) if source is not None)


def _visit(nodes, member, found, unplaceable, unprojected, in_mask=False):
    for node in nodes:
        if not isinstance(node, layerstack.LayerNode):
            uid = node.uid()
            if uid not in unprojected:
                unprojected[uid] = _unprojected_of(node)
            for entry in unprojected[uid]:
                entry.members.add(member)
        if isinstance(node, (layerstack.FillLayerNode, layerstack.FillEffectNode)):
            uid = node.uid()
            if uid not in found:
                mode = node.get_projection_mode()
                if mode == layerstack.ProjectionMode.Fill:
                    unplaceable.setdefault(uid, (node.get_name(), set()))
                    found[uid] = None
                else:
                    index = chart_index(node)
                    normal = None if index is None else _laid_normal(node, in_mask)
                    transformation = None if index is None else node.get_projection_parameters().uv_transformation
                    plain = index is not None and _identity(transformation)
                    found[uid] = None if index is None else Addressed(
                        uid, node.get_name(), index, isinstance(node, layerstack.FillLayerNode), normal,
                        normal is not None and normal[0] == "bitmap" and index == 0 and plain and _pictures_only(node),
                        set().union(*(_pictures(source) for source in _sources(node) if source is not None)),
                        False, not plain, 0.0 if plain else _turn(transformation))
            if found[uid] is not None:
                found[uid].members.add(member)
            elif uid in unplaceable:
                unplaceable[uid][1].add(member)
        if in_mask and isinstance(node, layerstack.GeneratorEffectNode):
            uid = node.uid()
            if uid not in found:
                found[uid] = (Addressed(uid, node.get_name(), 0, False, None, False, set(), True, False, 0.0)
                              if _carriable(node) else None)
            if found[uid] is not None:
                found[uid].members.add(member)
        if isinstance(node, layerstack.LayerNode):
            _visit(node.content_effects(), member, found, unplaceable, unprojected, in_mask)
            _visit(node.mask_effects(), member, found, unplaceable, unprojected, True)
            if isinstance(node, layerstack.GroupLayerNode):
                _visit(node.sub_layers(), member, found, unplaceable, unprojected, in_mask)
            if isinstance(node, layerstack.InstanceLayerNode):
                _visit([node.instance_source()], member, found, unplaceable, unprojected, in_mask)


def _mesh_map_pictures(texture_set):
    found = set()
    for usage in MESH_MAPS:
        resource = texture_set.get_mesh_map_resource(usage)
        if resource is not None:
            found.add((resource.name, resource.version))
    return found


def readers():
    """Every chart-addressed fill in the project, with the Texture Sets that show it and
    those whose own mesh map it reads -- Painter shows such a picture, in that Texture
    Set, as whatever its mesh map is, and swaps it along with the map -- and every
    generator the bridge can carry as such a fill; the fills projected per UV tile, which
    no chart can carry, with theirs; and every picture an effect it does not carry reads
    with no projection of its own. A generator other Texture Sets show too stays a
    generator, its mesh map inputs being each Texture Set's own; so does a turned tile
    laying normals they show."""
    found = {}
    unplaceable = {}
    unprojected = {}
    mesh_maps = {}
    for texture_set in textureset.all_texture_sets():
        mesh_maps[texture_set.name] = _mesh_map_pictures(texture_set)
        for stack in texture_set.all_stacks():
            _visit(layerstack.get_root_layer_nodes(stack), texture_set.name, found, unplaceable, unprojected)
    fills = [entry for entry in found.values() if entry is not None and not (
        len(entry.members) > 1 and (entry.generator or (entry.turn and entry.normal is not None)))]
    for fill in fills:
        fill.following = (set() if fill.generator else
                          {member for member in fill.members if fill.pictures & mesh_maps.get(member, set())})
    carried = {fill.uid for fill in fills if fill.generator}
    return fills, unplaceable, [entry for uid, entries in unprojected.items() if uid not in carried
                                for entry in entries]


# -- what the project applied last ------------------------------------------------------

def applied():
    """Every Texture Set's applied state: its chart table and the mesh maps kept per chart."""
    return {name: {"layout": str(state.get("layout") or ""),
                   "extra": {str(index): str(chart) for index, chart in dict(state.get("extra") or {}).items()},
                   "mesh_maps": {str(chart): dict(maps) for chart, maps in dict(state.get("mesh_maps") or {}).items()}}
            for name, state in dict(project_facts.read(LAYOUTS_KEY) or {}).items()}


def _tables(states):
    return {name: {"layout": state["layout"], "extra": state["extra"]} for name, state in states.items()}


def _current_mesh_maps(texture_set):
    held = {}
    for usage in MESH_MAPS:
        resource = texture_set.get_mesh_map_resource(usage)
        if resource is not None:
            held[usage.name] = {"name": resource.name, "version": resource.version}
    return held


# -- planning a surface ------------------------------------------------------------------

class Plan:
    """What one surface changes, worked out before anything moves."""

    __slots__ = ("moves", "desired", "states", "relayouts", "nodes", "fingerprints", "fills", "pictures",
                 "unprojected", "restored", "generators")

    def __init__(self, moves, desired, states, relayouts, nodes, fingerprints, fills, pictures, unprojected,
                 restored, generators):
        self.moves = moves
        self.desired = desired
        self.states = states
        self.relayouts = relayouts
        self.nodes = nodes
        self.fingerprints = fingerprints
        self.fills = fills
        self.pictures = pictures
        self.unprojected = unprojected
        self.restored = restored
        self.generators = generators

    @property
    def changes_anything(self):
        return bool(self.moves or self.relayouts)


def plan(record, directory):
    """Where everything addressed through a chart reads from once this surface is in.
    Raises ``LayoutError`` naming what cannot be kept, before anything moves. Mesh maps
    delivered with the surface are held at once: the transport retires its files on
    its own schedule, and Painter reads an import's file only when it first needs it."""
    desired = {name: layout_module.charts(table)
               for name, table in dict(record.get("layouts") or {}).items()}
    states = applied()
    before = _tables(states)
    changed = sorted(name for name in desired
                     if desired[name] != layout_module.charts(before.get(name)))
    fills, unplaceable, unprojected = readers()
    moves = layout_module.rebind([(fill.uid, fill.index, fill.members, fill.following) for fill in fills],
                                 before, desired, int(record.get("uv_sets") or 1))
    problems = []
    for uid, (name, members) in sorted(unplaceable.items()):
        if members & set(changed):
            problems.append("layer {0} is projected per UV tile and {1} changes layout; set "
                            "its projection to UV first".format(name, ", ".join(sorted(members))))
    relayouts = {}
    for name in changed:
        old = (before.get(name) or layout_module.empty())["layout"]
        new = desired[name]["layout"]
        if old == new:
            continue
        texture_set = textureset.TextureSet.from_name(name) if name in {
            one.name for one in textureset.all_texture_sets()} else None
        if texture_set is not None and texture_set.has_uv_tiles():
            problems.append("{0} is laid out in UV tiles, which a layout change cannot carry".format(name))
            continue
        delivered = dict(record.get("relaid") or {}).get(name)
        kept = (states.get(name) or {}).get("mesh_maps", {}).get(new)
        if kept is not None:
            relayouts[name] = ("kept", old, new, kept)
        elif delivered is not None and delivered["chart"] == new and delivered["mesh_maps"]:
            relayouts[name] = ("delivered", old, new, {
                usage: dict(entry, held=held_imports.hold(os.path.join(directory, entry["file"]),
                                                          entry["hash"]))
                for usage, entry in delivered["mesh_maps"].items()})
        elif texture_set is not None and _current_mesh_maps(texture_set):
            problems.append("{0} moves to a layout nobody laid its mesh maps out in; retarget it "
                            "from Blender, which sends them with the surface".format(name))
        else:
            relayouts[name] = ("none", old, new, {})
    replaced = {}
    restored = []
    known = {fill.uid: fill for fill in fills}
    records = dict(project_facts.read(NORMALS_KEY) or {})
    for name, entry in dict(record.get("relaid") or {}).items():
        layout = (before.get(name) or layout_module.empty())["layout"]
        for uid, replacement in dict(entry.get("fills") or {}).items():
            fill = known.get(int(uid))
            if fill is None or fill.normal is None:
                problems.append("{0}: the layer Blender laid the normals of ({1}) out for is gone "
                                "or no longer lays normals".format(name, uid))
                continue
            replaced[int(uid)] = dict(replacement, layout=layout)
        for uid in entry.get("restored") or []:
            if int(uid) not in known or str(uid) not in records:
                problems.append("{0}: the layer whose own normals were to come back ({1}) is gone or "
                                "has nothing remembered".format(name, uid))
                continue
            restored.append(int(uid))
    pictures = {}
    held = {entry.key: entry for entry in unprojected}
    for name, entry in dict(record.get("relaid") or {}).items():
        for key, replacement in dict(entry.get("pictures") or {}).items():
            if key not in held:
                problems.append("{0}: the effect whose picture Blender laid out anew ({1}) is gone or "
                                "reads no picture there any more".format(name, key))
                continue
            pictures[key] = replacement
    if problems:
        raise layout_module.LayoutError("; ".join(problems))
    nodes = {fill.uid: fill.name for fill in fills}
    generators = {fill.uid: next(iter(fill.members)) for fill in fills if fill.generator}
    return Plan(moves, desired, states, relayouts, nodes, dict(record.get("fingerprints") or {}),
                replaced, pictures, held, restored, generators)


# -- applying it -----------------------------------------------------------------------------

def _bind(uid, index):
    node = layerstack.get_node_by_uid(uid)
    old = node.get_projection_parameters()
    if index == 0:
        node.set_projection_parameters(layerstack.UVProjectionParams(
            filtering_mode=old.filtering_mode, uv_wrapping_mode=old.uv_wrapping_mode,
            uv_transformation=old.uv_transformation))
        return
    node.set_projection_parameters(layerstack.UVSetToUVSetProjectionParams(
        source_uv_set=index, filtering_mode=old.filtering_mode,
        uv_wrapping_mode=old.uv_wrapping_mode, uv_transformation=old.uv_transformation))


def before_surface(chosen):
    """Moves that read set 0 again go first: set 0 is on every surface."""
    for uid, index in sorted(chosen.moves.items()):
        if index == 0 and uid not in chosen.generators:
            _bind(uid, index)


def _import_delivered(name, chart, files):
    maps = {}
    for usage_name, entry in sorted(files.items()):
        resource = project_imports.take_in(entry["held"], substance_painter.resource.Usage.TEXTURE,
                                           name="{0}_{1}_{2}".format(name, usage_name, chart[:8]))
        identifier = resource.identifier()
        maps[usage_name] = {"name": identifier.name, "version": identifier.version}
    return maps


def _assign(texture_set, maps):
    for usage in MESH_MAPS:
        entry = maps.get(usage.name)
        texture_set.set_mesh_map_resource(
            usage, None if entry is None else substance_painter.resource.ResourceID.from_project(
                entry["name"], entry["version"]))


class Applied:
    """What a surface changed: one line about it, the Texture Sets that moved to another
    layout, and per Texture Set the mesh maps the new layout's replaced, ``{usage: (old,
    new)}`` with each as ``{"name": ..., "version": ...}``."""

    __slots__ = ("line", "relaid", "replaced")

    def __init__(self, line, relaid, replaced):
        self.line = line
        self.relaid = relaid
        self.replaced = replaced


def _restore(uid, records):
    """Give a fill back the pictures it laid first, in the layout they are right in, reading
    set 0 again where it read pictures laid out in the new layout."""
    remembered = records.pop(str(uid))
    node = layerstack.get_node_by_uid(uid)
    for name, picture in sorted(remembered["pictures"].items()):
        node.set_source(getattr(textureset.ChannelType, name),
                        substance_painter.resource.ResourceID.from_project(picture["name"], picture["version"]))
    if remembered["pixels"]:
        _bind(uid, 0)


def _turned(uid, replacement, records):
    """Give a fill the pictures Blender laid out anew in place of its own: every picture of a
    fill laying nothing but pictures, laid out in the new layout and read through set 0, else
    its picture of normals turned where each texel lies, in the chart the fill goes on reading.
    The picture each channel laid first is remembered, once, with the layout of the first
    change: a channel never laid anew before still lays its own."""
    node = layerstack.get_node_by_uid(uid)
    channels = {name: getattr(textureset.ChannelType, name) for name in replacement["pictures"]}
    record = records.setdefault(str(uid), {"layout": replacement["layout"], "pixels": False, "pictures": {}})
    record["pixels"] = record["pixels"] or bool(replacement["pixels"])
    for name, channel in channels.items():
        if name not in record["pictures"]:
            source = node.get_source(channel)
            record["pictures"][name] = {"name": source.resource_id.name, "version": source.resource_id.version}
    for name, channel in sorted(channels.items()):
        path = replacement["pictures"][name]
        resource = project_imports.take_in(path, substance_painter.resource.Usage.TEXTURE,
                                           name=os.path.splitext(os.path.basename(path))[0])
        node.set_source(channel, resource.identifier())
    if replacement["pixels"]:
        _bind(uid, 0)


def _copied(identifier, inner, target):
    """Give a substance's input ``identifier`` what another's holds."""
    if isinstance(inner, source_module.SourceUniformColor):
        target.set_source(identifier, inner.get_color())
    elif isinstance(inner, source_module.SourceSubstance):
        nested = target.set_source(identifier, inner.resource_id)
        for name in inner.image_inputs:
            _copied(name, inner.get_source(name), nested)
        nested.set_parameters(inner.get_parameters())
    elif inner is not None:
        target.set_source(identifier, inner.resource_id)


def _slots(texture_set):
    """The Texture Set's mesh maps now, by name and version, each with its usage."""
    found = {}
    for usage in MESH_MAPS:
        resource = texture_set.get_mesh_map_resource(usage)
        if resource is not None:
            found[(resource.name, resource.version)] = usage.name
    return found


def _carry(uid, index, texture_set, maps, layout, carried):
    """Lay in a generator's place a fill of the same substance read through UV set ``index``,
    its mesh map inputs bound to ``maps`` -- the mesh maps of ``layout``, the layout it
    computed in -- and all else it has as it was."""
    generator = layerstack.get_node_by_uid(uid)
    source = generator.get_source()
    usages = _slots(texture_set)
    for usage, entry in maps.items():
        usages[(entry["name"], entry["version"])] = usage
    fill = layerstack.insert_fill(layerstack.InsertPosition.above_node(generator))
    procedural = fill.set_source(None, source.resource_id)
    for identifier in source.image_inputs:
        inner = source.get_source(identifier)
        usage = (usages.get((inner.resource_id.name, inner.resource_id.version))
                 if isinstance(inner, source_module.SourceBitmap) else None)
        if usage is not None and usage in maps:
            procedural.set_source(identifier, substance_painter.resource.ResourceID.from_project(
                maps[usage]["name"], maps[usage]["version"]))
        else:
            _copied(identifier, inner, procedural)
    procedural.set_parameters(source.get_parameters())
    procedural.resolution = source.resolution
    fill.set_name(generator.get_name())
    fill.set_blending_mode(generator.get_blending_mode())
    fill.set_opacity(generator.get_opacity())
    fill.set_visible(generator.is_visible())
    _bind(fill.uid(), index)
    carried[str(fill.uid())] = {"texture_set": texture_set.name, "layout": layout}
    layerstack.delete_node(generator)


def _uncarry(uid, texture_set, carried):
    """Make a fill the bridge laid in a generator's place that generator again, now that the
    layout it computed in is set 0's: the inputs Painter binds to the Texture Set's mesh maps
    left to it, all else as the fill has it."""
    carried.pop(str(uid))
    fill = layerstack.get_node_by_uid(uid)
    procedural = fill.get_source(None)
    generator = layerstack.insert_generator_effect(layerstack.InsertPosition.above_node(fill),
                                                   procedural.resource_id)
    source = generator.get_source()
    slots = _slots(texture_set)
    for identifier in procedural.image_inputs:
        own = source.get_source(identifier) if identifier in source.image_inputs else None
        if isinstance(own, source_module.SourceBitmap) and (own.resource_id.name, own.resource_id.version) in slots:
            continue
        _copied(identifier, procedural.get_source(identifier), source)
    source.set_parameters(procedural.get_parameters())
    source.resolution = procedural.resolution
    generator.set_name(fill.get_name())
    generator.set_blending_mode(fill.get_blending_mode())
    generator.set_opacity(fill.get_opacity())
    generator.set_visible(fill.is_visible())
    layerstack.delete_node(fill)


def _read_picture(entry, resource_id):
    """Make an effect read another picture where it read ``entry``'s."""
    node = layerstack.get_node_by_uid(entry.uid)
    if isinstance(node, layerstack.ColorSelectionEffectNode):
        parameters = node.get_parameters()
        parameters.id_mask = resource_id
        node.set_parameters(parameters)
        return
    node.get_source().set_source(entry.slot, resource_id)


def after_surface(chosen):
    """Moves to the other UV sets, the mesh maps of every Texture Set that changed layout,
    the normals and the effects' pictures laid out anew, the generators carried to the
    layout they computed in or back, and the tables now applied."""
    for uid, index in sorted(chosen.moves.items()):
        if index != 0 and uid not in chosen.generators:
            _bind(uid, index)
    records = {key: value for key, value in dict(project_facts.read(NORMALS_KEY) or {}).items()
               if int(key) in chosen.nodes}
    for uid in chosen.restored:
        _restore(uid, records)
    for uid, replacement in sorted(chosen.fills.items()):
        _turned(uid, replacement, records)
    project_facts.write(NORMALS_KEY, records)
    states = dict(chosen.states)
    names = {one.name: one for one in textureset.all_texture_sets()}
    replaced = {}
    for name, (how, old, new, maps) in sorted(chosen.relayouts.items()):
        texture_set = names.get(name)
        if texture_set is None:
            continue
        state = states.setdefault(name, {"layout": "", "extra": {}, "mesh_maps": {}})
        current = _current_mesh_maps(texture_set)
        state.setdefault("mesh_maps", {})[old] = current
        if how == "delivered":
            maps = _import_delivered(name, new, maps)
        state["mesh_maps"][new] = maps
        _assign(texture_set, maps)
        replaced[name] = {usage: (current[usage], maps[usage]) for usage in current if usage in maps}
    carried = {key: value for key, value in dict(project_facts.read(CARRIED_KEY) or {}).items()
               if int(key) in chosen.nodes}
    olds = {name: old for name, (_how, old, _new, _maps) in chosen.relayouts.items()}
    for uid, index in sorted(chosen.moves.items()):
        if uid in chosen.generators and index != 0:
            name = chosen.generators[uid]
            _carry(uid, index, names[name], states[name]["mesh_maps"][olds[name]], olds[name], carried)
        elif str(uid) in carried and index == 0:
            _uncarry(uid, names[carried[str(uid)]["texture_set"]], carried)
    project_facts.write(CARRIED_KEY, carried)
    for key, replacement in sorted(chosen.pictures.items()):
        resource = project_imports.take_in(replacement["path"], substance_painter.resource.Usage.TEXTURE,
                                           name=os.path.splitext(os.path.basename(replacement["path"]))[0])
        _read_picture(chosen.unprojected[key], resource.identifier())
    _settle(states, chosen.desired, names)
    _hold_surface(chosen.fingerprints, names)
    moved = len(chosen.moves)
    relaid = sorted(name for name in chosen.relayouts if name in names)
    if moved or relaid:
        LOG.info("%d fill(s) now read another UV set, %d took their pictures of normals turned "
                 "into the new frames and %d their own back, %d effect(s) a picture laid out anew; "
                 "%d Texture Set(s) took the mesh maps of their new layout", moved, len(chosen.fills),
                 len(chosen.restored), len(chosen.pictures), len(relaid))
    return Applied("{0} fill(s) rebound, {1} with normals turned, {2} Texture Set(s) relaid".format(
        moved, len(chosen.fills), len(relaid)), relaid, replaced)


def _settle(states, desired, names):
    """Write the tables now applied, keeping mesh maps only for charts a table still names."""
    for name, table in desired.items():
        state = states.setdefault(name, {"mesh_maps": {}})
        state["layout"] = table["layout"]
        state["extra"] = dict(table["extra"])
        reachable = {table["layout"]} | set(table["extra"].values())
        state["mesh_maps"] = {chart: maps for chart, maps in dict(state.get("mesh_maps") or {}).items()
                              if chart in reachable}
    project_facts.write(LAYOUTS_KEY, {
        name: state for name, state in states.items()
        if name in names and (state["layout"] or state["extra"] or len(state["mesh_maps"]) > 1)})
    _keep_recorded(states)


def _hold_surface(fingerprints, names):
    project_facts.write(SURFACE_KEY, {name: str(fingerprint) for name, fingerprint
                                      in dict(fingerprints).items() if name in names})


def adopt(layouts, fingerprints):
    """A project just created from a surface starts out applying the tables it came with."""
    desired = {name: layout_module.charts(table) for name, table in dict(layouts).items()}
    names = {one.name for one in textureset.all_texture_sets()}
    _settle({}, desired, names)
    _hold_surface(fingerprints, names)


def read_layout(node):
    """Make a fill read its source through set 0, the Texture Set's own layout."""
    if node.get_projection_mode() == layerstack.ProjectionMode.UVSetToUVSet:
        _bind(node.uid(), 0)


def _keep_recorded(states):
    """Mesh maps kept for a chart that is not the layout still matter: an undo brings the
    chart back; so do the pictures fills will take back in the layout they are right in.
    Every one of them the bridge imported is held out of the after-save sweep."""
    kept = [entry for state in states.values() for maps in dict(state.get("mesh_maps") or {}).values()
            for entry in maps.values()]
    kept += [picture for entry in dict(project_facts.read(NORMALS_KEY) or {}).values()
             for picture in entry["pictures"].values()]
    project_imports.keep(kept)


# -- answering a coming layout change -------------------------------------------------------

def _save_mesh_map(texture_set_name, identifier, path):
    substance_painter.js.evaluate("alg.mapexport.saveMeshMap({0}, {1}, {2}, {{bitDepth: 32}})".format(
        json.dumps(texture_set_name), json.dumps(identifier), json.dumps(path.replace("\\", "/"))))


def _convention_maps(texture_set, directory):
    """The maps that tell which way the stored tangent maps point their green: the combined
    OpenGL normal export and the normal channel it is combined from, with a fill on top of
    everything for as long as they render -- the normal channel replaced by one leaning
    normal, the height by one level, so no bump the content makes stands in the way at any
    texel -- and how the Texture Set combines its normal channel with the mesh map."""
    stack = texture_set.get_stack()
    present = set(stack.all_channels())
    laid = {channel: value for channel, value in (
        (textureset.ChannelType.Normal, colormanagement.Color(*_LEANING_NORMAL)),
        (textureset.ChannelType.Height, colormanagement.Color(_LEVEL_HEIGHT, _LEVEL_HEIGHT, _LEVEL_HEIGHT)))
        if channel in present}
    over = layerstack.insert_fill(layerstack.InsertPosition.from_textureset_stack(stack)) if laid else None
    try:
        if over is not None:
            over.active_channels = set(laid)
            for channel, value in laid.items():
                over.set_source(channel, value)
                over.set_blending_mode(layerstack.BlendingMode.Replace, channel)
                over.set_opacity(1.0, channel)
        return _convention_exports(texture_set, directory)
    finally:
        if over is not None:
            layerstack.delete_node(over)


def _convention_exports(texture_set, directory):
    maps = []
    for name, kind, source, dest in (("probe_normal_gl", "virtualMap", "Normal_OpenGL", "RGB"),
                                     ("probe_channel_normal", "documentMap", "normal", "RGB")):
        maps.append({"fileName": name,
                     "channels": [{"destChannel": one, "srcChannel": one, "srcMapType": kind,
                                   "srcMapName": source} for one in dest],
                     "parameters": {"fileFormat": "exr", "bitDepth": "32f", "dithering": False,
                                    "paddingAlgorithm": "infinite"}})
    configuration = {"exportShaderParams": False, "exportPath": str(directory),
                     "exportPresets": [{"name": "ruri_layout_probe", "maps": maps}],
                     "defaultExportPreset": "ruri_layout_probe",
                     "exportList": [{"rootPath": texture_set.name, "exportPreset": "ruri_layout_probe"}],
                     "exportParameters": [{"parameters": {"paddingAlgorithm": "infinite",
                                                          "dithering": False}}]}
    result = substance_painter.export.export_project_textures(configuration)
    written = {os.path.splitext(os.path.basename(path))[0]: os.path.basename(path)
               for paths in result.textures.values() for path in paths}
    found = {key: written[key] for key in ("probe_normal_gl", "probe_channel_normal") if key in written}
    found["blending"] = substance_painter.js.evaluate(
        "alg.texturesets.structure({0}).additionalNormalMapBlending".format(json.dumps(texture_set.name)))
    return found


def _file_of(url):
    """The file a project resource was imported from, while it is still on disk."""
    info = json.loads(substance_painter.js.evaluate(
        "JSON.stringify(alg.resources.getResourceInfo({0}))".format(json.dumps(url))))
    path = str(info.get("filePath") or "")
    return path if path and os.path.isfile(path) else ""


def _normal_fills(fills, texture_set, staging, records):
    """Every fill of the Texture Set laying tangent normals through a chart, with where they
    come from, laid out in the chart the fill reads: a picture's own file while it is on
    disk, else the normals rendered as the fill lays them -- the picture as Painter reads
    it, or what a substance computes, which Blender only looks at, as it does at a tile's
    (``untouched``, with the degrees a tile turns its source by). A fill laying nothing but
    pictures, every other one of them a file on disk, hands over those files too, each with
    whether the channel it is laid in holds values past 0..1 (``pixels``)."""
    stack = texture_set.get_stack()
    found = []
    for fill in fills:
        if (texture_set.name not in fill.members or texture_set.name in fill.following
                or fill.normal is None):
            continue
        kind, url = fill.normal
        node = layerstack.get_node_by_uid(fill.uid)
        pictures = [{"channel": channel.name, "floating": stack.get_channel(channel).is_floating(),
                     "file": _file_of(node.get_source(channel).resource_id.url())}
                    for channel in sorted(node.active_channels if fill.pixels else (), key=lambda one: one.name)
                    if channel != textureset.ChannelType.Normal
                    and isinstance(node.get_source(channel), source_module.SourceBitmap)]
        moving = fill.pixels and all(picture["file"] for picture in pictures)
        entry = {"uid": fill.uid, "name": fill.name, "index": fill.index, "members": sorted(fill.members),
                 "untouched": kind != "bitmap" or fill.tiled, "turn": fill.turn,
                 "pixels": moving, "pictures": pictures if moving else [],
                 "file": _file_of(url) if kind == "bitmap" else "", "render": "",
                 "restorable": records[str(fill.uid)]["layout"] if str(fill.uid) in records else None}
        if not entry["file"]:
            entry["render"] = "fill_{0}_normal.exr".format(fill.uid)
            _render_own(node, str(staging.path(entry["render"])))
        found.append(entry)
    return found


def _unprojected_pictures(unprojected, texture_set, problems):
    """Every picture an effect of the Texture Set reads with no projection of its own,
    other than its own mesh maps, with the file it is: each is laid out in the Texture Set's
    current layout. A picture that is no file on this computer any more is a problem."""
    own = _mesh_map_pictures(texture_set)
    found = []
    for entry in unprojected:
        if texture_set.name not in entry.members:
            continue
        identity = substance_painter.resource.ResourceID.from_url(entry.url)
        if (identity.name, identity.version) in own:
            continue
        path = _file_of(entry.url)
        if not path:
            problems.append("{0}: {1} is a picture that is no file on this computer any more".format(
                entry.name, "its id mask" if not entry.slot else "its input {0}".format(entry.slot)))
            continue
        found.append({"key": entry.key, "name": entry.name, "label": entry.label,
                      "members": sorted(entry.members), "file": path})
    return found


def _render_own(node, path):
    """The normals a fill lays by itself, rendered over the whole chart it reads: its layer's
    content with every other effect of it hidden, an effect laying its normals in place of
    what lies under it, and a fill reading another UV set reading set 0 for as long as it
    renders -- Painter renders in the layout of set 0, where what is read through another UV
    set covers only the islands set 0 lays out -- all put back afterwards."""
    normal = textureset.ChannelType.Normal
    layer = node if isinstance(node, layerstack.FillLayerNode) else node.get_parent()
    others = [effect for effect in layer.content_effects() if effect.uid() != node.uid() and effect.is_visible()]
    held = None if node is layer else (node.get_blending_mode(normal), node.get_opacity(normal))
    shown = node.is_visible()
    projection = (node.get_projection_parameters()
                  if node.get_projection_mode() == layerstack.ProjectionMode.UVSetToUVSet else None)
    try:
        for effect in others:
            effect.set_visible(False)
        node.set_visible(True)
        if held is not None:
            node.set_blending_mode(layerstack.BlendingMode.Replace, normal)
            node.set_opacity(1.0, normal)
        if projection is not None:
            _bind(node.uid(), 0)
        substance_painter.js.evaluate("alg.mapexport.save([{0}, 'normal'], {1}, {{bitDepth: 32, "
                                      "padding: 'Passthrough'}})".format(
                                          layer.uid(), json.dumps(path.replace("\\", "/"))))
    finally:
        if projection is not None:
            node.set_projection_parameters(projection)
        if held is not None:
            node.set_blending_mode(held[0], normal)
            node.set_opacity(held[1], normal)
        node.set_visible(shown)
        for effect in others:
            effect.set_visible(True)


def _write_fresh_picture(path):
    side = 16
    pixel = bytes(int(round(value * 255.0)) for value in _LEANING_NORMAL)
    raw = b"".join(b"\x00" + pixel * side for _ in range(side))

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _render_alone(stack, resource_id, path):
    """The normals of one picture as a fill laying nothing else takes them, rendered: a
    layer made on top of the stack for it and deleted again."""
    layer = layerstack.insert_fill(layerstack.InsertPosition.from_textureset_stack(stack))
    try:
        layer.active_channels = {textureset.ChannelType.Normal}
        layer.set_projection_mode(layerstack.ProjectionMode.UV)
        layer.set_projection_parameters(layerstack.UVProjectionParams(
            uv_transformation=layerstack.UVTransformationParams(
                scale_mode=layerstack.ScaleMode.Factors, scale=[1.0, 1.0], rotation=0.0, offset=[0.0, 0.0])))
        layer.set_source(textureset.ChannelType.Normal, resource_id)
        substance_painter.js.evaluate("alg.mapexport.save([{0}, 'normal'], {1}, {{bitDepth: 32, "
                                      "padding: 'Passthrough'}})".format(
                                          layer.uid(), json.dumps(path.replace("\\", "/"))))
    finally:
        layerstack.delete_node(layer)


def _readings(texture_set, normal_fills, fills, staging):
    """How Painter takes the green of the pictures of normals involved: each fill's own
    picture that is on disk, and a picture it has never seen -- which is what a picture
    Blender lays out anew comes in as. Painter takes a picture by the first use it is put
    to, so each is read off a render of its own; Blender compares the renders with the
    files. A render needs no reading: it is in the project's own convention."""
    stack = texture_set.get_stack()
    urls = {fill.uid: fill.normal[1] for fill in fills if fill.normal is not None}
    for entry in normal_fills:
        if entry["file"]:
            entry["reading"] = "reading_{0}.exr".format(entry["uid"])
            _render_alone(stack, substance_painter.resource.ResourceID.from_url(urls[entry["uid"]]),
                          str(staging.path(entry["reading"])))
    picture = str(staging.path("fresh_normal.png"))
    _write_fresh_picture(picture)
    resource = project_imports.take_in(picture, substance_painter.resource.Usage.TEXTURE,
                                       name="ruri_bridge_fresh_normal")
    _render_alone(stack, resource.identifier(), str(staging.path("fresh_normal_render.exr")))
    return {"picture": "fresh_normal.png", "render": "fresh_normal_render.exr"}


def answer(publisher, texture_set_name, request_number):
    """Say how a Texture Set reads its charts and hand over its mesh maps, laid out in its
    current layout. Returns one line about it."""
    names = {one.name: one for one in textureset.all_texture_sets()}
    texture_set = names.get(texture_set_name)
    refused = ""
    if texture_set is None:
        refused = "this project has no Texture Set {0}".format(texture_set_name)
    elif texture_set.has_uv_tiles():
        refused = "{0} is laid out in UV tiles, which a layout change cannot carry".format(texture_set_name)
    fills, unplaceable, unprojected = readers()
    tiled = sorted(name for name, members in unplaceable.values() if texture_set_name in members)
    if tiled and not refused:
        refused = "{0} has fills projected per UV tile ({1}); set their projection to UV first".format(
            texture_set_name, ", ".join(tiled))
    pictures = []
    if texture_set is not None and not refused:
        problems = []
        pictures = _unprojected_pictures(unprojected, texture_set, problems)
        if problems:
            refused = "; ".join(problems)
    readers_here = [{"uid": fill.uid, "index": fill.index, "members": sorted(fill.members),
                     "following": sorted(fill.following)}
                    for fill in fills if texture_set_name in fill.members]
    states = applied()
    tables = _tables(states)
    kept = sorted(dict((states.get(texture_set_name) or {}).get("mesh_maps") or {}))
    with publisher.staging() as staging:
        mesh_maps = {}
        convention = {}
        normal_fills = []
        if texture_set is not None and not refused:
            for usage, (identifier, kind) in MESH_MAPS.items():
                if texture_set.get_mesh_map_resource(usage) is None:
                    continue
                file_name = "{0}.exr".format(identifier)
                _save_mesh_map(texture_set_name, identifier, str(staging.path(file_name)))
                mesh_maps[usage.name] = {"file": file_name, "kind": kind}
            normal_fills = _normal_fills(fills, texture_set, staging,
                                         dict(project_facts.read(NORMALS_KEY) or {}))
            pictured = [entry for entry in normal_fills if not entry["untouched"]]
            if normal_fills or any(entry["kind"] == "tangent" for entry in mesh_maps.values()):
                convention = _convention_maps(texture_set, staging.directory)
            if pictured:
                convention["fresh"] = _readings(texture_set, pictured, fills, staging)
        fingerprint = dict(project_facts.read(SURFACE_KEY) or {}).get(texture_set_name, "")
        staging.publish(record_module.layout_answer(
            "Substance", texture_set_name, request_number, fingerprint, readers_here, tables,
            kept, mesh_maps, normal_fills, pictures, convention, refused))
    if refused:
        return "cannot change the layout of {0}: {1}".format(texture_set_name, refused)
    return "handed {0}'s {1} mesh map(s) to Blender for its new layout".format(
        texture_set_name, len(mesh_maps))
