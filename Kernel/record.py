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

FORMAT_VERSION = 10

#: What a request is asking for. The only "kind" left, because it is the only
#: one that distinguishes something WITHIN a topic -- every other distinction is
#: the channel a record arrived on.
ASK_FOR_MESH = "mesh"
ASK_FOR_TEXTURES = "textures"
ASK_FOR_ANIMATION = "anim"
#: "This Texture Set is painted by that material": answered by the application
#: that owns the materials, because it is the one that names what crosses.
ASK_TO_BIND = "bind"
#: "Call this Texture Set by that name": answered by the application that owns
#: the Texture Sets. A rename there is the only edit that keeps every layer.
ASK_TO_RENAME = "rename"
#: "State your shading for these Texture Sets": answered by whichever side holds
#: shading parameters and was not the one asking, on the shading topic -- the same
#: errand pulls a shader either way.
ASK_FOR_SHADING = "shading"
#: "Cut these lanes out of your materials' textures": answered by the application
#: whose materials hold the textures, on the inputs topic.
ASK_FOR_INPUTS = "inputs"
#: "Stand these Texture Sets up from my materials' textures": answered by the
#: application with Texture Sets, which knows what its shader reads and asks for
#: exactly that.
ASK_TO_TAKE_TEXTURES = "take_textures"
#: "These Texture Sets are about to change layout: say which UV sets their content
#: reads and hand over their mesh maps": answered by the application with Texture
#: Sets, on the textures topic, so the side that owns the coordinates can lay the
#: maps out again in the same step that moves the coordinates.
ASK_FOR_LAYOUT = "layout"
#: The shape of that answer.
LAYOUT_ANSWER = "layout"

RECORD_FILE_NAME = "record.json"
#: A rig and its performance, as the animation tools on either side read it.
SCENE_FILE_NAME = "scene.glb"
#: The surface somebody paints on, as the texturing tool reads it.
SURFACE_FILE_NAME = "surface.fbx"
TEXTURE_DIRECTORY_NAME = "maps"

#: The texturing tool's own length unit, per metre. A project the bridge starts
#: measures in it, so a brush or a projection sized in centimetres means what it
#: says.
CENTIMETRES_PER_METRE = 100.0

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


def checked(record, where):
    """The record, if it is in the shape this build speaks; refused by name otherwise.

    Two applications on one session run whatever copy of the bridge each loaded,
    and a record from another copy is missing what this one reads or means
    something else by it -- read anyway, it fails three calls later as a missing
    key nobody can trace back.
    """
    version = record.get("format_version")
    if version != FORMAT_VERSION:
        raise RecordError("{0} is record format {1}, this build speaks {2}: restart the "
                          "application that wrote it".format(where, version, FORMAT_VERSION))
    return record


def read(generation_directory):
    directory = Path(generation_directory)
    path = directory / RECORD_FILE_NAME
    if not path.exists():
        raise RecordError("generation {0} has no {1}".format(directory, RECORD_FILE_NAME))
    with open(path, "r", encoding="utf-8") as handle:
        return checked(json.load(handle), "generation {0}".format(directory))


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


def frame(scale, offset=(0.0, 0.0, 0.0)):
    """Where a modelling world lands inside a texturing project.

    A project keeps the frame its surface first arrived in, for life: a texturing
    tool places its 3D projections and re-projects its strokes relative to that
    frame, so a surface that arrives in any other one is a different surface to
    it. Positions in the file are ``scale`` times the world turned Y-up, plus
    ``offset``.
    """
    return {"scale": float(scale), "offset": [float(value) for value in offset]}


def same_frame(first, second, tolerance=1e-9):
    if not first or not second:
        return False
    if abs(float(first["scale"]) - float(second["scale"])) > tolerance * max(
            1.0, abs(float(first["scale"]))):
        return False
    return all(abs(float(a) - float(b)) <= tolerance * max(1.0, abs(float(a)))
               for a, b in zip(first["offset"], second["offset"]))


def mesh(source, scene_file, scene, materials, frame_of_project, layouts, uv_sets,
         fingerprints, relaid=None):
    """The surface somebody paints on, in the frame of the project it is for.

    ``scene`` describes what went into the file (object names, how many faces
    paint into which Texture Set, the bounds) so the consumer can report and gate
    without parsing it. ``materials`` are the producing side's material rows,
    carried verbatim; each names the Texture Set it paints into, which is also the
    material name the file carries, because a texturing tool matches its Texture
    Sets by that name.

    ``layouts`` is every Texture Set's chart table (see ``layout``), the
    coordinates its ``uv_sets`` UV sets hold; the texturing side keeps everything
    read through a chart reading the same coordinates. ``relaid`` carries, for a
    Texture Set whose layout is a chart the texturing side has no mesh maps for
    yet, those maps laid out in it: ``{Texture Set: {"chart": chart, "mesh_maps":
    {usage: {"file": name, "hash": sha1}}, "fills": {uid: {"path": path}}}}``, the
    mesh maps beside the record; ``fills`` are pictures of tangent normals carried into
    the new layout's frames in place, for the fills reading them, on disk where they
    stay.

    ``fingerprints`` is, per Texture Set, a digest of its polygons and their layout
    coordinates as they cross: what its mesh maps are laid out on. The texturing side
    keeps the one it applied and hands it back with a layout answer, so the side that
    lays the maps out again knows they were laid out on the coordinates it holds.
    """
    record = _base("mesh", source)
    record.update({
        "scene_file": scene_file,
        "scene": scene,
        "materials": materials,
        "frame": frame_of_project,
        "layouts": layouts,
        "uv_sets": int(uv_sets),
        "fingerprints": dict(fingerprints),
        "relaid": relaid or {},
    })
    return record


def performance(source, scene_file, scene_name, **timing):
    """A rig and what it does: the GLB beside the record, and when it plays."""
    record = _base("anim", source)
    record.update({
        "scene_file": scene_file,
        "scene": {"name": scene_name},
    })
    record.update(timing)
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


def textures(source, document, directory, texture_sets):
    """Rendered channels: which files, in which folder, and how to read them.

    ``directory`` is a real folder beside the document the textures belong to,
    not a transport generation: they are the textures, and a file that has to be
    found again tomorrow cannot live somewhere that is recycled. Each map carries
    its own colour space because that is a fact about the texture, decided by the
    side that knows the channel's format.

    A Texture Set whose materials run a generated shader the texturing side holds
    the same generation of also carries ``slots``: for each texture that shader
    samples, lane by lane, which exported map and component stands it up again and
    through which operation -- or ``slots_refused``, saying why not.
    """
    record = _base("tex", source)
    record.update({
        "document": document,
        "directory": str(directory),
        "texture_sets": texture_sets,
    })
    return record


def layout_answer(source, texture_set, request, fingerprint, readers, tables, kept,
                  mesh_maps, fills, pictures, convention, refused):
    """A Texture Set about to change layout, as the texturing side holds it.

    ``request`` is the generation of the ask it answers. ``fingerprint`` is the
    Texture Set's from the surface the project holds (see ``mesh``), empty when the
    surface came without one. ``readers`` are the chart-addressed fills showing the
    Texture Set, ``{"uid", "index", "members", "following"}`` -- the UV set each reads,
    every Texture Set showing it and those whose own mesh map it reads -- and ``tables``
    the tables the project applies (charts only), by Texture Set. ``kept`` are the charts
    the project holds the Texture Set's mesh maps for, from before: moving to one of them
    takes those, so nothing is laid out again for it. ``mesh_maps`` is ``{usage: {"file": name, "kind": kind}}``
    beside the record, each laid out in the Texture Set's current layout; ``kind``
    says how its values follow a layout change (``tangent``, ``label`` or ``value``).
    ``fills`` are the content laying tangent normals through a chart: ``{"uid", "name",
    "index", "members", "procedural", "pixels", "file", "render", "pictures"}`` -- the UV set
    it reads, the Texture Sets showing it, whether a substance computes its normals (they are
    only looked at, never laid out) or a picture holds them, whether it lays nothing but
    pictures and uniform colours through set 0, and its normals, laid out in that UV set: the
    picture's own file while it is on disk (with ``reading``, a render of the picture alone),
    else a render of them as the fill lays them, beside the record; for a fill laying nothing
    but pictures whose other pictures are files on disk (``pixels``), ``pictures`` are those,
    ``{"channel", "floating", "file"}`` -- ``floating`` when the channel holds values past
    0..1;
    ``restorable`` is the chart in which the fill's own normals, from before the bridge laid
    them out anew, are right -- moving there gives them back instead -- or None when nothing
    is remembered (the chart a Texture Set began in is the empty name). ``pictures`` are the pictures
    effects read with no projection of their own, laid out in the current layout:
    ``{"key", "name", "label", "members", "file"}``. ``convention`` names the exports
    that tell which way the stored tangent maps point their green, how the normal channel
    combines with the mesh map, and -- with ``fresh`` -- a picture Painter had never seen
    and its render, when there are any. ``refused`` says why the layout cannot change,
    when it cannot.
    """
    record = _base(LAYOUT_ANSWER, source)
    record.update({
        "texture_set": texture_set,
        "request": int(request),
        "fingerprint": str(fingerprint),
        "readers": list(readers),
        "tables": dict(tables),
        "kept": list(kept),
        "mesh_maps": mesh_maps,
        "fills": list(fills),
        "pictures": list(pictures),
        "convention": convention,
        "refused": refused,
    })
    return record


def inputs(source, texture_sets):
    """A material's own textures, cut into the inputs a texturing tool's shader reads.

    Each Texture Set names the material the lanes came from, the shader they were cut
    for, and one file per input beside the record with the hash of its bytes -- so a
    reader that already holds those bytes does not take them in twice -- plus the
    inputs that could not be cut and why.
    """
    record = _base("inputs", source)
    record["texture_sets"] = texture_sets
    return record


def presence(source, document, texture_sets=(), materials=(), textures_directory="",
             frame_of_project=None):
    """What this application has open, stated by the application itself.

    Everybody publishes it and everybody reads everybody else's, which is how a
    side stops guessing whether the other one is there and what it is holding.
    A texturing tool fills ``texture_sets`` -- each with its layer count and the
    maps a whole export of it writes, or why it cannot say -- and the frame its
    project's surface lives in -- None for a project the bridge did not start and
    nobody has measured; a modelling tool fills ``materials`` and says where the
    textures of its document live. A material row names the generated shader the material runs
    and that shader's identity, empty for a material nobody generated.
    ``document`` is empty when nothing is open, which is an answer and not a
    missing one.
    """
    record = _base("here", source)
    record.update({
        "document": document or "",
        "texture_sets": list(texture_sets),
        "materials": list(materials),
        "textures_directory": textures_directory or "",
        "frame": frame_of_project,
    })
    return record


def shading(source, values_by_texture_set, shader_url_by_texture_set=None,
            vocabulary_by_texture_set=None, shader_name_by_texture_set=None,
            lookups_by_texture_set=None, identity_by_texture_set=None):
    """The current value of every shading parameter, per Texture Set synced.

    State and not an event, so it rides in the control block: a value that has
    already been replaced has nothing to say. It names the Texture Sets being synced
    -- every one, or the one selected on whichever side asked -- and nothing else.

    ``identity_by_texture_set`` is the shader's own identity -- the one hash its
    generator stamped into every application's copy of it. Two equal identities
    mean the two sides run the same shader, so every value crosses as it is and the
    material's textures follow; two different ones mean only the names both shaders
    share can be trusted to mean the same thing.
    """
    record = _base("shade", source)
    record.update({
        "by_texture_set": values_by_texture_set,
        "shader_url_by_texture_set": shader_url_by_texture_set or {},
        "vocabulary_by_texture_set": vocabulary_by_texture_set or {},
        "shader_name_by_texture_set": shader_name_by_texture_set or {},
        "lookups_by_texture_set": lookups_by_texture_set or {},
        "identity_by_texture_set": identity_by_texture_set or {},
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
    every default Painter project as colour.
    """
    if channel_format_name == CHANNEL_FORMAT_SRGB8:
        return COLOR_SPACE_SRGB
    return COLOR_SPACE_DATA
