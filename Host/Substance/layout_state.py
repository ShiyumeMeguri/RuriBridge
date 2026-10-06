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

A fill reading a picture of tangent-space normals keeps its pixels too, and the frame
they are decoded in turns where an island turns: such a picture is handed to Blender --
its own file, where Painter still knows one -- and comes back with every texel carried
into the frames of the new layout, in place, to stand in for it (``fills``).

A picture that is a Texture Set's own mesh map is not laid out in a chart at all, in
that Texture Set: Painter shows it there as whatever the mesh map is, and swaps it when
the map is swapped -- so it follows the layout, as the mesh maps do (``following``).

A fill whose UV transformation tiles, turns or offsets its source is a tile: a pattern
laid in UV space, which keeps tiling the new layout. It is left as it is, because
Painter's UV-set-to-UV-set projection samples a transformed source with other filtering
and does not turn the normals of a turned one -- switching it would change the very
Texture Sets that share it without being retargeted.

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
import os
import struct
import zlib

import substance_painter.export
import substance_painter.js
import substance_painter.layerstack as layerstack
import substance_painter.resource
import substance_painter.source as source_module
import substance_painter.textureset as textureset

from ...Kernel import layout as layout_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

from . import held_imports, project_facts, project_imports

LOG = logger("painter.layout")

#: Per Texture Set: the chart table applied last, and per chart the mesh maps laid out
#: in it, ``{usage: {"name": ..., "version": ...}}``.
LAYOUTS_KEY = "layouts"

#: Per Texture Set, the fingerprint of the surface the project holds (see the mesh
#: record): what its mesh maps are laid out on.
SURFACE_KEY = "surface"

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

#: The one normal a picture Painter has never seen holds, leaning along both axes so
#: neither can be mistaken for the other: how Painter takes a fresh picture of normals is
#: read off it (``_readings``).
_FRESH_NORMAL = (0.65, 0.8, 0.87)

_SPATIAL_SOURCES = (source_module.SourceBitmap, source_module.SourceSubstance,
                    source_module.SourceVectorial, source_module.SourceFont)
_TOLERANCE = 1e-6


# -- which fills read which chart ------------------------------------------------------

class Addressed:
    """One fill that reads its source through a chart, every Texture Set showing it, the
    pictures it reads, where its tangent normals come from (``normal_content``) when it
    lays any, and the Texture Sets whose own mesh map it reads (``following``)."""

    __slots__ = ("uid", "name", "index", "members", "layer", "normal", "pictures", "following")

    def __init__(self, uid, name, index, layer, normal, pictures):
        self.uid = uid
        self.name = name
        self.index = index
        self.members = set()
        self.layer = layer
        self.normal = normal
        self.pictures = pictures
        self.following = set()


def _identity(transformation):
    return (transformation.scale_mode == layerstack.ScaleMode.Factors
            and all(abs(value - 1.0) <= _TOLERANCE for value in transformation.scale)
            and abs(((transformation.rotation or 0.0) + 180.0) % 360.0 - 180.0) <= _TOLERANCE
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
    projection, a tile, a fill of uniform colours, or one reading the stack itself."""
    mode = node.get_projection_mode()
    if mode == layerstack.ProjectionMode.UVSetToUVSet:
        index = int(node.get_projection_parameters().source_uv_set or 0)
    elif mode == layerstack.ProjectionMode.UV:
        if not _identity(node.get_projection_parameters().uv_transformation):
            return None
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


def _visit(nodes, member, found, unplaceable, in_mask=False):
    for node in nodes:
        if isinstance(node, (layerstack.FillLayerNode, layerstack.FillEffectNode)):
            uid = node.uid()
            if uid not in found:
                mode = node.get_projection_mode()
                if mode == layerstack.ProjectionMode.Fill:
                    unplaceable.setdefault(uid, (node.get_name(), set()))
                    found[uid] = None
                else:
                    index = chart_index(node)
                    found[uid] = None if index is None else Addressed(
                        uid, node.get_name(), index, isinstance(node, layerstack.FillLayerNode),
                        None if in_mask else normal_content(node),
                        set().union(*(_pictures(source) for source in _sources(node) if source is not None)))
            if found[uid] is not None:
                found[uid].members.add(member)
            elif uid in unplaceable:
                unplaceable[uid][1].add(member)
        if isinstance(node, layerstack.LayerNode):
            _visit(node.content_effects(), member, found, unplaceable, in_mask)
            _visit(node.mask_effects(), member, found, unplaceable, True)
            if isinstance(node, layerstack.GroupLayerNode):
                _visit(node.sub_layers(), member, found, unplaceable, in_mask)
            if isinstance(node, layerstack.InstanceLayerNode):
                _visit([node.instance_source()], member, found, unplaceable, in_mask)


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
    Set, as whatever its mesh map is, and swaps it along with the map -- and the fills
    projected per UV tile, which no chart can carry, with theirs."""
    found = {}
    unplaceable = {}
    mesh_maps = {}
    for texture_set in textureset.all_texture_sets():
        mesh_maps[texture_set.name] = _mesh_map_pictures(texture_set)
        for stack in texture_set.all_stacks():
            _visit(layerstack.get_root_layer_nodes(stack), texture_set.name, found, unplaceable)
    fills = [entry for entry in found.values() if entry is not None]
    for fill in fills:
        fill.following = {member for member in fill.members if fill.pictures & mesh_maps.get(member, set())}
    return fills, unplaceable


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

    __slots__ = ("moves", "desired", "states", "relayouts", "nodes", "fingerprints", "fills")

    def __init__(self, moves, desired, states, relayouts, nodes, fingerprints, fills):
        self.moves = moves
        self.desired = desired
        self.states = states
        self.relayouts = relayouts
        self.nodes = nodes
        self.fingerprints = fingerprints
        self.fills = fills

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
    fills, unplaceable = readers()
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
        if delivered is not None and delivered["chart"] == new:
            relayouts[name] = ("delivered", old, new, {
                usage: dict(entry, held=held_imports.hold(os.path.join(directory, entry["file"]),
                                                          entry["hash"]))
                for usage, entry in delivered["mesh_maps"].items()})
        elif kept is not None:
            relayouts[name] = ("kept", old, new, kept)
        elif texture_set is not None and _current_mesh_maps(texture_set):
            problems.append("{0} moves to a layout nobody laid its mesh maps out in; retarget it "
                            "from Blender, which sends them with the surface".format(name))
        else:
            relayouts[name] = ("none", old, new, {})
    replaced = {}
    known = {fill.uid: fill for fill in fills}
    for name, entry in dict(record.get("relaid") or {}).items():
        for uid, replacement in dict(entry.get("fills") or {}).items():
            fill = known.get(int(uid))
            if fill is None or fill.normal is None or fill.normal[0] != "bitmap":
                problems.append("{0}: the layer Blender turned the normals of ({1}) is gone or "
                                "no longer lays a picture of normals".format(name, uid))
                continue
            replaced[int(uid)] = replacement
    if problems:
        raise layout_module.LayoutError("; ".join(problems))
    nodes = {fill.uid: fill.name for fill in fills}
    return Plan(moves, desired, states, relayouts, nodes, dict(record.get("fingerprints") or {}),
                replaced)


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
        if index == 0:
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


def _turned(uid, replacement):
    """Give a fill the picture of its normals Blender carried into the new frames."""
    node = layerstack.get_node_by_uid(uid)
    resource = project_imports.take_in(replacement["path"], substance_painter.resource.Usage.TEXTURE,
                                       name=os.path.splitext(os.path.basename(replacement["path"]))[0])
    node.set_source(textureset.ChannelType.Normal, resource.identifier())


def after_surface(chosen):
    """Moves to the other UV sets, the mesh maps of every Texture Set that changed layout,
    the normals turned with their islands, and the tables now applied."""
    for uid, index in sorted(chosen.moves.items()):
        if index != 0:
            _bind(uid, index)
    for uid, replacement in sorted(chosen.fills.items()):
        _turned(uid, replacement)
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
    _settle(states, chosen.desired, names)
    _hold_surface(chosen.fingerprints, names)
    moved = len(chosen.moves)
    relaid = sorted(name for name in chosen.relayouts if name in names)
    if moved or relaid:
        LOG.info("%d fill(s) now read another UV set, %d took normals turned with their "
                 "islands; %d Texture Set(s) took the mesh maps of their new layout", moved,
                 len(chosen.fills), len(relaid))
    return Applied("{0} fill(s) rebound, {1} with turned normals, {2} Texture Set(s) relaid".format(
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
    chart back. Every one the bridge imported is held out of the after-save sweep."""
    kept = [entry for state in states.values() for maps in dict(state.get("mesh_maps") or {}).values()
            for entry in maps.values()]
    project_imports.keep(kept)


# -- answering a coming layout change -------------------------------------------------------

def _save_mesh_map(texture_set_name, identifier, path):
    substance_painter.js.evaluate("alg.mapexport.saveMeshMap({0}, {1}, {2}, {{bitDepth: 32}})".format(
        json.dumps(texture_set_name), json.dumps(identifier), json.dumps(path.replace("\\", "/"))))


def _convention_maps(texture_set, directory):
    """The maps that tell which way the stored tangent maps point their green: the
    combined OpenGL normal export, the normal and height channels it is combined from, and
    how the Texture Set combines its normal channel with the mesh map."""
    maps = []
    for name, kind, source, dest in (("probe_normal_gl", "virtualMap", "Normal_OpenGL", "RGB"),
                                     ("probe_channel_normal", "documentMap", "normal", "RGB"),
                                     ("probe_height", "documentMap", "height", "L")):
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
    found = {key: written[key] for key in ("probe_normal_gl", "probe_channel_normal", "probe_height")
             if key in written}
    found["blending"] = substance_painter.js.evaluate(
        "alg.texturesets.structure({0}).additionalNormalMapBlending".format(json.dumps(texture_set.name)))
    return found


def _file_of(url):
    """The file a project resource was imported from, while it is still on disk."""
    info = json.loads(substance_painter.js.evaluate(
        "JSON.stringify(alg.resources.getResourceInfo({0}))".format(json.dumps(url))))
    path = str(info.get("filePath") or "")
    return path if path and os.path.isfile(path) else ""


def _normal_fills(fills, texture_set_name, staging):
    """Every fill of the Texture Set laying tangent normals through a chart: where its
    picture is on disk, or -- for a layer whose normals are not a picture on disk -- its
    normals rendered in the current layout, so Blender can tell whether any lie where the
    frames turn."""
    found = []
    for fill in fills:
        if texture_set_name not in fill.members or texture_set_name in fill.following or fill.normal is None:
            continue
        kind, url = fill.normal
        entry = {"uid": fill.uid, "name": fill.name, "index": fill.index,
                 "members": sorted(fill.members), "source": kind, "file": "", "render": ""}
        if kind == "bitmap":
            entry["file"] = _file_of(url)
        if not entry["file"] and fill.layer:
            render = "fill_{0}_normal.exr".format(fill.uid)
            substance_painter.js.evaluate("alg.mapexport.save([{0}, 'normal'], {1}, {{bitDepth: 32, "
                                          "padding: 'Passthrough'}})".format(
                                              fill.uid, json.dumps(str(staging.path(render)).replace("\\", "/"))))
            entry["render"] = render
        found.append(entry)
    return found


def _write_fresh_picture(path):
    side = 16
    pixel = bytes(int(round(value * 255.0)) for value in _FRESH_NORMAL)
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
    Blender turns comes in as. Painter takes a picture by the first use it is put to, so
    each is read off a render of its own; Blender compares the renders with the files."""
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
    fills, unplaceable = readers()
    tiled = sorted(name for name, members in unplaceable.values() if texture_set_name in members)
    if tiled and not refused:
        refused = "{0} has fills projected per UV tile ({1}); set their projection to UV first".format(
            texture_set_name, ", ".join(tiled))
    used = sorted({fill.index for fill in fills if texture_set_name in fill.members
                   and texture_set_name not in fill.following and fill.index > 0})
    taken = {fill.index for fill in fills if fill.index > 0}
    for state in applied().values():
        taken |= {int(index) for index in state["extra"]}
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
            normal_fills = _normal_fills(fills, texture_set_name, staging)
            if normal_fills or any(entry["kind"] == "tangent" for entry in mesh_maps.values()):
                convention = _convention_maps(texture_set, staging.directory)
            if any(entry["file"] for entry in normal_fills):
                convention["fresh"] = _readings(texture_set, normal_fills, fills, staging)
        fingerprint = dict(project_facts.read(SURFACE_KEY) or {}).get(texture_set_name, "")
        staging.publish(record_module.layout_answer(
            "Substance", texture_set_name, request_number, fingerprint, used, sorted(taken),
            mesh_maps, normal_fills, convention, refused))
    if refused:
        return "cannot change the layout of {0}: {1}".format(texture_set_name, refused)
    return "handed {0}'s {1} mesh map(s) to Blender for its new layout".format(
        texture_set_name, len(mesh_maps))
