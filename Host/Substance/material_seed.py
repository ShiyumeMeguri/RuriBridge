# -*- coding: utf-8 -*-
"""A Texture Set started from the material Blender paints it with.

A generated material samples its own textures -- the base map, the packed normal, the
gloss map, and the ramps, lookups and masks its shader reads whole. When the Texture
Set runs the very same shader (by identity), Painter can stand that material up as it
is: every paintable input the shelf manifest declares gets the lanes of the material's
texture it comes from, an input Painter combines with its own bake (the tangent normal,
the occlusion) becomes the Texture Set's mesh map, and every texture the shader reads
whole becomes a parameter of the Texture Set's own shader instance.

Which input is which lanes of which texture, through which operation, is the manifest's
``inputs`` -- the table the importer reads when it brings a game material in. Painter's
Python has no numerics, so the per-pixel part is asked of the application that holds the
textures (``jobs`` below is the asking, ``apply`` the answer).

Nothing is removed:

* the channels go into one fill layer of the bridge's own at the bottom of the stack,
  found again and refilled on the next sync, never stacked twice -- every layer somebody
  painted stays above it and keeps covering what it covered;
* a channel the stack lacks is added, in the format the manifest states and, for a user
  channel, labelled with the input's semantic name -- the label the importer gives it,
  and the one Painter's export names the channel's file after;
* a mesh map is set where the Texture Set has none and refreshed where it is the one the
  bridge set; a bake somebody made is not the bridge's to replace, and nothing is imported
  for it;
* a texture already taken in with the same bytes is used again rather than imported twice;
  one whose bytes changed comes in anew, and the one it replaced leaves the project at the
  next save (``project_imports``);
* a texture is imported from where it is held until the project closes (``held_imports``),
  never from the delivery, which the transport retires before Painter may have read it.
"""

from __future__ import annotations

import os

import substance_painter.layerstack as layerstack
import substance_painter.project
import substance_painter.resource
import substance_painter.textureset as textureset

from ...Kernel.log import logger

from . import held_imports, layout_state, project_facts, project_imports, shader_state

LOG = logger("painter.seed")

#: Per Texture Set: the uid of the bridge's layer, and per input the hash of the bytes
#: taken in and the resource they became.
SEED_KEY = "seed"
#: The kinds of manifest input a material's texture can feed.
FED_KINDS = ("NativeChannel", "OverflowChannel", "RawTexture")
#: The inputs that become parameters of the Texture Set's shader, which the shader reads
#: in the Texture Set's own layout: the one part of a stand-up a layout change does not
#: carry by itself -- its channels read their pictures through the UV set holding the old
#: layout, its mesh maps are laid out again with the surface.
SHADER_KINDS = ("RawTexture",)


def _wide(entry):
    """Whether an input wants more than eight bits: a normal, or a channel stored wider."""
    return entry.get("MeshMap") == "Normal" or any(
        width in str(entry.get("Format") or "") for width in ("16", "32"))


def jobs(manifest, images, kinds):
    """What to ask Blender to cut: one job per input of these kinds the manifest declares
    that one of the material's textures feeds -- through the first of its sources the
    material has."""
    found = []
    for entry in manifest.get("inputs") or []:
        if entry["Kind"] not in kinds:
            continue
        source = next((one for one in entry.get("Sources") or [] if one["Source"] in images), None)
        if source is None:
            continue
        found.append({"input": entry["Id"], "source": source["Source"],
                      "channels": source["Channels"], "operation": source["Operation"],
                      "wide": _wide(entry)})
    return found


def _channel_type(identifier):
    """This application's channel for a manifest input, matched by its own name."""
    wanted = identifier.lower()
    for name in dir(textureset.ChannelType):
        if name.lower() == wanted:
            return getattr(textureset.ChannelType, name)
    raise LookupError("Painter has no channel called {0}".format(identifier))


def _remembered():
    return dict(project_facts.read(SEED_KEY) or {})


def _remember(seeded):
    project_facts.write(SEED_KEY, seeded)


def seeded(names):
    """Of these Texture Sets, the ones the bridge stood up from a Blender material."""
    remembered = _remembered()
    return sorted(name for name in names if name in remembered)


def follow_mesh_maps(replaced):
    """A mesh map the bridge set and a layout change replaced with the same map laid out
    anew stays the bridge's: its record follows, so the next delivery replaces it as it
    would have replaced the old one. ``replaced`` is ``layout_state.Applied.replaced``."""
    seeded_now = _remembered()
    changed = False
    for name, maps in replaced.items():
        state = seeded_now.get(name)
        if not state:
            continue
        known = dict(state.get("inputs") or {})
        for key, record in known.items():
            for old, new in maps.values():
                if record.get("name") == old["name"] and record.get("version") == old["version"]:
                    known[key] = {"hash": "", "name": new["name"], "version": new["version"]}
                    changed = True
        state["inputs"] = known
    if changed:
        _remember(seeded_now)


def _is_record(identifier, record):
    """Whether a resource is the one a record says the bridge took in."""
    return (record is not None and identifier.name == record.get("name")
            and identifier.version == record.get("version"))


def _resource(item, directory, known, label):
    """The project resource for one delivered file: the one already taken in when the
    bytes are the same, else a fresh import named after it. Returns its url.

    A resource is remembered by its name and version, not its url: the url carries the
    context the project was given when it was opened, and that changes every time."""
    held = known.get(item["input"]) or {}
    if held.get("hash") == item["hash"] and held.get("name"):
        identifier = substance_painter.resource.ResourceID.from_project(
            held["name"], held.get("version"))
        if substance_painter.resource.Resource.retrieve(identifier):
            return identifier.url()
    resource = project_imports.take_in(
        held_imports.hold(os.path.join(directory, item["file"]), item["hash"]),
        substance_painter.resource.Usage.TEXTURE, name=label)
    identifier = resource.identifier()
    known[item["input"]] = {"hash": item["hash"], "name": identifier.name,
                            "version": identifier.version}
    return identifier.url()


def _bridge_layer(stack, remembered_uid, material):
    """The bridge's fill layer in this stack: the one made last time, else a new one at
    the bottom, under everything already there."""
    if remembered_uid is not None:
        try:
            node = layerstack.get_node_by_uid(int(remembered_uid))
            if (isinstance(node, layerstack.FillLayerNode)
                    and node.get_texture_set().name == stack.material().name):
                return node
        except ValueError:
            pass
    roots = layerstack.get_root_layer_nodes(stack)
    position = (layerstack.InsertPosition.below_node(roots[-1]) if roots
                else layerstack.InsertPosition.from_textureset_stack(stack))
    layer = layerstack.insert_fill(position)
    layer.set_name("Blender: {0}".format(material))
    return layer


def apply(entry, directory):
    """Put one Texture Set's delivered inputs in place. Returns what was done."""
    name = entry["name"]
    texture_set = textureset.TextureSet.from_name(name)
    stack = texture_set.get_stack()
    manifest = shader_state.shader_manifest(entry["shader"])
    if manifest is None:
        raise LookupError("no manifest for {0} on this shelf".format(entry["shader"]))
    declared = {one["Id"]: one for one in manifest.get("inputs") or []}
    seeded = _remembered()
    state = dict(seeded.get(name) or {})
    known = dict(state.get("inputs") or {})
    report = {"channels": [], "added_channels": [], "mesh_maps": [], "kept_mesh_maps": [],
              "parameters": [], "missing": dict(entry.get("missing") or {})}
    channels = {}
    parameters = {}
    for item in entry["inputs"]:
        input_entry = declared.get(item["input"])
        if input_entry is None:
            report["missing"][item["input"]] = "the manifest on this shelf declares no such input"
            continue
        resource_name = "{0}_{1}".format(name, item["input"])
        baked = input_entry.get("MeshMap") or ""
        if baked:
            usage = getattr(textureset.MeshMapUsage, baked)
            held = texture_set.get_mesh_map_resource(usage)
            if held is not None and not _is_record(held, known.get(item["input"])):
                known.pop(item["input"], None)
                report["kept_mesh_maps"].append(baked)
                continue
            url = _resource(item, directory, known, resource_name)
            if held is None or not _is_record(held, known[item["input"]]):
                texture_set.set_mesh_map_resource(
                    usage, substance_painter.resource.ResourceID.from_url(url))
            report["mesh_maps"].append(baked)
            continue
        url = _resource(item, directory, known, resource_name)
        if input_entry["Kind"] == "RawTexture":
            parameters[item["input"]] = url
            continue
        channel_type = _channel_type(input_entry["Id"])
        if not stack.has_channel(channel_type):
            label = input_entry.get("Semantic") if input_entry["Kind"] == "OverflowChannel" else None
            stack.add_channel(channel_type, getattr(textureset.ChannelFormat, input_entry["Format"]),
                              label)
            report["added_channels"].append(input_entry["Id"])
        channels[channel_type] = url
    if channels:
        layer = _bridge_layer(stack, state.get("layer"), entry["material"])
        # The material's textures are laid out in its current layout, which is set 0.
        layout_state.read_layout(layer)
        layer.active_channels = set(channels)
        for channel_type, url in channels.items():
            layer.set_source(channel_type, substance_painter.resource.ResourceID.from_url(url))
        state["layer"] = layer.uid()
        report["channels"] = sorted(str(one).split(".")[-1] for one in channels)
    if parameters:
        layout = shader_state.Layout()
        identifier = layout.instance_by_texture_set.get(name)
        running = layout.shader_by_instance.get(identifier, "") if identifier is not None else ""
        if running != entry["shader"]:
            report["missing"].update({key: "the Texture Set runs {0}, not {1}: pull the shader "
                                           "first".format(running or "no shader", entry["shader"])
                                      for key in parameters})
        else:
            shader_state.set_parameters(identifier, parameters)
            report["parameters"] = sorted(parameters)
    state["inputs"] = known
    seeded[name] = state
    _remember(seeded)
    for key, why in sorted(report["missing"].items()):
        LOG.warning("%s: %s not set: %s", name, key or "nothing", why)
    LOG.info("%s from %s: channels %s, mesh maps %s (kept %s), shader textures %s", name,
             entry["material"], report["channels"], report["mesh_maps"],
             report["kept_mesh_maps"], report["parameters"])
    return report
