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

Binding is by construction rather than by table. Painter names a Texture Set
after the glTF material it came from, and what this bridge puts there is the
material's *identity*, not its name -- so the way home survives anyone renaming
anything on either side. Inside the material, a channel lands in the Image
Texture node labelled after it, or in one created for it.

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

from ...Kernel.log import logger

LOG = logger("blender.textures")

BRIDGE_KEY_PROPERTY = "ruri_bridge_key"
IDENTITY_PROPERTY = "ruri_bridge_identity"
CHANNEL_MAP_PROPERTY = "ruri_bridge_channels"
UDIM_TOKEN = "<UDIM>"
_TILE_PATTERN = re.compile(r"^(?P<stem>.*?)(?P<tile>1[0-9]{3})(?P<suffix>\.[^.]+)$")


def _image_key(identity, channel_name):
    """Images are keyed by identity, so a rename never orphans one."""
    return "{0}/{1}".format(identity, channel_name)


def resolve_material(identity, name):
    """The material this Texture Set belongs to, by identity before name.

    Identity wins because it is the thing that does not move. The name is
    consulted only for a material that has never crossed the bridge -- the first
    contact -- and that material is stamped on the way through, so the question
    is never asked of it twice.
    """
    if identity:
        for material in bpy.data.materials:
            if material.get(IDENTITY_PROPERTY) == identity:
                return material
    material = bpy.data.materials.get(name)
    if material is not None and identity and not material.get(IDENTITY_PROPERTY):
        material[IDENTITY_PROPERTY] = identity
        LOG.info("adopted %r as the material for identity %s", name, identity[:8])
    return material


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


def ingest_map(identity, display_name, entry, directory):
    """Bring one channel of one texture set in, reusing its datablock."""
    channel_name = entry["channel"]
    key = _image_key(identity, channel_name)
    readable = "{0}/{1}".format(display_name, channel_name)
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
                    channel_name, display_name, len(paths)))
    else:
        filepath, tiles = paths[0], []

    image = _find_image(key)
    if image is None:
        image = bpy.data.images.load(filepath, check_existing=False)
        image[BRIDGE_KEY_PROPERTY] = key
        image.use_fake_user = True
    else:
        _drop_stale_pack(image)
        image.filepath = filepath
    if image.name != readable:
        image.name = readable

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


def _is_packed(image):
    return image.packed_file is not None or len(image.packed_files) > 0


def _drop_stale_pack(image):
    """Forget data packed by an earlier save, so the new pages are what is read.

    Saving takes bridge textures into the .blend, because the arena keeps only
    its newest generations and a file saved on top of an older one would open
    with nothing behind its images. Packed data then wins over the file path, so
    an image repointed at a newer generation would go on showing the old paint
    until that pack is dropped.
    """
    if _is_packed(image):
        image.unpack(method="REMOVE")


def keep_textures_in_file():
    """Take every bridge texture into the .blend. Returns how many were taken.

    Nothing is copied while both applications are live: an ingested image reads
    the very pages Painter exported into. Those pages belong to an arena
    generation though, and the arena keeps only the newest, so persistence has to
    be paid for at the moment it is asked for -- which is the moment somebody
    saves. Packing is Blender's own answer to a source file that will not be
    there later, and it puts the data in the one file that needs it.
    """
    taken = 0
    for image in bpy.data.images:
        if image.get(BRIDGE_KEY_PROPERTY) is None or _is_packed(image):
            continue
        try:
            image.pack()
        except RuntimeError as error:
            LOG.warning("could not take %s into the file: %s", image.name, error)
            continue
        taken += 1
    if taken:
        LOG.info("took %d bridge texture(s) into the file", taken)
    return taken


def _comparable(name):
    """Names are compared without case or separators, on both sides.

    Painter names a map from its export preset, so the channel arrives spelled
    the preset's way (``Base_color``); a node label is typed by a person. One
    normalisation applied to both is the whole rule -- not a second attempt after
    an exact match fails.
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

    The detour is chosen by the socket's type rather than by its name: a vector
    input fed by a tangent-space map needs the conversion, and a colour or value
    input does not. That is a fact about the socket, so it needs no table of
    channel names to stay right when the shader on the other end is not
    Principled at all.
    """
    if socket.type == "VECTOR":
        converter = tree.nodes.new("ShaderNodeNormalMap")
        converter.location = (node.location.x + 260, node.location.y)
        tree.links.new(node.outputs["Color"], converter.inputs["Color"])
        tree.links.new(converter.outputs["Normal"], socket)
        return
    tree.links.new(node.outputs["Color"], socket)


def channel_map_of(material):
    """The material's own say in where a channel belongs, if it has one.

    A material built by a shader generator labels its texture nodes in the
    generator's vocabulary -- Unity property names, for instance -- which no
    amount of name matching will ever turn into "basecolor". That mapping is the
    generator's to state, not this bridge's to guess, so it is read as data off
    the material: ``ruri_bridge_channels = {"Base_color": "_BaseMap"}``.
    """
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

    Three ways a channel finds its place, in order of how much the material has
    already said: the mapping it declares, then a texture node already labelled
    with the channel's name, then a node created for it and wired to the shader
    input of that name. A channel that matches none of them still arrives as a
    labelled node -- delivered and waiting, rather than silently absent.

    Returns how many landed in an existing home, how many nodes were created, and
    how many reached a shader input, because those are three different answers
    and reporting one number for them is how "nothing happened" hides.
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
    bound = 0
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
        bound += 1

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
    if created:
        LOG.info("%r: %d channel(s) landed in existing nodes, %d node(s) added, "
                 "%d wired to a shader input", material.name, bound, created, connected)
    return {"landed": bound, "created": created, "connected": connected}


def ingest(generation, bind=True):
    """Consume one textures generation. Returns a per-texture-set report."""
    payload = generation.record
    directory = str(generation.path(payload["directory"]))
    report = []
    for texture_set in payload["texture_sets"]:
        name = texture_set["name"]
        identity = texture_set.get("identity") or ""
        material = resolve_material(identity, name)
        display = material.name if material is not None else name
        images_by_channel = {}
        for entry in texture_set["maps"]:
            images_by_channel[entry["channel"]] = ingest_map(
                identity or name, display, entry, directory)
        placement = {"landed": 0, "created": 0, "connected": 0}
        homeless = False
        if bind and material is not None:
            placement = bind_into_material(material, images_by_channel)
        elif bind:
            homeless = True
            LOG.warning(
                "Texture Set %r has no Blender material of that name, so its %d channel(s) "
                "arrived but nothing displays them; send the mesh again to give it one",
                name, len(images_by_channel))
        report.append({
            "texture_set": name,
            "channels": {channel: image.colorspace_settings.name
                         for channel, image in sorted(images_by_channel.items())},
            "bound_nodes": placement["landed"] + placement["created"],
            "connected_nodes": placement["connected"],
            "homeless": homeless,
        })
    return report
