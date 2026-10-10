# -*- coding: utf-8 -*-
"""Faces that moved into another Texture Set, painted as they were.

A face that paints into another Texture Set than the one it was painted in leaves its paint
behind: Painter's layers are a Texture Set's, and the face shows the stack it moved into.
Blender keeps, per face, the Texture Set its paint lives in and where in that layout it lies,
and carries the paint over in the same step that sends the surface: it asks how the sources'
stacks are made (``answer``), and the surface brings what to build (``plan``,
``before_surface``, ``after_surface``).

Each carry of faces from one source into one Texture Set -- an **event** -- becomes a folder of
its own on top of that Texture Set's stack, ``From <source>``, showing the source's stack on
those faces only, in place of what lies under it -- blended by Replace in every channel, so where
the source's layers lay nothing the faces show nothing laid, as they did in the source, and not the
Texture Set's own layers: a black mask with a picture of them laid out in this layout, and everything
laid out in UV space read through the UV set where every face holds the coordinates its paint
is laid out in (``Kernel.layout``), the faces that came holding there the coordinates they had
in the source. The folder holds the source's stack, root by root, top to bottom, as Blender
decided from what this side handed over:

* a layer the source shows through an instance is instanced again from where it lives -- live,
  edited in one place, and showing no paint, which Painter casts only in the Texture Set the
  layer belongs to, there as in the source;
* a layer of the source that holds no paint is instanced too, while the source keeps faces; a
  source that keeps none goes with this surface, and what is needed of it is copied first;
* a layer of the source that holds paint is copied -- a folder by Painter's own smart material,
  the bridge's own layer of the source's Blender material as the plain fill it is -- and its
  paint is cast again where it now lies: strokes laid in 3D land where they lay on the surface,
  live; paint laid out in UV space or picked by polygon cannot follow -- the UVs and the
  triangles are the new Texture Set's -- and a fill stands in for it inside the copy, pictures
  of it as the source showed it, read through the coordinates it was made in
  (``paint_pixels.stand_in``); copies covering none of the faces are left out. An instance a
  copied folder holds is cast again from where its layer lives: a smart material keeps no
  instance's source.

The mesh maps of the Texture Set take the moving faces' maps laid out where they lie now, so what
computes from mesh maps computes on them as it did in the source; channels the sources have and
the Texture Set lacks are added with their format and label. Nothing of the Texture Set's own is
touched, nothing is baked: procedural content stays procedural.

The masks follow the faces: every surface brings a picture of where each event's faces lie now,
and a mask whose picture changed takes the new one.
"""

from __future__ import annotations

import os

import substance_painter.colormanagement as colormanagement
import substance_painter.layerstack as layerstack
import substance_painter.resource
import substance_painter.source as source_module
import substance_painter.textureset as textureset

from ...Kernel import record as record_module
from ...Kernel.log import logger

from . import held_imports, layout_state, material_seed, paint_pixels, project_document, project_facts, project_imports

LOG = logger("painter.guests")

#: Per Texture Set holding guests, per event, the source the faces came from, the folder showing
#: its paint on them, the fill of the folder's mask and the file of the picture it reads:
#: ``{target: {event: {"source", "group", "mask", "file"}}}``.
GUESTS_KEY = "guests"


class GuestError(RuntimeError):
    """Faces whose paint cannot be carried, named."""


# -- what the stacks are --------------------------------------------------------------------------

def shown_by(node):
    """The layer an instance shows, through instances of instances; the node itself otherwise."""
    while isinstance(node, layerstack.InstanceLayerNode):
        node = node.instance_source()
    return node


def _layers_under(node):
    found = [node]
    if isinstance(node, layerstack.GroupLayerNode):
        for child in node.sub_layers():
            found.extend(_layers_under(child))
    return found


def _kind(node):
    """``group`` for a folder, ``plain`` for a fill with no effect and no mask, ``other`` else:
    what a copy can be made of."""
    if isinstance(node, layerstack.GroupLayerNode):
        return "group"
    if isinstance(node, layerstack.FillLayerNode) and not node.content_effects() and not node.has_mask():
        return "plain"
    return "other"


def _coverage(node, texture_set, staging):
    """Where a root layer covers its Texture Set: per channel its content with the coverage as
    alpha, and its mask."""
    channels = {}
    for channel in sorted(texture_set.get_stack().all_channels(), key=lambda one: one.name):
        file_name = "coverage_{0}_{1}.exr".format(node.uid(), channel.name)
        paint_pixels.save(node.uid(), channel.name.lower(), str(staging.path(file_name)), True)
        channels[channel.name] = file_name
    mask = ""
    if node.has_mask():
        mask = "coverage_{0}_mask.exr".format(node.uid())
        paint_pixels.save(node.uid(), paint_pixels.MASK, str(staging.path(mask)), False)
    return {"mask": mask, "channels": channels}


def _layer(node, texture_set, painted, seed, emptied, staging, measured):
    """One layer of the Texture Set as ``carry_answer`` describes a root layer; where it covers the
    Texture Set only when ``measured`` and it would be copied."""
    shows = shown_by(node)
    home = shows.get_texture_set().name
    paint = any(layer.uid() in painted for layer in _layers_under(shows))
    own_seed = seed is not None and node.uid() == int(seed)
    copied = measured and home == texture_set.name and not own_seed and (paint or emptied)
    return {"uid": node.uid(), "name": node.get_name(), "shows": shows.uid(), "home": home,
            "paint": paint, "seed": own_seed, "kind": _kind(shows), "visible": node.is_visible(),
            "coverage": _coverage(node, texture_set, staging) if copied and node.is_visible() else None}


def _roots(texture_set, painted, seed, emptied, staging):
    """The Texture Set's root layers, top first, as ``carry_answer`` describes them."""
    return [_layer(node, texture_set, painted, seed, emptied, staging, True)
            for node in layerstack.get_root_layer_nodes(texture_set.get_stack())]


def _lying_in(node, group):
    """The layer a node lies under that is a root layer or lies in ``group`` (a uid)."""
    while node.get_parent() is not None and node.get_parent().uid() != group:
        node = node.get_parent()
    return node


def _leaving(texture_set, folder, folders, painted, seed, emptied, held, staging, problems):
    """An event's folder in the Texture Set holding it, for the faces leaving with it, as
    ``carry_answer`` gives ``rehomes``: the root layers over the folder -- but the other events'
    folders (``folders``, uids), which show on their own faces only -- and the layers it holds, top
    first; the paint under the ones copied that a copy cannot cast again -- polygon fills only, the
    faces lying where they lay -- each with the layer of those it lies under (``root``); every layer
    lying in the folder, which goes with it (``lying``); and every layer and effect the folder shows,
    through instances too (``inside``)."""
    group = layerstack.get_node_by_uid(int(folder["group"]))
    over = []
    for node in layerstack.get_root_layer_nodes(texture_set.get_stack()):
        if node.uid() == group.uid():
            break
        if node.uid() in folders:
            continue
        over.append(dict(_layer(node, texture_set, painted, seed, emptied, staging, True), inside=False))
    inside = [dict(_layer(node, texture_set, painted, None, True, staging, False), inside=True)
              for node in group.sub_layers()]
    copied = {entry["uid"] for entry in over if entry["coverage"] is not None} | {group.uid()}
    found = paint_pixels.carried(texture_set, held, copied, problems, laid_alike=True)
    frozen = [dict(entry, root=_lying_in(layerstack.get_node_by_uid(entry["layer"]), group.uid()).uid())
              for entry in paint_pixels.capture(found, staging)]
    return {"holder": texture_set.name, "roots": over + inside, "frozen": frozen,
            "lying": [layer.uid() for layer in _layers_under(group)], "inside": _nodes_under(group)}


def _nodes_under(node):
    """Every layer and effect a root layer shows, through instances."""
    found = []
    for layer in _layers_under(shown_by(node)):
        found.append(layer.uid())
        found.extend(effect.uid() for effect in layer.content_effects())
        if layer.has_mask():
            found.extend(effect.uid() for effect in layer.mask_effects())
        if isinstance(layer, layerstack.InstanceLayerNode):
            found.extend(_nodes_under(layer))
    return found


def _normals(texture_set, fills, roots, staging):
    """The fills a source shows laying tangent normals through a chart (``layout_state.normal_fills``),
    each with the root layer it lies under."""
    owner = {}
    for root in roots:
        for uid in _nodes_under(layerstack.get_node_by_uid(int(root["uid"]))):
            owner.setdefault(uid, int(root["uid"]))
    return [dict(entry, root=owner[entry["uid"]])
            for entry in layout_state.normal_fills(fills, texture_set, staging, {}) if entry["uid"] in owner]


def _described(texture_set, prefix, staging):
    """A Texture Set's resolution, channels and mesh maps, the maps beside the answer."""
    resolution = texture_set.get_resolution()
    channels = [{"channel": channel_type.name, "format": channel.format().name, "label": channel.label()}
                for channel_type, channel in sorted(texture_set.get_stack().all_channels().items(),
                                                    key=lambda item: item[0].name)]
    mesh_maps = {}
    for usage, (identifier, kind) in layout_state.MESH_MAPS.items():
        if texture_set.get_mesh_map_resource(usage) is None:
            continue
        file_name = "{0}_{1}.exr".format(prefix, identifier)
        layout_state.save_mesh_map(texture_set.name, identifier, str(staging.path(file_name)))
        mesh_maps[usage.name] = {"file": file_name, "kind": kind}
    return {"resolution": [resolution.width, resolution.height], "channels": channels, "mesh_maps": mesh_maps}


def answer(publisher, renamed, moves, rehomes, emptied, request_number, refused=""):
    """Say how the stacks the moving faces' paint lives in are made, once the Texture Sets ``renamed``
    (old name to new) were renamed, and hand over what carrying their faces needs: the mesh maps of
    the Texture Sets the faces leave and go into, the chart-addressed fills those show, the sources'
    root layers with pictures of where the ones that would be copied cover them, the folders leaving
    with their faces (``rehomes``, ``{event: {"holder", "target"}}``) with what lies over them and in
    them (``_leaving``), pictures of the paint a copy cannot cast again, and the fills laying normals
    through a chart. Returns one line about it."""
    names = {one.name: one for one in textureset.all_texture_sets()}
    events = known()
    targets = sorted(set(moves) | {entry["target"] for entry in rehomes.values()})
    sources = sorted({source for listed in moves.values() for source in listed})
    holders = sorted({entry["holder"] for entry in rehomes.values()})
    leaving_from = set(sources) | set(holders)
    if not refused:
        missing = sorted(name for name in leaving_from if name not in names)
        if missing:
            refused = "this project has no Texture Set {0} to carry paint from".format(", ".join(missing))
    if not refused:
        gone = sorted(event for event, entry in rehomes.items() if event not in events.get(entry["holder"], {}))
        if gone:
            refused = "this project holds no folder of {0} for the faces carried into it".format(
                ", ".join(sorted({rehomes[event]["holder"] for event in gone})))
    tiled = sorted(name for name in leaving_from | set(targets) if name in names and names[name].has_uv_tiles())
    if tiled and not refused:
        refused = "{0} laid out in UV tiles, which faces cannot be carried between".format(", ".join(tiled))
    fills = []
    if not refused:
        fills, unplaceable, _unprojected = layout_state.readers()
        projected = sorted(name for name, members in unplaceable.values() if members & leaving_from)
        if projected:
            refused = ("fills projected per UV tile ({0}) cannot read where their faces went; set their "
                       "projection to UV first".format(", ".join(projected)))
    readers = [{"uid": fill.uid, "index": fill.index, "members": sorted(fill.members),
                "following": sorted(fill.following)} for fill in fills if fill.members & leaving_from]
    roots, frozen, normals, described, convention, leaving = {}, {}, {}, {}, {}, {}
    carrying = bool(targets) and not refused
    with publisher.staging() as staging:
        held = None
        if carrying:
            try:
                held = paint_pixels.document()
            except project_document.DocumentError as error:
                refused = "the project file holds its strokes in a form the bridge does not read: {0}".format(error)
                carrying = False
        if carrying:
            painted = project_document.painted(held)
            seeds = {name: state.get("layer")
                     for name, state in dict(project_facts.read(material_seed.SEED_KEY) or {}).items()}
            problems = []
            for source in sources:
                roots[source] = _roots(names[source], painted, seeds.get(source), source in emptied, staging)
                copied = {entry["uid"] for entry in roots[source] if entry["coverage"] is not None}
                found = paint_pixels.carried(names[source], held, copied, problems)
                frozen[source] = [dict(entry, root=paint_pixels.root_of(layerstack.get_node_by_uid(entry["layer"])).uid())
                                  for entry in paint_pixels.capture(found, staging)]
                normals[source] = _normals(names[source], fills, roots[source], staging)
            for event, entry in sorted(rehomes.items()):
                holder = entry["holder"]
                folders = {int(one["group"]) for one in events[holder].values()}
                leaving[event] = dict(_leaving(names[holder], events[holder][event], folders, painted,
                                               seeds.get(holder), holder in emptied, held, staging, problems),
                                      target=entry["target"])
            if problems:
                refused = "; ".join(problems)
        if carrying and not refused:
            for index, name in enumerate(sorted(leaving_from | set(targets))):
                if name in names:
                    described[name] = _described(names[name], "set{0}".format(index), staging)
            carried_from = sources + holders
            probed = next((name for name in carried_from if described[name]["mesh_maps"].get("Normal")), None)
            normal_paint = any(channel["space"] == "normal"
                               for listed in list(frozen.values()) + [one["frozen"] for one in leaving.values()]
                               for entry in listed for channel in entry["channels"])
            pictured = [entry for listed in normals.values() for entry in listed if not entry["untouched"]]
            if probed is None and (normal_paint or any(normals.values())):
                probed = carried_from[0]
            if probed is not None:
                convention[probed] = layout_state.convention_maps(names[probed], staging.directory)
                if pictured:
                    convention[probed]["fresh"] = layout_state.readings(names[probed], pictured, fills, staging)
        staging.publish(record_module.carry_answer(
            "Substance", request_number, renamed, moves, leaving, layout_state.tables_of(layout_state.applied()),
            described, readers, roots, frozen, normals, convention, refused))
    done = ["renamed {0} -> {1}".format(old, new) for old, new in sorted(renamed.items())]
    if refused:
        return "; ".join(done + ["cannot carry faces into {0}: {1}".format(", ".join(targets) or "anything",
                                                                            refused)])
    if targets:
        done.append("handed the stacks of {0} to Blender for their faces moving into {1}".format(
            ", ".join(sorted(leaving_from)), ", ".join(targets)))
    return "; ".join(done) or "nothing to carry"


def rename(renames):
    """Texture Sets renamed, old name to new: the events held in each, and the sources they came
    from, follow the names."""
    held = dict(project_facts.read(GUESTS_KEY) or {})
    project_facts.write(GUESTS_KEY, {
        renames.get(target, target): {event: dict(entry, source=renames.get(entry["source"], entry["source"]))
                                      for event, entry in dict(events).items()}
        for target, events in held.items()})


# -- the surface's guests -------------------------------------------------------------------------

def known():
    """Every event the project holds whose folder is still there, by Texture Set."""
    found = {}
    for target, events in dict(project_facts.read(GUESTS_KEY) or {}).items():
        for event, entry in dict(events).items():
            if paint_pixels.exists(entry["group"]) and paint_pixels.exists(entry["mask"]):
                found.setdefault(target, {})[str(event)] = dict(entry)
    return found


class Plan:
    """What one surface does with guests, worked out before anything moves: the events it
    carries (``carry``), the masks of every event (``guests``), the mesh maps taken in, held,
    and -- filled in before the surface goes in, while every source is still there -- the
    layers to lay, by uid: copies (``copies``) and the layers instances show (``instances``,
    ``{"shows", "name", "visible", "blending"}``), each with how it blends where it lies
    (``_blending``); the channels of each Texture Set they lie in (``channels``); how each folder
    leaving with its faces blends, by event (``folders``); and what is laid only as near as Painter
    lets it, one line each (``notes``)."""

    __slots__ = ("guests", "carry", "mesh_maps", "copies", "instances", "channels", "folders", "notes")

    def __init__(self, guests, carry, mesh_maps):
        self.guests = guests
        self.carry = carry
        self.mesh_maps = mesh_maps
        self.copies = {}
        self.instances = {}
        self.channels = {}
        self.folders = {}
        self.notes = []

    @property
    def emptied(self):
        return set((self.carry or {}).get("emptied") or [])


def _home(carried):
    """The Texture Set the layers an event lays lie in now: the folder's, for one leaving with its
    faces; the source's otherwise."""
    return carried["holder"] or carried["source"]


def _outside_instances(node, inside):
    """The Texture Sets showing a layer, or anything under it, through instances not in ``inside``."""
    found = set()
    for layer in _layers_under(node):
        found |= {instance.get_texture_set().name for instance in layer.instances() if instance.uid() not in inside}
    return found


def _own_problem(target, carried, events):
    """What stands in the way of laying an event's folder under the target's own layers the source
    shows, or empty: they have to be the source's topmost layers and the target's topmost, in the same
    order, so the target's stack lays them over the faces as the source did."""
    own = [root for root in carried["roots"] if root["carry"] == "own"]
    if not own:
        return ""
    if any(root["carry"] != "own" for root in carried["roots"][:len(own)]):
        return ("{0} lays layers of {1} under layers of its own; carrying its faces into {1} would change their "
                "order".format(carried["source"], target))
    folders = {int(entry["group"]) for entry in events.values()}
    stacked = [node.uid() for node in layerstack.get_root_layer_nodes(textureset.TextureSet.from_name(target).get_stack())
               if node.uid() not in folders]
    try:
        shown = [shown_by(layerstack.get_node_by_uid(int(root["uid"]))).uid() for root in own]
    except ValueError:
        return "{0}: a layer of {1} it showed is gone".format(carried["source"], target)
    if stacked[:len(shown)] != shown:
        return ("{0} shows layers of {1} in another order, or with others between, than {1}'s own stack lays them; "
                "carrying its faces into {1} would change how they look".format(carried["source"], target))
    return ""


def plan(record, directory):
    """Check what the surface's guests need before anything moves; ``GuestError`` names what cannot
    be carried. Mesh maps delivered with the surface are held at once."""
    guests = dict(record.get("guests") or {})
    carry = record.get("carry") or {}
    targets = dict(carry.get("targets") or {})
    held = known()
    names = {one.name for one in textureset.all_texture_sets()}
    problems = []
    for target, entry in sorted(guests.items()):
        for event, guest in sorted(entry["events"].items()):
            carried_now = event in dict((targets.get(target) or {}).get("events") or {})
            if not carried_now and event not in held.get(target, {}):
                problems.append("faces of {0} came from {1} and this project holds no paint carried for them; "
                                "open the project they were carried in".format(target, guest["source"]))
    for target, entry in sorted(targets.items()):
        for event, carried in sorted(entry["events"].items()):
            if _home(carried) not in names:
                problems.append("there is no Texture Set {0} to carry paint from".format(_home(carried)))
                continue
            if carried["holder"] and event not in held.get(carried["holder"], {}):
                problems.append("{0} holds no folder for the faces leaving it for {1}".format(carried["holder"],
                                                                                            target))
                continue
            for root in carried["roots"]:
                try:
                    node = layerstack.get_node_by_uid(int(root["uid"]))
                except ValueError:
                    problems.append("{0}: its root layer {1} is gone".format(carried["source"], root["uid"]))
                    continue
                shows = shown_by(node)
                if root["carry"] == "instance":
                    if shows.get_texture_set().name in set(carry.get("emptied") or []):
                        problems.append("{0}: {1} would show a layer of {2}, which goes with this surface".format(
                            carried["source"], node.get_name(), shows.get_texture_set().name))
                elif root["carry"] == "copy" and _kind(shows) == "other":
                    problems.append("{0}: {1} would have to be copied, and only folders and plain fills can be; "
                                    "put it in a folder in Painter".format(carried["source"], node.get_name()))
                if root["carry"] == "skip":
                    continue
                for layer in [node] + (_layers_under(shows) if root["carry"] == "copy" else []):
                    if isinstance(layer, layerstack.InstanceLayerNode) and layer.has_mask():
                        problems.append("{0}: the instance {1} has a mask of its own, which an instance laid again "
                                        "cannot take along; put the mask on a folder around it in Painter".format(
                                            carried["source"], layer.get_name()))
            problem = _own_problem(target, carried, held.get(target, {}))
            if problem:
                problems.append(problem)
    emptied = set(carry.get("emptied") or [])
    for source in sorted(emptied & names):
        for node in layerstack.get_root_layer_nodes(textureset.TextureSet.from_name(source).get_stack()):
            elsewhere = _outside_instances(node, set()) - emptied
            if elsewhere:
                problems.append("{0} goes with this surface, and {1} show(s) its layer {2} through instances".format(
                    source, ", ".join(sorted(elsewhere)), node.get_name()))
    if problems:
        raise GuestError("; ".join(problems))
    mesh_maps = {}
    for target, carried_into in targets.items():
        mesh_maps[target] = {usage: dict(delivered, held=held_imports.delivered(directory, delivered))
                             for usage, delivered in dict(carried_into.get("mesh_maps") or {}).items()}
    masked = {target: dict(entry, events={event: dict(guest, mask=held_imports.delivered(directory, guest["mask"]))
                                          for event, guest in entry["events"].items()})
              for target, entry in guests.items()}
    if targets:
        carry = dict(carry, targets={
            target: dict(entry, events={event: dict(carried, frozen={
                uid: paint_pixels.held(frozen, directory) for uid, frozen in dict(carried.get("frozen") or {}).items()},
                normals={uid: held_imports.delivered(directory, picture)
                         for uid, picture in dict(carried.get("normals") or {}).items()})
                for event, carried in entry["events"].items()})
            for target, entry in targets.items()})
    return Plan(masked, carry, mesh_maps)


# -- before the surface goes in: what a source that goes leaves behind ----------------------------

def _shape(node):
    """A layer's tree as it stands, to find its parts again in a copy: kinds, uids, effects, and the
    layer every instance shows."""
    shape = {"uid": node.uid(), "kind": type(node).__name__, "name": node.get_name(), "visible": node.is_visible()}
    if isinstance(node, layerstack.InstanceLayerNode):
        shape["shows"] = shown_by(node).uid()
    if isinstance(node, layerstack.LayerNode):
        shape["content"] = [[effect.uid(), type(effect).__name__] for effect in node.content_effects()]
        shape["mask"] = ([[effect.uid(), type(effect).__name__] for effect in node.mask_effects()]
                         if node.has_mask() else None)
    if isinstance(node, layerstack.GroupLayerNode):
        shape["children"] = [_shape(child) for child in node.sub_layers()]
    return shape


def _plain(node):
    """Everything a plain fill lays, to lay it again."""
    channels = {}
    for channel in node.active_channels:
        source = node.get_source(channel)
        if isinstance(source, source_module.SourceBitmap):
            laid = {"bitmap": [source.resource_id.name, source.resource_id.version],
                    "space": source.get_color_space()}
        elif isinstance(source, source_module.SourceUniformColor):
            laid = {"colour": source.get_color()}
        else:
            raise GuestError("{0} lays {1} from a {2}, which a plain copy cannot lay again".format(
                node.get_name(), channel.name, type(source).__name__))
        channels[channel] = dict(laid, blending=node.get_blending_mode(channel), opacity=node.get_opacity(channel))
    return {"name": node.get_name(), "visible": node.is_visible(), "projection": node.get_projection_mode(),
            "parameters": node.get_projection_parameters(), "channels": channels}


def _blending(node, channels):
    """How a root layer blends over what lies under it, per channel of ``channels``: ``{channel:
    (mode, opacity)}``. An instance blends by its own settings, Passthrough taking the blending of
    the layer it shows; any other layer -- shown again through an instance -- is taken as it is,
    Passthrough at full opacity."""
    if isinstance(node, layerstack.InstanceLayerNode):
        return {channel: (node.get_blending_mode(channel), node.get_opacity(channel)) for channel in channels}
    return {channel: (layerstack.BlendingMode.Passthrough, 1.0) for channel in channels}


def _set_blending(node, channel, mode, opacity):
    if (node.get_blending_mode(channel), node.get_opacity(channel)) != (mode, opacity):
        node.set_blending_mode(mode, channel)
        node.set_opacity(opacity, channel)


def _blend(node, blending, stack, home):
    """Make an instance laid again blend as the root it stands for blended in its source, channel by
    channel, and lay nothing in a channel the source does not have and the layer it shows lays in --
    ``home``, the channels of the Texture Set that layer lives in. A channel neither has is left as
    the instance came: Painter takes a user channel the layer's own Texture Set lacks for another."""
    for channel in stack.all_channels():
        if channel in blending:
            _set_blending(node, channel, *blending[channel])
        elif channel in home:
            _set_blending(node, channel, layerstack.BlendingMode.Disable, 1.0)


def _blend_copy(node, blending):
    """Make a copy blend as the root it stands for blended in its source: where that root is an
    instance blending by its own settings, by them; where it passes the layer's own blending through,
    the copy keeps the blending it came with."""
    for channel, (mode, opacity) in blending.items():
        if mode != layerstack.BlendingMode.Passthrough:
            _set_blending(node, channel, mode, opacity)


def before_surface(chosen):
    """Take what the folders will hold while every source is still there: the layer each instance
    will show, and copies -- a folder as a smart material, a plain fill as what it lays, each with
    its tree as it stands -- each root with how it blends in its source. A root the target's own
    layer stands in for that blends otherwise there is noted."""
    folder = held_imports.folder()
    names = {one.name for one in textureset.all_texture_sets()}
    events = known()
    for target, entry in sorted(dict((chosen.carry or {}).get("targets") or {}).items()):
        target_channels = (set(textureset.TextureSet.from_name(target).get_stack().all_channels())
                           if target in names else set())
        for event, carried in sorted(entry["events"].items()):
            channels = set(textureset.TextureSet.from_name(_home(carried)).get_stack().all_channels())
            chosen.channels[_home(carried)] = channels
            if carried["holder"]:
                leaving = layerstack.get_node_by_uid(int(events[carried["holder"]][event]["group"]))
                chosen.folders[event] = {channel: (leaving.get_blending_mode(channel), leaving.get_opacity(channel))
                                         for channel in channels}
            for root in carried["roots"]:
                node = layerstack.get_node_by_uid(int(root["uid"]))
                if root["carry"] in ("instance", "own"):
                    shows = shown_by(node)
                    blending = _blending(node, channels)
                    chosen.instances[int(root["uid"])] = {
                        "shows": shows.uid(), "name": node.get_name(), "visible": node.is_visible(),
                        "blending": blending, "home": set(shows.get_texture_set().get_stack().all_channels())}
                    apart = sorted(channel.name for channel in channels & target_channels
                                   if blending[channel] != _blending(shows, [channel])[channel])
                    if root["carry"] == "own" and apart:
                        chosen.notes.append("{0}'s faces lie under {1}'s own {2}, which {0} showed blended otherwise "
                                            "in {3}".format(carried["source"], target, shows.get_name(), ", ".join(apart)))
                    continue
                if root["carry"] != "copy" or int(root["uid"]) in chosen.copies:
                    continue
                shows = shown_by(node)
                copy = {"shape": _shape(shows), "kind": _kind(shows), "blending": _blending(node, channels)}
                if copy["kind"] == "group":
                    name = "ruri_carry_{0}".format(shows.uid())
                    layerstack.export_as_smart_material(shows, name, str(folder))
                    resource = project_imports.take_in(str(folder / (name + ".spsm")),
                                                       substance_painter.resource.Usage.SMART_MATERIAL, name=name)
                    copy["resource"] = resource.identifier()
                else:
                    copy["plain"] = _plain(shows)
                chosen.copies[int(root["uid"])] = copy


# -- after the surface is in: the folders -----------------------------------------------------------

def _lay_plain(position, plain):
    fill = layerstack.insert_fill(position)
    fill.active_channels = set(plain["channels"])
    for channel, laid in plain["channels"].items():
        if "bitmap" in laid:
            name, version = laid["bitmap"]
            fill.set_source(channel, substance_painter.resource.ResourceID.from_project(name, version)
                            ).set_color_space(laid["space"])
        else:
            fill.set_source(channel, laid["colour"])
        fill.set_blending_mode(laid["blending"], channel)
        fill.set_opacity(laid["opacity"], channel)
    fill.set_projection_mode(plain["projection"])
    if plain["parameters"] is not None:
        fill.set_projection_parameters(plain["parameters"])
    fill.set_name(plain["name"])
    fill.set_visible(plain["visible"])
    return fill


def _match(shape, node, found, converted, repairs):
    """Walk a copy along the tree of what it copies, mapping uids: layers and effects to their
    copies, a mask's own paint a smart material turned into an effect at its bottom
    (``converted``), and every instance whose source the copy lost (``repairs``)."""
    if type(node).__name__ != shape["kind"]:
        raise GuestError("the copy of {0} is not made as it is ({1} where {2} was)".format(
            shape["name"], type(node).__name__, shape["kind"]))
    found[shape["uid"]] = node.uid()
    if "shows" in shape:
        repairs.append((node, shape))
    if "content" in shape:
        _match_effects(shape, shape["content"], node.content_effects(), found, None)
        if shape["mask"] is not None:
            if not node.has_mask():
                raise GuestError("the copy of {0} has no mask".format(shape["name"]))
            _match_effects(shape, shape["mask"], node.mask_effects(), found, converted)
    if "children" in shape:
        children = node.sub_layers()
        if len(children) != len(shape["children"]):
            raise GuestError("the copy of {0} holds {1} layers where it held {2}".format(
                shape["name"], len(children), len(shape["children"])))
        for child_shape, child in zip(shape["children"], children):
            _match(child_shape, child, found, converted, repairs)


def _match_effects(shape, listed, effects, found, converted):
    extra = effects[len(listed):]
    if len(effects) < len(listed) or (extra and (converted is None or not all(
            isinstance(effect, layerstack.PaintEffectNode) for effect in extra))):
        raise GuestError("the copy of {0} holds {1} effects where it held {2}".format(
            shape["name"], len(effects), len(listed)))
    for (uid, kind), effect in zip(listed, effects):
        if type(effect).__name__ != kind:
            raise GuestError("the copy of {0} holds a {1} where it held a {2}".format(
                shape["name"], type(effect).__name__, kind))
        found[uid] = effect.uid()
    if extra:
        converted[shape["uid"]] = [effect.uid() for effect in extra]


def _repair(node, shape, channels, stack):
    """Show again, where a copy holds an instance whose source it lost, the layer the instance showed,
    blended as the instance was in the source's ``channels``."""
    instance = layerstack.instantiate(layerstack.InsertPosition.above_node(node),
                                      layerstack.get_node_by_uid(int(shape["shows"])))
    instance.set_name(shape["name"])
    instance.set_visible(shape["visible"])
    _blend(instance, _blending(node, channels), stack,
           set(layerstack.get_node_by_uid(int(shape["shows"])).get_texture_set().get_stack().all_channels()))
    layerstack.delete_node(node)
    return instance


def _lay_normal(node, path):
    """Make a copied fill lay its normals from the picture Blender turned into the frames its faces
    have now, read through the chart it reads."""
    resource = project_imports.take_in(path, substance_painter.resource.Usage.TEXTURE,
                                       name=os.path.splitext(os.path.basename(path))[0])
    node.set_source(textureset.ChannelType.Normal, resource.identifier())


def _carried_fills(node, index, following, bound):
    """Make every chart-addressed fill under a layer read the painted UV set, but the ones showing
    the target's own mesh maps, which follow its layout."""
    for layer in _layers_under(node):
        for effect in list(layer.content_effects()) + (list(layer.mask_effects()) if layer.has_mask() else []):
            _bind_fill(effect, index, following, bound)
        _bind_fill(layer, index, following, bound)
        if isinstance(layer, layerstack.InstanceLayerNode):
            _carried_fills(shown_by(layer), index, following, bound)


def _bind_fill(node, index, following, bound):
    if not isinstance(node, (layerstack.FillLayerNode, layerstack.FillEffectNode)):
        return
    if node.uid() in bound or node.uid() in following:
        return
    if layout_state.chart_index(node) != 0:
        return
    layout_state.bind(node.uid(), index)
    bound.add(node.uid())


def _read_mask(fill, path):
    """Make a mask's fill read the picture at ``path``, as it is stored."""
    identifier = project_imports.take_in(path, substance_painter.resource.Usage.TEXTURE,
                                         name=os.path.splitext(os.path.basename(path))[0]).identifier()
    fill.set_source(None, identifier).set_color_space(colormanagement.GenericColorSpace.Raw)


def _mask_fill(group, path):
    group.add_mask(layerstack.MaskBackground.Black)
    fill = layerstack.insert_fill(layerstack.InsertPosition.inside_node(group, layerstack.NodeStack.Mask))
    _read_mask(fill, path)
    fill.set_projection_mode(layerstack.ProjectionMode.UV)
    fill.set_projection_parameters(layerstack.UVProjectionParams(
        uv_transformation=layerstack.UVTransformationParams(
            scale_mode=layerstack.ScaleMode.Factors, scale=[1.0, 1.0], rotation=0.0, offset=[0.0, 0.0])))
    return fill


def _channels(texture_set, wanted, folders):
    """Add the channels the sources have and the Texture Set lacks; the folders already there
    replace what lies under them in those too. Returns the labels added."""
    stack = texture_set.get_stack()
    added = []
    for entry in wanted:
        channel = getattr(textureset.ChannelType, entry["channel"])
        if stack.has_channel(channel):
            continue
        stack.add_channel(channel, getattr(textureset.ChannelFormat, entry["format"]), entry["label"] or None)
        for folder in folders:
            layerstack.get_node_by_uid(int(folder["group"])).set_blending_mode(layerstack.BlendingMode.Replace,
                                                                               channel)
        added.append(entry["label"] or entry["channel"])
    return added


def _mesh_maps(texture_set, held):
    maps = {}
    for usage_name, entry in sorted(held.items()):
        resource = project_imports.take_in(entry["held"], substance_painter.resource.Usage.TEXTURE,
                                           name="{0}_{1}_{2}".format(texture_set.name, usage_name, entry["hash"][:8]))
        identifier = resource.identifier()
        maps[usage_name] = {"name": identifier.name, "version": identifier.version}
    layout_state.hold_mesh_maps(texture_set, maps)
    return maps


def _build(texture_set, index, event, carried, mask, chosen, standing):
    """One event's folder: on top of the Texture Set's stack, or right under the Texture Set's own
    layers the source shows, which lie on the faces as they lay in the source; blending Replace, or
    as the folder it stands for blended, for one leaving with its faces. What it lays reading the
    layout of set 0 is made to read where the faces' paint is laid out, but what goes on reading the
    layout (``reads``)."""
    stack = texture_set.get_stack()
    own = [layerstack.get_node_by_uid(int(chosen.instances[int(root["uid"])]["shows"]))
           for root in carried["roots"] if root["carry"] == "own"]
    position = (layerstack.InsertPosition.below_node(own[-1]) if own
                else layerstack.InsertPosition.from_textureset_stack(stack))
    group = layerstack.insert_group(position)
    group.set_name("From {0}".format(carried["source"]))
    blending = chosen.folders.get(event, {})
    for channel in stack.all_channels():
        _set_blending(group, channel, *blending.get(channel, (layerstack.BlendingMode.Replace, 1.0)))
    mask_fill = _mask_fill(group, mask)
    inside = layerstack.InsertPosition.inside_node(group, layerstack.NodeStack.Substack)
    laid = [("own", node, None, "painted") for node in own]
    for root in reversed(carried["roots"]):
        if root["carry"] in ("skip", "own"):
            continue
        if root["carry"] == "instance":
            shown = chosen.instances[int(root["uid"])]
            made = layerstack.instantiate(inside, layerstack.get_node_by_uid(int(shown["shows"])))
            made.set_name(shown["name"])
            made.set_visible(shown["visible"])
            _blend(made, shown["blending"], stack, shown["home"])
            laid.append(("instance", made, None, root["reads"]))
            continue
        copy = chosen.copies[int(root["uid"])]
        if copy["kind"] == "group":
            made = layerstack.insert_smart_material(inside, copy["resource"])
            made.set_name(copy["shape"]["name"])
            made.set_visible(copy["shape"]["visible"])
            _blend_copy(made, copy["blending"])
        else:
            made = _lay_plain(inside, copy["plain"])
        laid.append(("copy", made, copy, root["reads"]))
    following = {uid for uid in _following(texture_set)}
    bound = set()
    stand_ins = []
    turned = set()
    for how, made, copy, reads in laid:
        if how == "copy":
            found, converted, repairs = {}, {}, []
            _match(copy["shape"], made, found, converted, repairs)
            for node, shape in repairs:
                _repair(layerstack.get_node_by_uid(found[shape["uid"]]), shape, chosen.channels[_home(carried)],
                        stack)
            for uid, path in sorted(dict(carried.get("normals") or {}).items()):
                if int(uid) in found:
                    _lay_normal(layerstack.get_node_by_uid(found[int(uid)]), path)
                    turned.add(int(uid))
            for uid, frozen in sorted(dict(carried.get("frozen") or {}).items()):
                if int(frozen["root"]) != copy["shape"]["uid"]:
                    continue
                layer = layerstack.get_node_by_uid(found[int(frozen["layer"])])
                effect = None if frozen["own"] else layerstack.get_node_by_uid(found[int(uid)])
                if frozen["own"] and frozen["mask"]:
                    for converted_uid in converted.get(int(frozen["layer"]), []):
                        layerstack.get_node_by_uid(converted_uid).set_visible(False)
                fill = paint_pixels.stand_in(layer, effect, frozen)
                stand_ins.append((fill, layer, frozen, effect))
        if reads == "painted":
            _carried_fills(made, index, following, bound)
    for uid in sorted({int(one) for one in dict(carried.get("normals") or {})} - turned):
        chosen.notes.append("{0}: a fill laying normals ({1}) lies under an instance in a copy, and keeps the "
                            "directions the old islands gave them".format(carried["source"], uid))
    for fill, layer, frozen, effect in stand_ins:
        layout_state.bind(fill.uid(), 0 if frozen["moved"] else index)
        standing[str(fill.uid())] = {"layer": layer.uid(), "mask": bool(frozen["mask"]),
                                     "effect": None if effect is None else effect.uid()}
    return ({"source": carried["source"], "group": group.uid(), "mask": mask_fill.uid(),
             "file": os.path.basename(mask)}, len(laid), len(stand_ins))


def _following(texture_set):
    """The fills that show a mesh map rather than a picture, and so read set 0 wherever they lie: one
    whose picture is a mesh map of the Texture Set it lives in shows, in every Texture Set showing it,
    that Texture Set's own map -- and the target's map holds the source's on the faces carried in --
    and one showing the target's own map follows it."""
    fills, _unplaceable, _unprojected = layout_state.readers()
    maps = {one.name: layout_state.mesh_map_pictures(one) for one in textureset.all_texture_sets()}
    return {fill.uid for fill in fills
            if texture_set.name in fill.following
            or fill.pictures & maps.get(layerstack.get_node_by_uid(fill.uid).get_texture_set().name, set())}


def _remask(entry, path):
    """Give an event's mask the picture of where its faces lie now, when that changed: Blender names
    a picture by its bytes."""
    if os.path.basename(path) == entry["file"]:
        return False
    _read_mask(layerstack.get_node_by_uid(int(entry["mask"])), path)
    entry["file"] = os.path.basename(path)
    return True


def after_surface(chosen):
    """Lay the folders of the events this surface carries, the mesh maps and channels they bring --
    and the resolution of a Texture Set the surface made -- take away the folders that left with
    their faces, and give every other event's mask where its faces lie now. Returns one line about
    it."""
    names = {one.name: one for one in textureset.all_texture_sets()}
    events = known()
    standing = dict(project_facts.read(paint_pixels.CARRIED_PAINT_KEY) or {})
    built, layers, pixels, added, remasked, left = 0, 0, 0, [], 0, []
    targets = dict((chosen.carry or {}).get("targets") or {})
    for target, entry in sorted(targets.items()):
        texture_set = names[target]
        if entry.get("resolution"):
            width, height = entry["resolution"]
            held = texture_set.get_resolution()
            if (held.width, held.height) != (width, height):
                texture_set.set_resolution(textureset.Resolution(width, height))
        added += ["{0} in {1}".format(one, target) for one in _channels(
            texture_set, entry.get("channels") or [], list(events.get(target, {}).values()))]
        if chosen.mesh_maps.get(target):
            _mesh_maps(texture_set, chosen.mesh_maps[target])
        index = int(chosen.guests[target]["index"])
        for event, carried in sorted(entry["events"].items()):
            mask = chosen.guests[target]["events"][event]["mask"]
            events.setdefault(target, {})[event], count, stood = _build(
                texture_set, index, event, carried, mask, chosen, standing)
            built += 1
            layers += count
            pixels += stood
    for target, entry in sorted(targets.items()):
        for event, carried in sorted(entry["events"].items()):
            if carried["holder"]:
                layerstack.delete_node(layerstack.get_node_by_uid(int(events[carried["holder"]].pop(event)["group"])))
                left.append("{0} -> {1}".format(carried["holder"], target))
    for target, entry in sorted(chosen.guests.items()):
        for event, guest in sorted(entry["events"].items()):
            if event in events.get(target, {}) and _remask(events[target][event], guest["mask"]):
                remasked += 1
    project_facts.write(GUESTS_KEY, {target: listed for target, listed in events.items() if listed})
    project_facts.write(paint_pixels.CARRIED_PAINT_KEY, {uid: record for uid, record in standing.items()
                                                          if paint_pixels.exists(uid)})
    if not built and not remasked:
        return ""
    line = "{0} folder(s) carry {1} layer(s) of paint from other Texture Sets, {2} piece(s) of it as pixels".format(
        built, layers, pixels)
    if left:
        line += "; folders gone with their faces: {0}".format(", ".join(sorted(set(left))))
    if added:
        line += "; channels added: {0}".format(", ".join(added))
    if remasked:
        line += "; {0} mask(s) follow their faces".format(remasked)
    for note in chosen.notes:
        LOG.warning("%s", note)
    if chosen.notes:
        line += "; " + "; ".join(chosen.notes)
    LOG.info(line)
    return line
