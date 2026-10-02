# -*- coding: utf-8 -*-
"""Taking Painter's rendered channels into Blender's materials.

The textures are files in the document's own ``textures`` folder, written there
by Painter's export. They ARE the textures -- nothing is copied out of a
transport afterwards -- so an image here points at the file with a path relative
to the document, and saving, moving the project folder or reopening tomorrow all
find it where it is.

The colour space is never guessed. It arrives in the record, decided by the side
that knows the channel's format, and is applied verbatim; deriving it from a file
name or from which socket the image lands in is how textures end up wrong by a
gamma curve.

Which materials a Texture Set belongs to is the material's own statement (see
``mesh_publish.texture_set_of``): every material that paints into it receives
its channels, which is what one material split in two across one UV layout
needs. Inside a material, a channel lands in the Image Texture node labelled
after it, or in one created for it and wired to the shader input of that name.
"""

from __future__ import annotations

import os
import re

import bpy

from ...Kernel.log import logger
from . import mesh_publish

LOG = logger("blender.textures")

CHANNEL_MAP_PROPERTY = "ruri_bridge_channels"
UDIM_TOKEN = "<UDIM>"
_TILE_PATTERN = re.compile(r"^(?P<stem>.*?)(?P<tile>1[0-9]{3})(?P<suffix>\.[^.]+)$")


def materials_painting(texture_set):
    """Every material that paints into this Texture Set."""
    return [material for material in bpy.data.materials
            if material.library is None
            and mesh_publish.texture_set_of(material) == texture_set]


def _relative(path):
    """The path as the document will keep it: relative when it can be."""
    if not bpy.data.filepath:
        return path
    try:
        return bpy.path.relpath(path)
    except ValueError:
        return path


def _same_file(image, path):
    if not image.filepath:
        return False
    return os.path.normcase(os.path.abspath(bpy.path.abspath(image.filepath))) == \
        os.path.normcase(os.path.abspath(path))


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


def ingest_map(entry, directory):
    """One rendered channel as an image datablock on its own file."""
    paths = [os.path.join(directory, name) for name in entry["files"]]
    missing = [path for path in paths if not os.path.exists(path)]
    if missing:
        raise RuntimeError("Painter reported {0} but it is not there".format(missing[0]))
    if len(paths) > 1:
        filepath, tiles = _tiled_filepath(paths)
        if filepath is None:
            raise RuntimeError(
                "channel {0} exported {1} files that are not a UDIM set".format(
                    entry["channel"], len(paths)))
    else:
        filepath, tiles = paths[0], []

    image = next((one for one in bpy.data.images if _same_file(one, filepath)), None)
    if image is None:
        image = bpy.data.images.load(filepath, check_existing=False)
        image.filepath = _relative(filepath)
    if tiles:
        image.source = "TILED"
        image.tiles[0].number = tiles[0]
        existing = {tile.number for tile in image.tiles}
        for number in tiles[1:]:
            if number not in existing:
                image.tiles.new(tile_number=number)
    image.colorspace_settings.name = entry["color_space"]
    image.reload()
    LOG.info("took %s (%s)", os.path.basename(filepath), entry["color_space"])
    return image


def _comparable(name):
    """Names are compared without case or separators, on both sides.

    Painter names a map from its export preset (``Base_color``); a node label is
    typed by a person. One normalisation applied to both is the whole rule.
    """
    return "".join(character for character in name.lower() if character.isalnum())


def _surface_node(tree):
    """Whatever actually feeds the material output, be it Principled or a group."""
    for node in tree.nodes:
        if node.type != "OUTPUT_MATERIAL":
            continue
        for link in node.inputs["Surface"].links:
            return link.from_node
    return None


def _connect(tree, node, socket):
    """Wire an image into a shader input, through a Normal Map where one is due.

    Chosen by the socket's type rather than its name: a vector input fed by a
    tangent-space map needs the conversion, and a colour or value input does not.
    """
    if socket.type == "VECTOR":
        converter = tree.nodes.new("ShaderNodeNormalMap")
        converter.location = (node.location.x + 260, node.location.y)
        tree.links.new(node.outputs["Color"], converter.inputs["Color"])
        tree.links.new(converter.outputs["Normal"], socket)
        return
    tree.links.new(node.outputs["Color"], socket)


def channel_map_of(material):
    """The material's own say in where a channel belongs, if it has one."""
    declared = material.get(CHANNEL_MAP_PROPERTY)
    if not declared:
        return {}
    try:
        return {_comparable(key): str(value) for key, value in dict(declared).items()}
    except (TypeError, ValueError):
        LOG.warning("%r carries a %s that is not a mapping; ignoring it",
                    material.name, CHANNEL_MAP_PROPERTY)
        return {}


def bind_into_material(material, images_by_channel):
    """Give every received channel a home in this material, creating what is missing.

    In order of how much the material has already said: the mapping it declares,
    then a texture node already labelled with the channel's name, then a node
    created for it and wired to the shader input of that name. A channel that
    matches none of them still arrives as a labelled node -- delivered and
    waiting, rather than silently absent.
    """
    if not material.use_nodes or material.node_tree is None:
        material.use_nodes = True
    tree = material.node_tree
    by_comparable = {}
    for channel, image in images_by_channel.items():
        key = _comparable(channel)
        if key in by_comparable:
            raise RuntimeError(
                "channels {0!r} and {1!r} are indistinguishable once compared; a node "
                "label cannot name one of them".format(by_comparable[key][0], channel))
        by_comparable[key] = (channel, image)

    declared = channel_map_of(material)
    landed = 0
    placed = set()
    for node in tree.nodes:
        if node.type != "TEX_IMAGE":
            continue
        wanted = _comparable(node.label)
        entry = by_comparable.get(wanted)
        if entry is None:
            for channel_key, target in declared.items():
                if _comparable(target) == wanted and channel_key in by_comparable:
                    entry = by_comparable[channel_key]
                    break
        if entry is None:
            continue
        node.image = entry[1]
        placed.add(entry[0])
        landed += 1

    surface = _surface_node(tree)
    inputs_by_comparable = ({_comparable(socket.name): socket for socket in surface.inputs}
                            if surface is not None else {})
    created = 0
    connected = 0
    offset = 0
    for key, (channel, image) in sorted(by_comparable.items()):
        if channel in placed:
            continue
        if key in declared:
            socket_name = _comparable(declared[key])
            if socket_name in inputs_by_comparable:
                key = socket_name
        node = tree.nodes.new("ShaderNodeTexImage")
        node.label = channel
        node.name = channel
        node.image = image
        anchor = surface.location if surface is not None else (0.0, 0.0)
        node.location = (anchor[0] - 900, anchor[1] + 400 - offset * 320)
        offset += 1
        created += 1
        socket = inputs_by_comparable.get(key)
        if socket is not None and not socket.links:
            _connect(tree, node, socket)
            connected += 1
    return {"landed": landed, "created": created, "connected": connected}


def ingest(generation):
    """Take one textures record in. Returns a report per Texture Set."""
    payload = generation.record
    directory = payload["directory"]
    report = []
    for texture_set in payload["texture_sets"]:
        name = texture_set["name"]
        images = {entry["channel"]: ingest_map(entry, directory)
                  for entry in texture_set["maps"]}
        materials = materials_painting(name)
        placed = {"landed": 0, "created": 0, "connected": 0}
        waiting = []
        for material in materials:
            if material.get(mesh_publish.SHADING_DECLARATION) is not None:
                # A generated material reads its images through its own record and
                # its shader's packing, not through loose nodes; binding them here
                # would put nodes in a tree its generator rebuilds.
                waiting.append(material.name)
                continue
            for key, value in bind_into_material(material, images).items():
                placed[key] += value
        if not materials:
            LOG.warning("Texture Set %r has no material here that paints into it; its %d "
                        "channel(s) are in the textures folder and nothing shows them",
                        name, len(images))
        report.append({
            "texture_set": name,
            "materials": [material.name for material in materials],
            "channels": sorted(images),
            "placed": placed,
            "generated": waiting,
        })
    return report
