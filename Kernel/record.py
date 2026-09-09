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

#: What a request is asking for. The only "kind" left, because it is the only
#: one that distinguishes something WITHIN a topic -- every other distinction the
#: old kinds carried is now the channel a record arrived on.
ASK_FOR_MESH = "mesh"
ASK_FOR_TEXTURES = "textures"
ASK_FOR_ANIMATION = "anim"

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
    """Every record says who published it and what shape it is.

    ``kind`` is the shape, not the destination: where it goes was decided by the
    channel it was published on, and a record that also named its destination
    would be a second statement of the same thing.
    """
    return {
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "source": source,
        "process_id": os.getpid(),
    }


def binding(scene_identity, scene_identity_is_new, document, project_path,
            vertex_count=0):
    """Which document on the sending side a Painter project belongs to.

    This travels into the Painter project's own metadata, where saving carries
    it along, so a project opened again days later still knows which scene it
    answers to. Names cannot do that job: both hosts let people rename anything,
    and a path moves the moment somebody reorganises a drive.
    """
    return {
        "scene_identity": scene_identity,
        "scene_identity_is_new": scene_identity_is_new,
        "document": document,
        "project_path": project_path,
        "vertex_count": vertex_count,
    }


def mesh(source, intent, scene, materials, unit_scale, up_axis, binding_record=None,
         textures=None):
    """The model: geometry lives in the GLB, everything else here.

    ``scene`` describes what went into the GLB (object names, primitive counts,
    the bounds) so the consumer can report and verify without parsing it.
    ``materials`` are the producing side's material rows, carried verbatim.

    ``textures`` is what the sending side ALREADY has on those materials -- the
    ground a texturing tool paints on top of. Each entry names a file beside the
    GLB, the channel it belongs in, and the colour space the image itself
    declares. Absent when the scene renders with no images, which is a real
    answer rather than a missing one.
    """
    record = _base("mesh", source)
    record.update({
        "intent": intent,
        "scene_file": SCENE_FILE_NAME,
        "scene": scene,
        "materials": materials,
        "unit_scale": unit_scale,
        "up_axis": up_axis,
        "binding": binding_record or {},
        "textures": textures or {},
    })
    return record


def request(source, asked_for, **details):
    """Ask another application to do a thing.

    One builder, not one per errand. An application that cannot read another's
    document has asking as its only move, and what it is asking for is a field
    rather than a channel -- so the third application asks for a model without a
    line of new plumbing anywhere.
    """
    record = _base("ask", source)
    record["for"] = asked_for
    record.update(details)
    return record


def textures(source, project_path, mesh_path, texture_sets):
    """Painter -> Blender: what was rendered, where, and how to read it.

    Each map carries its own colour space because that is a fact about the
    texture, decided by the side that knows the channel's format. A consumer
    that re-derives it from a file name or a slot will eventually be wrong.
    """
    record = _base("tex", source)
    record.update({
        "project_path": project_path,
        "mesh_path": mesh_path,
        "directory": TEXTURE_DIRECTORY_NAME,
        "texture_sets": texture_sets,
    })
    return record


def presence(source, is_open, project_path, mesh_path, texture_sets,
             binding_record=None):
    """What this application currently has open, and what it is bound to.

    Everybody publishes it and everybody reads everybody else's, which is how a
    side stops guessing whether the other one is there and what it is holding.
    """
    record = _base("here", source)
    record.update({
        "is_open": is_open,
        "project_path": project_path,
        "mesh_path": mesh_path,
        "texture_sets": texture_sets,
        "binding": binding_record or {},
    })
    return record


def shading(source, values_by_texture_set, shader_url_by_texture_set=None,
            vocabulary_by_texture_set=None, shader_name_by_texture_set=None):
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

    An offer may also say which shader's vocabulary it is spoken in, when the
    material it came from declared one. That is not a filter either -- the
    intersection is still the receiver's to compute -- but it is the difference
    between "your shader does not expose these hundred and thirty six names" and
    "these values are for a shader nobody here is running", and only one of those
    two sentences tells somebody what to do about it.

    The shader's own declaration -- every parameter's label, widget and help text
    -- is deliberately NOT here. Nothing ever read it, and on a character with a
    generated shader on every Texture Set it is two megabytes against a control
    block that holds half of one: the publish raised, and the state that mattered
    never crossed at all. A name per Texture Set is the whole of the shape a
    receiver needs.
    """
    record = _base("shade", source)
    record.update({
        "by_texture_set": values_by_texture_set,
        "shader_url_by_texture_set": shader_url_by_texture_set or {},
        "vocabulary_by_texture_set": vocabulary_by_texture_set or {},
        "shader_name_by_texture_set": shader_name_by_texture_set or {},
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
