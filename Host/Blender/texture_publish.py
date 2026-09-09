# -*- coding: utf-8 -*-
"""The textures this document already has, sent with the model.

A texturing tool that receives a model and nothing else starts from grey. What
this document has -- a base colour, a normal map, whatever was already authored
or imported -- is the ground the painting goes on top of, and it is sitting right
there in the material graph.

**What each image IS comes from the socket it feeds**, not from its file name. A
graph says "this image is the base colour" by being wired to the base-colour
input; a file called ``red.png`` says nothing at all, and a scene assembled from
somebody else's assets is full of names that lie. Only images that reach the
surface shader are sent, for the same reason: an image nobody is using is not a
texture, it is a file that happens to be open.

**What colour space an image is in comes from the image**, which is the only
thing that knows. Deriving it from the channel it feeds, or from its name, is
measured elsewhere in these tools as a 68x brightness error.

The files themselves are written into the session and marked resident, so the
other side maps the same pages rather than reading them back off storage.
"""

from __future__ import annotations

import os

import bpy

from ...Kernel import arena as arena_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

LOG = logger("blender.textures")

#: Which surface input an image has to reach to be worth sending, and what the
#: other side should call the channel it lands in. The names on the right are the
#: neutral ones the record carries; the receiving side maps them into its own
#: vocabulary, which is its business and not this side's.
SURFACE_INPUTS = (
    ("Base Color", "basecolor"),
    ("Metallic", "metallic"),
    ("Roughness", "roughness"),
    ("Normal", "normal"),
    ("Emission Color", "emissive"),
    ("Emission", "emissive"),
    ("Alpha", "opacity"),
    ("Specular IOR Level", "specular"),
)

#: How far to walk back from a surface input before giving up. A base colour
#: behind a mix, a normal behind a normal-map node and a colour ramp: real graphs
#: put a few nodes in the way, and none of them change what the image IS.
MAXIMUM_DEPTH = 6

TEXTURE_DIRECTORY_NAME = "from_blender"


def _surface_node(tree):
    """The node actually being rendered, found through the output rather than by
    type: a graph can hold several shaders and only one of them is wired up."""
    for node in tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            link = node.inputs["Surface"].links
            if link:
                return link[0].from_node
    for node in tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _image_behind(socket, depth=0):
    """The image feeding this input, through whatever is in the way."""
    if depth > MAXIMUM_DEPTH or not socket.links:
        return None
    node = socket.links[0].from_node
    if node.type == "TEX_IMAGE":
        return node.image
    for candidate in node.inputs:
        found = _image_behind(candidate, depth + 1)
        if found is not None:
            return found
    return None


def images_of(material):
    """Every image this material actually renders with, by neutral channel name."""
    if not material.use_nodes or material.node_tree is None:
        return {}
    surface = _surface_node(material.node_tree)
    if surface is None:
        return {}
    found = {}
    for input_name, channel in SURFACE_INPUTS:
        if channel in found:
            continue
        socket = surface.inputs.get(input_name)
        if socket is None:
            continue
        image = _image_behind(socket)
        if image is not None:
            found[channel] = image
    return found


#: What Blender calls a format, and what the file is called.
_EXTENSIONS = {"OPEN_EXR": ".exr", "PNG": ".png", "JPEG": ".jpg",
               "TARGA": ".tga", "TIFF": ".tif", "BMP": ".bmp"}


def _extension_of(image):
    """The suffix the bytes already have, or the one the format implies."""
    for candidate in (image.filepath_raw, image.filepath):
        suffix = os.path.splitext(bpy.path.abspath(candidate or ""))[1]
        if suffix:
            return suffix
    return _EXTENSIONS.get(image.file_format, ".png")


def _source_bytes(image):
    """The encoded file this image already is, or None if it is only pixels.

    A packed image carries the exact bytes Blender read; an unpacked one with a
    path is the same bytes one level out. Either way this is a copy, and copying
    is what "the file already exists" should cost.
    """
    packed = image.packed_file
    if packed is not None and packed.data:
        return bytes(packed.data)
    resolved = bpy.path.abspath(image.filepath_raw or image.filepath or "")
    if resolved and os.path.isfile(resolved):
        with open(resolved, "rb") as handle:
            return handle.read()
    return None


def _write_image(image, directory, stem):
    """Put one image in the session, without making a second copy of it.

    Encoding is the fallback and not the road: it is for an image that exists
    nowhere but in memory. Everything else is bytes that are already correct.
    """
    path = os.path.join(directory, stem + _extension_of(image))
    raw = _source_bytes(image)
    if raw is not None:
        with open(path, "wb") as handle:
            handle.write(raw)
    else:
        previous = image.filepath_raw, image.file_format
        try:
            image.filepath_raw = path
            image.save()
        finally:
            image.filepath_raw, image.file_format = previous
    arena_module.keep_in_memory(path)
    return os.path.basename(path), os.path.getsize(path)


def _safe(name):
    return "".join(character if character.isalnum() or character in "-_." else "_"
                   for character in name)


def publish_into(staging, materials):
    """Write every material's textures into a staging generation.

    Returns the record section: material identity -> channel -> file and colour
    space. Empty when nothing in scope renders with an image, which is a real
    answer and not a failure.
    """
    directory = os.path.join(str(staging.directory), TEXTURE_DIRECTORY_NAME)
    written = {}
    seen = {}
    count = 0
    total = 0
    for material in materials:
        found = images_of(material)
        if not found:
            continue
        identity = material.get("ruri_bridge_identity") or material.name
        channels = {}
        for channel, image in sorted(found.items()):
            key = image.name
            if key not in seen:
                if not os.path.isdir(directory):
                    os.makedirs(directory, exist_ok=True)
                try:
                    seen[key] = _write_image(
                        image, directory, "{0}_{1}".format(_safe(identity), channel))
                except Exception as error:
                    LOG.warning("could not send %s for %s: %s", image.name,
                                material.name, error)
                    continue
                count += 1
                total += seen[key][1]
            file_name, _size = seen[key]
            channels[channel] = {
                "file": file_name,
                # The image's own answer. Nothing here derives it from the channel
                # or the file name; both are measured to be wrong.
                "color_space": image.colorspace_settings.name,
                "image": image.name,
            }
        if channels:
            written[identity] = channels
    if count:
        LOG.info("sent %d texture(s) for %d material(s), %.1f MB",
                 count, len(written), total / 1e6)
    return {"directory": TEXTURE_DIRECTORY_NAME, "by_material": written} if written else {}
