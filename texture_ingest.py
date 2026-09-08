# -*- coding: utf-8 -*-
"""Taking Painter's rendered channels into Blender's image datablocks.

Blender's image API has no borrowed-pointer ingress: ``Image.pixels`` is a float
RGBA array Blender owns, so feeding it raw bytes would both copy and inflate an
eight-bit map to four times its size. Loading the file lets Blender's own threaded
decoders fill an ImBuf of the right depth instead, which is the floor this leg can
reach -- unlike the mesh leg, no arrangement here avoids a copy, because the
receiving buffer belongs to Blender.

The colour space is never guessed. It arrives in the record, decided by the side
that knows the channel's format, and is applied verbatim; deriving it from a file
name or from which socket the image lands in is how textures end up wrong by a
gamma curve.

Binding is by construction rather than by table: Painter names a Texture Set
after the glTF material it came from, which is the Blender material name this
bridge sent, so the target material is unambiguous. Inside it, an Image Texture
node whose label is the channel name receives that channel.

Every ingested image is given a fake user. A channel with no node to land in yet
has no user at all, and Blender does not write user-less datablocks when the file
is saved -- so without this, pulling textures, saving and reopening would silently
lose exactly the maps that were not wired up yet.

Images point straight into the arena, and nothing is copied out of it. That is
safe because retirement keeps two generations alive -- the newest and the one
this side last acknowledged -- so the payload an image was loaded from survives
until a newer payload replaces it and the image is repointed. Copying every map
into a private store would be a whole extra pass over every texture, every pull,
to buy durability the arena already provides.

The arena therefore lives somewhere durable rather than in the temporary folder;
see ``arena.default_root``.
"""

from __future__ import annotations

import os
import re

import bpy

from ruri_bridge.log import logger

LOG = logger("blender.textures")

BRIDGE_KEY_PROPERTY = "ruri_bridge_key"
UDIM_TOKEN = "<UDIM>"
_TILE_PATTERN = re.compile(r"^(?P<stem>.*?)(?P<tile>1[0-9]{3})(?P<suffix>\.[^.]+)$")


def _image_key(texture_set_name, channel_name):
    return "{0}/{1}".format(texture_set_name, channel_name)


def _find_image(key):
    for image in bpy.data.images:
        if image.get(BRIDGE_KEY_PROPERTY) == key:
            return image
    return None


def _tiled_filepath(paths):
    """Fold ``name_1001.png``-style siblings into Blender's <UDIM> form."""
    tiles = []
    template = None
    for path in paths:
        match = _TILE_PATTERN.match(os.path.basename(path))
        if match is None:
            return None, []
        tiles.append(int(match.group("tile")))
        candidate = os.path.join(
            os.path.dirname(path),
            match.group("stem") + UDIM_TOKEN + match.group("suffix"))
        if template is None:
            template = candidate
        elif template != candidate:
            return None, []
    return template, sorted(tiles)


def ingest_map(texture_set_name, entry, directory):
    """Bring one channel of one texture set in, reusing its datablock."""
    channel_name = entry["channel"]
    key = _image_key(texture_set_name, channel_name)
    paths = [os.path.join(directory, name) for name in entry["files"]]
    missing = [path for path in paths if not os.path.exists(path)]
    if missing:
        raise RuntimeError("Painter reported {0} but it is not in the arena".format(missing[0]))

    if len(paths) > 1:
        filepath, tiles = _tiled_filepath(paths)
        if filepath is None:
            raise RuntimeError(
                "channel {0} of {1} exported {2} files that are not a UDIM set; the "
                "bridge has no naming rule for them".format(
                    channel_name, texture_set_name, len(paths)))
    else:
        filepath, tiles = paths[0], []

    image = _find_image(key)
    if image is None:
        image = bpy.data.images.load(filepath, check_existing=False)
        image.name = key
        image[BRIDGE_KEY_PROPERTY] = key
        image.use_fake_user = True
    else:
        image.filepath = filepath

    if tiles:
        image.source = "TILED"
        image.tiles[0].number = tiles[0]
        existing = {tile.number for tile in image.tiles}
        for number in tiles[1:]:
            if number not in existing:
                image.tiles.new(tile_number=number)
    image.colorspace_settings.name = entry["color_space"]
    image.reload()
    LOG.info("ingested %s (%s, %s)", key, entry["color_space"], os.path.basename(filepath))
    return image


def _comparable(name):
    """Names are compared without case or separators, on both sides.

    Painter names a map from its export preset, so the channel arrives spelled
    the preset's way (``Base_color``); a node label is typed by a person. One
    normalisation applied to both is the whole rule -- not a second attempt after
    an exact match fails.
    """
    return "".join(character for character in name.lower() if character.isalnum())


def bind_into_material(material, images_by_channel):
    """Fill Image Texture nodes whose label names a channel we just received."""
    if not material.use_nodes or material.node_tree is None:
        return 0
    by_comparable = {}
    for channel, image in images_by_channel.items():
        key = _comparable(channel)
        if key in by_comparable:
            raise RuntimeError(
                "channels {0!r} and {1!r} are indistinguishable once compared; a node "
                "label cannot name one of them".format(by_comparable[key][0], channel))
        by_comparable[key] = (channel, image)
    bound = 0
    for node in material.node_tree.nodes:
        if node.type != "TEX_IMAGE":
            continue
        entry = by_comparable.get(_comparable(node.label))
        if entry is None:
            continue
        node.image = entry[1]
        bound += 1
    return bound


def ingest(generation, bind=True):
    """Consume one textures generation. Returns a per-texture-set report."""
    payload = generation.record
    directory = str(generation.path(payload["directory"]))
    report = []
    for texture_set in payload["texture_sets"]:
        name = texture_set["name"]
        images_by_channel = {}
        for entry in texture_set["maps"]:
            images_by_channel[entry["channel"]] = ingest_map(name, entry, directory)
        bound = 0
        material = bpy.data.materials.get(name)
        if bind and material is not None:
            bound = bind_into_material(material, images_by_channel)
        elif bind:
            LOG.warning("no Blender material named %r to bind %d channels into",
                        name, len(images_by_channel))
        report.append({
            "texture_set": name,
            "channels": {channel: image.colorspace_settings.name
                         for channel, image in sorted(images_by_channel.items())},
            "bound_nodes": bound,
        })
    return report
