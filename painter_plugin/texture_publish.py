# -*- coding: utf-8 -*-
"""Rendering Painter's channels into the arena and publishing what came out.

Which maps exist and which channels feed them is not a table kept here: it comes
from a Painter export preset via ``list_output_maps``, which is Painter's own
answer for the stack in front of it. This module only overrides two things per
map -- the file format and the bit depth -- and it derives both from the format
of the channels that map reads, so a 16-bit channel is never quietly written out
as 8-bit. That is what makes the round trip lossless.

Only the file names are the bridge's own, and deliberately so: naming the maps
here is what lets an exported path be attributed back to the map that produced
it without guessing.

Colour space is settled from ``ChannelFormat``, whose documented storage column
is the one place Painter says whether a channel is held sRGB-encoded or linear.
"""

from __future__ import annotations

import os
import re

import substance_painter.export
import substance_painter.project
import substance_painter.textureset

from ruri_bridge import record as record_module
from ruri_bridge.log import logger

LOG = logger("painter.textures")

DEFAULT_PRESET_NAME = "Document channels + Normal + AO (No Alpha)"
PADDING_ALGORITHM = "infinite"

WILDCARDS = ("colorSpace", "sceneMaterial", "textureSet", "uvTileName",
             "project", "mesh", "udim")
_TOKEN = re.compile(r"\$(" + "|".join(WILDCARDS) + ")")
_LEFTOVER_TOKEN = re.compile(r"\$\w+")
_EMPTY_GROUP = re.compile(r"[(\[{][_\-. ]*[)\]}]")
_SEPARATORS = "_-. "


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
    same thing and every exported file matching the first map's template.
    """
    leftover = _LEFTOVER_TOKEN.findall(_TOKEN.sub("", file_name))
    if leftover:
        LOG.warning("export preset uses wildcards this build does not know: %s",
                    ", ".join(sorted(set(leftover))))
    stripped = _LEFTOVER_TOKEN.sub("", _TOKEN.sub("", file_name))
    stripped = _EMPTY_GROUP.sub("", stripped).strip(_SEPARATORS)
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


def plan_stack(preset_name, texture_set, stack):
    """Every map this stack will write, with the bridge's own file names."""
    channels_by_name = _channels_by_name(stack)
    tiled = texture_set.has_uv_tiles()
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
        file_name = ("$textureSet_$udim_" + key) if tiled else ("$textureSet_" + key)
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


def _touched_by(planned_map, dirty_channels):
    """Whether one map has to be re-rendered for this set of changed channels.

    A map with no document channel behind it is derived from the stack by
    Painter -- a converted normal, a mixed occlusion -- and there is no way from
    here to say which channel it was derived from, so it re-renders whenever
    anything in its stack did.
    """
    document_sources = [name for name in planned_map.source_channels if name.islower()]
    if not document_sources:
        return True
    return any(name in dirty_channels for name in document_sources)


def build_configuration(export_directory, preset_name, selected_texture_sets=None,
                        dirty_channels_by_texture_set=None):
    """The export JSON, plus the plan needed to read its output back."""
    if not substance_painter.project.is_open():
        raise TexturePublishError("no project is open")
    presets = []
    export_list = []
    plan_by_stack = {}
    for texture_set in substance_painter.textureset.all_texture_sets():
        if selected_texture_sets and texture_set.name() not in selected_texture_sets:
            continue
        for stack in texture_set.all_stacks():
            planned = plan_stack(preset_name, texture_set, stack)
            if dirty_channels_by_texture_set is not None:
                dirty = dirty_channels_by_texture_set.get(texture_set.name(), set())
                planned = [entry for entry in planned if _touched_by(entry, dirty)]
            if not planned:
                continue
            generated_name = "ruri_{0}".format(str(stack).replace("/", "_"))
            presets.append({"name": generated_name,
                            "maps": [entry.definition for entry in planned]})
            export_list.append({"rootPath": str(stack), "exportPreset": generated_name})
            plan_by_stack[(texture_set.name(), stack.name())] = (texture_set, stack, planned)
    if not export_list:
        raise TexturePublishError("the open project has no stack with any channel to export")
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
            match = entry.pattern.match(stem)
            if match is None:
                continue
            by_key.setdefault(entry.key, []).append(
                os.path.relpath(path, export_directory).replace("\\", "/"))
            break
        else:
            unmatched.append(path)
    return by_key, unmatched


def publish(arena, publisher, preset_name=DEFAULT_PRESET_NAME, selected_texture_sets=None,
            dirty_channels_by_texture_set=None):
    """Export channels into a fresh generation and publish it.

    Passing the changed channels turns this into an incremental publish: only
    those maps are rendered and written, which is what makes a paint stroke cost
    one map rather than a whole Texture Set.
    """
    with publisher.staging() as staging:
        export_directory = staging.path(record_module.TEXTURE_DIRECTORY_NAME)
        export_directory.mkdir(parents=True, exist_ok=True)

        configuration, plan_by_stack = build_configuration(
            export_directory, preset_name, selected_texture_sets,
            dirty_channels_by_texture_set)
        result = substance_painter.export.export_project_textures(configuration)
        if result.status != substance_painter.export.ExportStatus.Success:
            LOG.warning("export finished as %s: %s", result.status, result.message)

        texture_sets = []
        for identity, paths in result.textures.items():
            entry = plan_by_stack.get(identity)
            if entry is None:
                LOG.warning("Painter exported stack %s which was not planned", identity)
                continue
            texture_set, stack, planned = entry
            by_key, unmatched = _attribute(paths, planned, export_directory)
            for path in unmatched:
                LOG.warning("exported file %s matched no planned map", path)
            resolution = texture_set.get_resolution()
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
                    "is_floating": planned_map.is_floating,
                    "is_color": planned_map.is_color,
                    "color_space": planned_map.color_space,
                    "source_channels": planned_map.source_channels,
                })
            texture_sets.append({
                "identity": texture_set.original_name,
                "name": texture_set.name(),
                "stack": stack.name(),
                "resolution": [resolution.width, resolution.height],
                "maps": maps,
            })

        return staging.publish(record_module.textures(
            source="painter",
            project_path=substance_painter.project.file_path(),
            mesh_path=substance_painter.project.last_imported_mesh_path(),
            texture_sets=texture_sets))


def apply_display_names(names_by_identity):
    """Show the readable material names Blender knows, keyed by identity.

    The name Blender puts in the mesh is an identity, so that renaming a material
    there cannot arrive here as a different material and strand the paint. That
    identity is what ``original_name`` reports for ever after, and it is also what
    the Texture Set would be called in the UI -- which is unreadable. So the
    display name is set from what Blender calls the material today, and reset
    whenever Blender says it changed.
    """
    renamed = {}
    for texture_set in substance_painter.textureset.all_texture_sets():
        wanted = names_by_identity.get(texture_set.original_name)
        if not wanted or texture_set.name() == wanted:
            continue
        try:
            texture_set.name = wanted
        except ValueError as error:
            LOG.warning("cannot show %r as %r: %s", texture_set.original_name, wanted, error)
            continue
        renamed[texture_set.original_name] = wanted
    if renamed:
        LOG.info("renamed %d Texture Set(s) to follow Blender", len(renamed))
    return renamed


def current_project_state():
    """What Painter has open, for the panel and for Blender's status line."""
    if not substance_painter.project.is_open():
        return record_module.project_state("painter", False, None, None, [])
    texture_sets = []
    for texture_set in substance_painter.textureset.all_texture_sets():
        resolution = texture_set.get_resolution()
        stacks = []
        for stack in texture_set.all_stacks():
            stacks.append({
                "name": stack.name(),
                "channels": sorted(channel_type.name
                                   for channel_type in stack.all_channels()),
            })
        texture_sets.append({
            "identity": texture_set.original_name,
            "name": texture_set.name(),
            "resolution": [resolution.width, resolution.height],
            "stacks": stacks,
        })
    return record_module.project_state(
        "painter", True,
        substance_painter.project.file_path(),
        substance_painter.project.last_imported_mesh_path(),
        texture_sets)
