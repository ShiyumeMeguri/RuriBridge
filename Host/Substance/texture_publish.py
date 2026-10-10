# -*- coding: utf-8 -*-
"""Rendering Painter's channels into the Blender document's textures folder.

Which maps exist and which channels feed them is not a table kept here: it comes
from a Painter export preset via ``list_output_maps``, which is Painter's own
answer for the stack in front of it. This module only overrides two things per
map -- the file format and the bit depth -- and it derives both from the format
of the channels that map reads, so a 16-bit channel is never quietly written out
as 8-bit.

The files land in the folder the Blender document names, under names made from
the Texture Set and the map. They are the textures from then on: written once,
read in place, and found again tomorrow.

Colour space is settled from ``ChannelFormat``, whose documented storage column
is the one place Painter says whether a channel is held sRGB-encoded or linear.
"""

from __future__ import annotations

import contextlib
import os
import re

import substance_painter.export
import substance_painter.layerstack
import substance_painter.project
import substance_painter.textureset

from ...Kernel import record as record_module
from ...Kernel.log import logger

from . import (guest_state, layout_state, material_seed, mesh_ingest, paint_pixels, project_facts, shader_state,
               slot_recipe)

LOG = logger("painter.textures")

DEFAULT_PRESET_NAME = "Document channels + Normal + AO (No Alpha)"
PADDING_ALGORITHM = "infinite"

WILDCARDS = ("colorSpace", "sceneMaterial", "textureSet", "uvTileName",
             "project", "mesh", "udim")
_TOKEN = re.compile(r"\$(" + "|".join(WILDCARDS) + ")")
_LEFTOVER_TOKEN = re.compile(r"\$\w+")
_EMPTY_GROUP = re.compile(r"[(\[{][_\-. ]*[)\]}]")
_SEPARATORS = "_-. "
_UNSAFE_IN_FILE_NAMES = re.compile(r'[\\/:*?"<>|\s]+')


class TexturePublishError(RuntimeError):
    """An export that cannot be configured or that produced nothing."""


def available_preset_names():
    """Every export preset name Painter will accept, both catalogues."""
    names = [preset.name for preset in substance_painter.export.list_predefined_export_presets()]
    names.extend(preset.resource_id.name
                 for preset in substance_painter.export.list_resource_export_presets())
    return names


def _output_maps_for(preset_name, stack):
    for preset in substance_painter.export.list_predefined_export_presets():
        if preset.name == preset_name:
            return preset.list_output_maps(stack)
    for preset in substance_painter.export.list_resource_export_presets():
        if preset.resource_id.name == preset_name:
            return preset.list_output_maps()
    raise TexturePublishError(
        "no export preset named {0!r}; Painter offers {1}".format(
            preset_name, ", ".join(available_preset_names())))


def _channels_by_name(stack):
    return {channel_type.name.lower(): channel
            for channel_type, channel in stack.all_channels().items()}


def _map_key(file_name, document_names, other_names, index):
    """A stable name for one output map, preferring what the preset already calls it.

    The wildcards are stripped by the documented vocabulary rather than by a
    general word pattern: Painter's separator is an underscore, which a word
    pattern swallows along with the name after it, leaving every map called the
    same thing.

    What is left becomes part of a file name, and it can carry anything: Painter
    names a user channel's map after the channel's label, which is free text. A
    slash in it would put the file in a folder of its own, where nothing finds it.
    """
    leftover = _LEFTOVER_TOKEN.findall(_TOKEN.sub("", file_name))
    if leftover:
        LOG.warning("export preset uses wildcards this build does not know: %s",
                    ", ".join(sorted(set(leftover))))
    stripped = _LEFTOVER_TOKEN.sub("", _TOKEN.sub("", file_name))
    stripped = _UNSAFE_IN_FILE_NAMES.sub("_", _EMPTY_GROUP.sub("", stripped)).strip(_SEPARATORS)
    while "__" in stripped:
        stripped = stripped.replace("__", "_")
    if stripped:
        return stripped
    if document_names:
        return "_".join(sorted(document_names))
    if other_names:
        return "_".join(sorted(other_names))
    return "map{0}".format(index)


def _map_sources(output_map, channels_by_name):
    """Which document channels a map reads, and what else it reads besides."""
    document_names = []
    other_names = []
    for entry in output_map.get("channels", []):
        name = str(entry.get("srcMapName", ""))
        if not name:
            continue
        if entry.get("srcMapType") == "documentMap":
            lowered = name.lower()
            if lowered not in document_names:
                document_names.append(lowered)
        elif name not in other_names:
            other_names.append(name)
    channels = [channels_by_name[name] for name in document_names
                if name in channels_by_name]
    return document_names, other_names, channels


def _format_override(channels):
    """File format and bit depth wide enough for every channel feeding a map."""
    if not channels:
        return "png", "8", False, False
    bit_depth = max(channel.bit_depth() for channel in channels)
    floating = any(channel.is_floating() for channel in channels)
    is_color = any(channel.is_color() for channel in channels)
    if floating:
        return "exr", ("32f" if bit_depth >= 32 else "16f"), True, is_color
    return "png", str(bit_depth), False, is_color


def _color_space(channels):
    """One colour space for the map, or data when its sources disagree."""
    spaces = {record_module.color_space_for(channel.format().name)
              for channel in channels}
    if len(spaces) == 1:
        return spaces.pop()
    if spaces:
        LOG.warning("map sources disagree on colour space (%s); treating it as data",
                    ", ".join(sorted(spaces)))
    return record_module.COLOR_SPACE_DATA


def _template_pattern(file_name):
    parts = []
    cursor = 0
    for match in _TOKEN.finditer(file_name):
        parts.append(re.escape(file_name[cursor:match.start()]))
        token = match.group(1)
        parts.append("(?P<udim>[0-9]*)" if token == "udim" else ".*?")
        cursor = match.end()
    parts.append(re.escape(file_name[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


class PlannedMap:
    """One output map: how it is written and how to recognise its files."""

    __slots__ = ("key", "file_name", "pattern", "file_format", "bit_depth",
                 "is_floating", "is_color", "color_space", "source_channels", "definition")

    def __init__(self, key, file_name, file_format, bit_depth, is_floating, is_color,
                 color_space, source_channels, definition):
        self.key = key
        self.file_name = file_name
        self.pattern = _template_pattern(file_name)
        self.file_format = file_format
        self.bit_depth = bit_depth
        self.is_floating = is_floating
        self.is_color = is_color
        self.color_space = color_space
        self.source_channels = source_channels
        self.definition = definition


def plan_stack(preset_name, texture_set, stack, stem_suffix=""):
    """Every map this stack will write, named ``<Texture Set>[_<suffix>]_<map>``."""
    channels_by_name = _channels_by_name(stack)
    tiled = texture_set.has_uv_tiles()
    stem = "$textureSet" + ("_" + stem_suffix if stem_suffix else "")
    planned = []
    for index, output_map in enumerate(_output_maps_for(preset_name, stack)):
        document_names, other_names, channels = _map_sources(output_map, channels_by_name)
        key = _map_key(output_map.get("fileName", ""), document_names, other_names, index)
        if any(entry.key == key for entry in planned):
            raise TexturePublishError(
                "preset {0!r} produces two maps that both resolve to the name {1!r} for "
                "stack {2}; they would overwrite each other on disk".format(
                    preset_name, key, stack))
        file_format, bit_depth, floating, is_color = _format_override(channels)
        file_name = (stem + "_$udim_" + key) if tiled else (stem + "_" + key)
        definition = dict(output_map)
        definition["fileName"] = file_name
        parameters = dict(definition.get("parameters", {}))
        parameters.update({
            "fileFormat": file_format,
            "bitDepth": bit_depth,
            "dithering": False,
            "paddingAlgorithm": PADDING_ALGORITHM,
        })
        definition["parameters"] = parameters
        planned.append(PlannedMap(key, file_name, file_format, bit_depth, floating,
                                  is_color, _color_space(channels),
                                  document_names + other_names, definition))
    return planned


def build_configuration(export_directory, preset_name, texture_sets, stem_suffix=""):
    """The export JSON, plus the plan needed to read its output back."""
    presets = []
    export_list = []
    plan_by_stack = {}
    for texture_set in texture_sets:
        for stack in texture_set.all_stacks():
            planned = plan_stack(preset_name, texture_set, stack, stem_suffix)
            if not planned:
                continue
            generated_name = "ruri_{0}".format(str(stack).replace("/", "_"))
            presets.append({"name": generated_name,
                            "maps": [entry.definition for entry in planned]})
            export_list.append({"rootPath": str(stack), "exportPreset": generated_name})
            plan_by_stack[(texture_set.name, stack.name())] = (texture_set, stack, planned)
    if not export_list:
        raise TexturePublishError("nothing to export: no stack has a channel")
    configuration = {
        "exportShaderParams": False,
        "exportPath": str(export_directory),
        "exportPresets": presets,
        "defaultExportPreset": presets[0]["name"],
        "exportList": export_list,
        "exportParameters": [{"parameters": {"paddingAlgorithm": PADDING_ALGORITHM,
                                             "dithering": False}}],
    }
    return configuration, plan_by_stack


def _attribute(paths, planned, export_directory):
    """Match every written file back to the map whose template named it."""
    by_key = {}
    unmatched = []
    for path in paths:
        stem, _extension = os.path.splitext(os.path.basename(path))
        for entry in planned:
            if entry.pattern.match(stem) is None:
                continue
            by_key.setdefault(entry.key, []).append(
                os.path.relpath(path, export_directory).replace("\\", "/"))
            break
        else:
            unmatched.append(path)
    return by_key, unmatched


def _selected_layers():
    """The layers selected in the active stack, with an effect standing for its layer."""
    stack = substance_painter.textureset.get_active_stack()
    chosen = []
    for node in substance_painter.layerstack.get_selected_nodes(stack):
        while node is not None and not isinstance(node, substance_painter.layerstack.LayerNode):
            node = node.get_parent()
        if node is not None and node not in chosen:
            chosen.append(node)
    if not chosen:
        raise TexturePublishError("no layer is selected in Painter")
    return stack, chosen


@contextlib.contextmanager
def _only_these_visible(stack, chosen):
    """Hide every other layer of the stack for the duration, then put them back.

    The folders a chosen layer sits in stay visible, or nothing inside them would
    render; their other contents are hidden like everything else. A layer that
    was already hidden is left hidden and is not "restored" afterwards.
    """
    keep = set()
    for node in chosen:
        parent = node.get_parent()
        while parent is not None and isinstance(parent, substance_painter.layerstack.LayerNode):
            keep.add(parent)
            parent = parent.get_parent()
    hidden = []
    pending = list(substance_painter.layerstack.get_root_layer_nodes(stack))
    try:
        while pending:
            node = pending.pop()
            if node in chosen:
                continue
            if node in keep:
                children = getattr(node, "sub_layers", None)
                if callable(children):
                    pending.extend(children())
                continue
            if node.is_visible():
                node.set_visible(False)
                hidden.append(node)
        yield
    finally:
        for node in hidden:
            node.set_visible(True)


def _safe(name):
    return _UNSAFE_IN_FILE_NAMES.sub("_", name).strip("_") or "layer"


def _attach_recipes(entry, shader, manifests):
    """Say how the generated shader this Texture Set's materials run is stood up again
    from this export, or why it is not. Only for the very shader on this shelf: another
    generation of it reads its channels differently, and a recipe written from the
    wrong table puts every lane in the wrong place without a word."""
    if not shader["name"]:
        return
    if shader["name"] not in manifests:
        manifests[shader["name"]] = shader_state.shader_manifest(shader["name"])
    manifest = manifests[shader["name"]]
    shelved = str((manifest or {}).get("identity") or "")
    if not shelved or shelved != shader["identity"]:
        entry["slots_refused"] = {"": "{0} on this shelf is {1}, the material says {2}".format(
            shader["name"], shelved or "unstamped", shader["identity"] or "nothing")}
        return
    entry["slots"], refused = slot_recipe.recipes(manifest, entry["maps"])
    if refused:
        entry["slots_refused"] = refused


def publish(publisher, directory, preset_name=DEFAULT_PRESET_NAME, layer=False, shaders=None,
            texture_sets=None):
    """Export into the document's textures folder and say what landed where.

    With ``layer`` set, only the layer selected in Painter is rendered: its
    Texture Set alone, every other layer hidden for the length of the export, the
    files named after the layer so they never overwrite the Texture Set's own.
    Otherwise the whole of ``texture_sets`` is, or of every Texture Set when it is None.

    ``shaders`` names, per Texture Set, the generated shader its materials run and
    that shader's identity; a whole export carries the recipe that stands that
    shader's textures up from the maps (see ``slot_recipe``).
    """
    if not substance_painter.project.is_open():
        raise TexturePublishError("no project is open")
    if not directory:
        raise TexturePublishError("Blender has not said where its textures live; "
                                  "save the .blend and attach it")
    os.makedirs(directory, exist_ok=True)
    if layer:
        stack, chosen = _selected_layers()
        texture_sets = [stack.material()]
        suffix = _safe("_".join(node.get_name() for node in chosen))
        isolation = _only_these_visible(stack, chosen)
    else:
        texture_sets = list(substance_painter.textureset.all_texture_sets()
                            if texture_sets is None else texture_sets)
        suffix = ""
        isolation = contextlib.nullcontext()

    with isolation:
        configuration, plan_by_stack = build_configuration(
            directory, preset_name, texture_sets, suffix)
        result = substance_painter.export.export_project_textures(configuration)
    if result.status != substance_painter.export.ExportStatus.Success:
        LOG.warning("export finished as %s: %s", result.status, result.message)

    exported = []
    manifests = {}
    for identity, paths in result.textures.items():
        entry = plan_by_stack.get(identity)
        if entry is None:
            LOG.warning("Painter exported stack %s which was not planned", identity)
            continue
        texture_set, stack, planned = entry
        by_key, unmatched = _attribute(paths, planned, directory)
        for path in unmatched:
            LOG.warning("exported file %s matched no planned map", path)
        maps = []
        for planned_map in planned:
            files = by_key.get(planned_map.key)
            if not files:
                continue
            maps.append({
                "channel": planned_map.key,
                "files": sorted(files),
                "file_format": planned_map.file_format,
                "bit_depth": planned_map.bit_depth,
                "is_color": planned_map.is_color,
                "color_space": planned_map.color_space,
                "source_channels": planned_map.source_channels,
            })
        resolution = texture_set.get_resolution()
        entry = {
            "name": texture_set.name,
            "stack": stack.name(),
            "layer": suffix,
            "resolution": [resolution.width, resolution.height],
            "maps": maps,
        }
        shader = (shaders or {}).get(texture_set.name)
        if shader is not None and not layer:
            _attach_recipes(entry, shader, manifests)
        exported.append(entry)
    if not exported:
        raise TexturePublishError("the export wrote nothing: {0}".format(result.message))
    return publisher.publish_record(record_module.textures(
        "Substance", substance_painter.project.file_path(), directory, exported))


def rename(renames):
    """Rename Texture Sets, old name to new. The one edit that keeps every layer.

    Done in two steps through names nobody uses, so swapping two names, or
    renaming A to B while B is renamed away, never collides half-way. A target
    that is already taken by a Texture Set not being renamed is refused before
    anything moves.
    """
    by_name = {texture_set.name: texture_set
               for texture_set in substance_painter.textureset.all_texture_sets()}
    wanted = {old: new for old, new in renames.items() if old in by_name and old != new}
    missing = sorted(set(renames) - set(by_name))
    if missing:
        LOG.warning("asked to rename Texture Sets this project does not have: %s",
                    ", ".join(missing))
    staying = set(by_name) - set(wanted)
    taken = sorted(new for new in wanted.values() if new in staying)
    if taken:
        raise TexturePublishError(
            "this project already has Texture Sets called {0}; rename or remove "
            "those first".format(", ".join(taken)))
    if len(set(wanted.values())) != len(wanted):
        raise TexturePublishError("two Texture Sets cannot both be called the same thing")
    for index, old in enumerate(sorted(wanted)):
        by_name[old].name = "__ruri_rename_{0}".format(index)
    for index, old in enumerate(sorted(wanted)):
        by_name[old].name = wanted[old]
    layout_state.rename(wanted)
    guest_state.rename(wanted)
    material_seed.rename(wanted)
    paint_pixels.rename(wanted)
    if wanted:
        LOG.info("renamed %s", ", ".join("{0} -> {1}".format(old, new)
                                         for old, new in sorted(wanted.items())))
    return wanted


def map_names(texture_set):
    """The maps a whole export of this Texture Set writes, by name, in the preset's order:
    what a material that is not generated takes its textures from, mapped on the Blender
    side by the material's own table."""
    names = []
    for stack in texture_set.all_stacks():
        for planned in plan_stack(DEFAULT_PRESET_NAME, texture_set, stack):
            if planned.key not in names:
                names.append(planned.key)
    return names


def _texture_set_state(texture_set):
    resolution = texture_set.get_resolution()
    state = {"name": texture_set.name, "layers": mesh_ingest.layer_count(texture_set),
             "resolution": [resolution.width, resolution.height]}
    try:
        state["maps"] = map_names(texture_set)
    except TexturePublishError as error:
        state["maps"] = []
        state["maps_refused"] = str(error)
    return state


def current_project_state():
    """What Painter has open: the project file, every Texture Set with its layers, its resolution
    and the maps an export of it writes, the frame its surface lives in and the fingerprints of the
    surface it holds."""
    if not substance_painter.project.is_open():
        return record_module.presence("Substance", "")
    texture_sets = [_texture_set_state(texture_set)
                    for texture_set in substance_painter.textureset.all_texture_sets()]
    return record_module.presence(
        "Substance", substance_painter.project.file_path() or "(unsaved project)",
        texture_sets=sorted(texture_sets, key=lambda entry: entry["name"]),
        frame_of_project=mesh_ingest.project_frame(),
        surface=project_facts.read(layout_state.SURFACE_KEY) or {},
        guests={name: {event: entry["source"] for event, entry in events.items()}
                for name, events in guest_state.known().items()})
