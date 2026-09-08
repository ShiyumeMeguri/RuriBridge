# -*- coding: utf-8 -*-
"""The wire contract: which channels exist and what a generation carries.

A generation directory always holds ``record.json``. Bulk payloads sit beside it
under names this module names, so a consumer never guesses a path: it reads the
record and follows what the record says is there.

Every record is a plain dictionary. That is the point -- the bridge moves data
and does not know what either host will do with it. The material rows a mesh
record carries, for instance, are whatever the producing side considers a
material; the bridge neither interprets nor validates them, so a shader
generator can change its vocabulary without this module moving.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FORMAT_VERSION = 1

CHANNEL_TO_PAINTER = "to_painter"
CHANNEL_TO_BLENDER = "to_blender"
CHANNEL_STATE_TO_PAINTER = "state_to_painter"
CHANNEL_STATE_TO_BLENDER = "state_to_blender"
CHANNELS = (CHANNEL_TO_PAINTER, CHANNEL_TO_BLENDER,
            CHANNEL_STATE_TO_PAINTER, CHANNEL_STATE_TO_BLENDER)
QUEUED_CHANNELS = (CHANNEL_TO_PAINTER, CHANNEL_TO_BLENDER)
STATE_CHANNELS = (CHANNEL_STATE_TO_PAINTER, CHANNEL_STATE_TO_BLENDER)

KIND_MESH = "mesh"
KIND_MESH_REQUEST = "mesh_request"
KIND_EXPORT_REQUEST = "export_request"
KIND_TEXTURES = "textures"
KIND_PROJECT_STATE = "project_state"
KIND_SHADER_STATE = "shader_state"
KIND_SHADER_VALUES = "shader_values"

INTENT_AUTO = "auto"
INTENT_CREATE_PROJECT = "create_project"
INTENT_RELOAD_MESH = "reload_mesh"

RECORD_FILE_NAME = "record.json"
SCENE_FILE_NAME = "scene.glb"
TEXTURE_DIRECTORY_NAME = "maps"

COLOR_SPACE_SRGB = "sRGB"
COLOR_SPACE_DATA = "Non-Color"

CHANNEL_FORMAT_SRGB8 = "sRGB8"


class RecordError(RuntimeError):
    """A record that is absent, unreadable, or of an unexpected shape."""


def write(generation_directory, record):
    """Write the record last, after every payload beside it is complete."""
    directory = Path(generation_directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True)
    path = directory / RECORD_FILE_NAME
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path


def read(generation_directory):
    directory = Path(generation_directory)
    path = directory / RECORD_FILE_NAME
    if not path.exists():
        raise RecordError("generation {0} has no {1}".format(directory, RECORD_FILE_NAME))
    with open(path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    version = record.get("format_version")
    if version != FORMAT_VERSION:
        raise RecordError(
            "generation {0} is record format {1}, this build speaks {2}".format(
                directory, version, FORMAT_VERSION))
    return record


def _base(kind, source):
    return {
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "source": source,
        "process_id": os.getpid(),
    }


def mesh(source, intent, scene, materials, unit_scale, up_axis):
    """Blender -> Painter: geometry lives in the GLB, everything else here.

    ``scene`` describes what went into the GLB (object names, primitive counts,
    the bounds) so the consumer can report and verify without parsing it.
    ``materials`` are the producing side's material rows, carried verbatim.
    """
    record = _base(KIND_MESH, source)
    record.update({
        "intent": intent,
        "scene_file": SCENE_FILE_NAME,
        "scene": scene,
        "materials": materials,
        "unit_scale": unit_scale,
        "up_axis": up_axis,
    })
    return record


def mesh_request(source):
    """Painter -> Blender: send me the scene as it stands.

    The symmetric counterpart of an export request. Painter cannot read a
    Blender scene, so asking is the only move it has, and having it is what lets
    the two panels offer the same actions from either side.
    """
    return _base(KIND_MESH_REQUEST, source)


def export_request(source, preset_name, resolution_log2=None, texture_sets=None):
    """Blender -> Painter: render the channels out into the arena now."""
    record = _base(KIND_EXPORT_REQUEST, source)
    record.update({
        "preset_name": preset_name,
        "resolution_log2": resolution_log2,
        "texture_sets": texture_sets,
    })
    return record


def textures(source, project_path, mesh_path, texture_sets):
    """Painter -> Blender: what was rendered, where, and how to read it.

    Each map carries its own colour space because that is a fact about the
    texture, decided by the side that knows the channel's format. A consumer
    that re-derives it from a file name or a slot will eventually be wrong.
    """
    record = _base(KIND_TEXTURES, source)
    record.update({
        "project_path": project_path,
        "mesh_path": mesh_path,
        "directory": TEXTURE_DIRECTORY_NAME,
        "texture_sets": texture_sets,
    })
    return record


def project_state(source, is_open, project_path, mesh_path, texture_sets):
    """Painter -> Blender: what Painter currently has open."""
    record = _base(KIND_PROJECT_STATE, source)
    record.update({
        "is_open": is_open,
        "project_path": project_path,
        "mesh_path": mesh_path,
        "texture_sets": texture_sets,
    })
    return record


def shader_values(source, values_by_texture_set, shader_url_by_texture_set=None):
    """Either way: the current value of every watched uniform, and nothing else.

    This is the record that rides in the control block rather than in a
    generation, because it is state and not an event -- a value that has already
    been replaced has nothing to say, so the latest one overwriting the previous
    one in place is exactly right, and it costs no filesystem at all.

    An offer is deliberately unfiltered. The sender does not know which uniforms
    the other side's shader exposes, and a table of names kept here would be a
    second truth source for something the shader can be asked about directly, so
    the intersection is computed by the receiver and reported back.

    Which shader each Texture Set runs travels in the same record because it is
    state too: assigning the shader an instance already runs is nothing.
    """
    record = _base(KIND_SHADER_VALUES, source)
    record.update({
        "by_texture_set": values_by_texture_set,
        "shader_url_by_texture_set": shader_url_by_texture_set or {},
    })
    return record


def shader_state(source, instances, parameters, assignment):
    """Painter -> Blender: which shaders run where, and what they expose."""
    record = _base(KIND_SHADER_STATE, source)
    record.update({
        "instances": instances,
        "parameters": parameters,
        "assignment": assignment,
    })
    return record


def color_space_for(channel_format_name):
    """The one place a Painter channel format becomes a colour space name.

    The deciding fact is the format's storage, which Painter documents per
    ChannelFormat member: sRGB8 is the only member stored sRGB-encoded, and every
    other member is stored linear. So exactly one answer is an encoding, and the
    rest are values to be taken as they are.

    ``Channel.is_color`` is deliberately not consulted. It means RGB rather than
    grayscale, not perceptual colour -- a normal map is RGB16F and answers True --
    so letting it choose between linear colour and data marks every normal map in
    every default Painter project as colour. Under Blender's default scene-linear
    space that costs nothing, but under an ACES config it would run normals
    through a primaries conversion.
    """
    if channel_format_name == CHANNEL_FORMAT_SRGB8:
        return COLOR_SPACE_SRGB
    return COLOR_SPACE_DATA
