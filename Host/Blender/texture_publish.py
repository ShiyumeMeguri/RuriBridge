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
import shutil

import bpy
import numpy

from ...Kernel import arena as arena_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

LOG = logger("blender.textures")

#: Which surface input an image has to reach to be worth sending, and what the
#: other side should call the channel it lands in. The names on the right are the
#: neutral ones the record carries; the receiving side maps them into its own
#: vocabulary, which is its business and not this side's.
#:
#: The neutral names are the ones a generated material declares for its own
#: images, so a document with both kinds of material in it speaks one vocabulary
#: rather than two that have to be reconciled somewhere further along.
SURFACE_INPUTS = (
    ("Base Color", "BaseColor"),
    ("Metallic", "Metallic"),
    ("Roughness", "Roughness"),
    ("Normal", "TangentNormal"),
    ("Emission Color", "Emission"),
    ("Emission", "Emission"),
    ("Alpha", "Opacity"),
    ("Specular IOR Level", "SpecularLevel"),
)

#: The custom property a material uses to declare what it is made of. Written by
#: whatever generated the material; read here and nowhere written.
SHADING_DECLARATION = "ruri_shading"

#: A lane that is the whole image. Anything narrower is a packed map -- roughness
#: in red, metal in green -- and one file's worth of bytes is not one channel's
#: worth of picture, so it needs splitting before it means anything.
WHOLE_IMAGE_CHANNELS = ("rgb", "rgba")

#: What a declared lane MEANS, said in the neutral channel vocabulary this record
#: carries, and what has to happen to the numbers on the way.
#:
#: The declaration speaks the source engine's semantics; the record speaks a
#: rendering one. Smoothness and roughness are the same measurement counted from
#: opposite ends, and the side that knows it is holding smoothness is the side
#: that should turn it round -- the receiving host is then only ever asked to put
#: a picture in a channel, which is a thing any host can do without knowing whose
#: convention the file came from.
#:
#: A semantic that is NOT in here has no neutral channel, and is refused by name
#: rather than dropped into an approximate one.
LANE_CHANNELS = {
    "BaseColor": ("BaseColor", None),
    "Opacity": ("Opacity", None),
    "Metallic": ("Metallic", None),
    "Roughness": ("Roughness", None),
    "Smoothness": ("Roughness", "invert"),
    "SpecularLevel": ("SpecularLevel", None),
    "Occlusion": ("Occlusion", None),
    "Height": ("Height", None),
    "Emission": ("Emission", None),
    "PackedTangentNormal": ("TangentNormal", None),
}

#: Where each lane letter sits in an RGBA pixel.
LANE_COMPONENTS = {"r": 0, "g": 1, "b": 2, "a": 3}

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


def _declared_images(material):
    """The images a generated material states it binds, and what each one IS.

    A generated material has no surface node to read the answer off -- its graph
    is a stack of groups, and the image that feeds base colour arrives through a
    dozen nodes that all mean something. So the material says it instead, with
    the same channel packing declaration the shader on the other side was
    generated from.

    Three answers come back, because a texture is one of three things here:

    * a SURFACE CHANNEL. Sometimes that is the whole file (base colour,
      emission) and it crosses byte for byte; sometimes it is one lane of a
      packed file -- roughness in red, metal in green, a two-channel normal --
      and that lane has to be cut out and made into a picture before it is a
      channel at all. Both come back the same way, as the channel they land in
      plus the cut that makes them;
    * a LOOKUP the shader samples directly, when the slot declares no channel
      lane at all: a diffuse ramp, a shadow LUT, an SDF lightmap, a matcap. The
      whole file is what the shader wants and it wants it under the slot's own
      name, so it crosses unchanged. Refusing these because "a ramp is not a
      base colour" was the bug that left the generated shader sampling black --
      it has 22 such samplers and they carry the entire stylised look;
    * REFUSED, when the lane means something this record has no channel for. A
      mask the other side keeps in a numbered overflow slot is a real picture
      with nowhere neutral to put it, and inventing a place for it would be
      guessing. Those are named rather than dropped.
    """
    declaration = material.get(SHADING_DECLARATION)
    if declaration is None:
        return None, None, ()
    section = dict(declaration).get("images") or {}
    group = material.get(str(section.get("group") or "")) or {}
    packing = dict(section.get("packing") or {})
    found = {}
    raw = {}
    refused = []
    for slot in sorted(group.keys()):
        lanes = [tuple(lane) for lane in (packing.get(slot) or ())]
        image = bpy.data.images.get(str(group[slot]))
        if image is None:
            refused.append((slot, "names {0!r}, and no image here answers to it".format(
                str(group[slot]))))
            continue
        if not lanes:
            raw[slot] = image
            continue
        for lane, semantic, operation in lanes:
            translated = LANE_CHANNELS.get(semantic)
            if translated is None:
                refused.append((slot, "{0} lane means {1!r}, which is not a channel this "
                                      "record has a name for".format(lane, semantic)))
                continue
            channel, cut = translated
            if channel in found:
                continue
            if lane in WHOLE_IMAGE_CHANNELS and not operation and cut is None:
                found[channel] = (image, None, "")
                continue
            found[channel] = (image, lane, operation or cut or "")
    return found, raw, tuple(refused)


def images_of(material):
    """Every image this material renders with: the ones that are surface
    channels by neutral channel name, the ones the shader samples directly by
    the slot name it samples them under, and what could not cross -- so a
    texture that stayed behind is something said out loud rather than a channel
    that silently came up grey.
    """
    declared, raw, packed = _declared_images(material)
    if declared is not None:
        return declared, raw, packed
    if not material.use_nodes or material.node_tree is None:
        return {}, {}, ()
    surface = _surface_node(material.node_tree)
    if surface is None:
        return {}, {}, ()
    found = {}
    for input_name, channel in SURFACE_INPUTS:
        if channel in found:
            continue
        socket = surface.inputs.get(input_name)
        if socket is None:
            continue
        image = _image_behind(socket)
        if image is not None:
            found[channel] = (image, None, "")
    # An ordinary material has no slot vocabulary: everything it renders with
    # reaches a surface input, which is the whole of what it can say.
    return found, {}, ()


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


def _encode_whole(image, path):
    """The file this image already is, written out once.

    Encoding is the fallback and not the road: it is for an image that exists
    nowhere but in memory. Everything else is bytes that are already correct.
    """
    raw = _source_bytes(image)
    if raw is not None:
        with open(path, "wb") as handle:
            handle.write(raw)
        return
    previous = image.filepath_raw, image.file_format
    try:
        image.filepath_raw = path
        image.save()
    finally:
        image.filepath_raw, image.file_format = previous


def _write_image(image, directory, stem):
    """Put one image in the session, without making a second copy of it.

    Even copying bytes that are already correct is eighteen megabytes a send on
    one character, and every send does it again for images that did not move --
    so the copy happens once into the cache and every generation after that links
    to it.
    """
    path = os.path.join(directory, stem + _extension_of(image))
    stamp = _content_stamp(image)
    if stamp is None:
        _encode_whole(image, path)
    else:
        cache = _picture_cache_directory()
        kept = os.path.join(cache, "{0}_whole_{1}{2}".format(
            _safe(image.name), stamp, _extension_of(image)))
        if not os.path.isfile(kept):
            os.makedirs(cache, exist_ok=True)
            _encode_whole(image, kept)
        _place(kept, path)
    arena_module.keep_in_memory(path)
    return os.path.basename(path), os.path.getsize(path)


def _read_pixels(image):
    """One image as a height x width x RGBA array of numbers.

    ``foreach_get`` is the only read that does not go through Python per pixel;
    a 2048 square costs one allocation and one copy instead of sixteen million
    attribute lookups.
    """
    width, height = image.size
    flat = numpy.empty(width * height * 4, dtype=numpy.float32)
    image.pixels.foreach_get(flat)
    return flat.reshape(height, width, 4)


def _cut(image, lane, operation):
    """The picture one lane of a packed image actually is.

    A single lane becomes a grey picture of that measurement. A two-lane normal
    becomes a normal: the source keeps x and y and throws z away, because z is
    recoverable -- the vector is unit length -- and the two conventions differ
    only in which lanes x and y were parked in.
    """
    pixels = _read_pixels(image)
    if operation in ("unpack_normal_alpha_green", "unpack_normal_pair"):
        if operation == "unpack_normal_alpha_green":
            x = pixels[..., 0] * pixels[..., 3] * 2.0 - 1.0
        else:
            x = pixels[..., 0] * 2.0 - 1.0
        y = pixels[..., 1] * 2.0 - 1.0
        z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
        out = numpy.empty(pixels.shape, dtype=numpy.float32)
        out[..., 0] = x * 0.5 + 0.5
        out[..., 1] = y * 0.5 + 0.5
        out[..., 2] = z * 0.5 + 0.5
        out[..., 3] = 1.0
        return out
    component = LANE_COMPONENTS.get(lane)
    if component is None:
        raise ValueError("{0!r} is not a lane of one component".format(lane))
    value = pixels[..., component]
    if operation == "invert":
        value = 1.0 - value
    out = numpy.empty(pixels.shape, dtype=numpy.float32)
    out[..., 0] = value
    out[..., 1] = value
    out[..., 2] = value
    out[..., 3] = 1.0
    return out


#: Where the pictures that crossed are kept between sends. Beside the sessions
#: rather than inside one, because a generation is immutable and thrown away
#: while the picture made from an image that did not change is the same picture
#: every time.
PICTURE_CACHE_DIRECTORY_NAME = "pictures"


def _picture_cache_directory():
    return os.path.join(str(arena_module.default_root().parent),
                        PICTURE_CACHE_DIRECTORY_NAME)


def _place(kept, path):
    """Put a cached picture in the generation without copying its bytes again.

    A hard link is the same file under a second name: the generation gets a real
    entry that survives the cache being cleaned, and costs a directory write
    rather than the eighteen megabytes a character's textures actually are.
    Across volumes there is no such thing, so that falls back to a copy.
    """
    if os.path.exists(path):
        os.remove(path)
    try:
        os.link(kept, path)
    except OSError:
        shutil.copyfile(kept, path)


def _content_stamp(image):
    """Something that moves when an image's pixels do, cheaply.

    The bytes themselves are the only exact answer and reading a scene's worth of
    them to decide whether to skip work costs what the work costs. What is free
    is what the file system already knows: for a packed image the size of the
    block Blender is holding, for a linked one the size and modification time of
    the file it came from. An image that exists only as pixels in memory answers
    nothing, and is cut every time rather than cached wrongly.
    """
    packed = image.packed_file
    if packed is not None and packed.data:
        return "p{0}".format(len(packed.data))
    resolved = bpy.path.abspath(image.filepath_raw or image.filepath or "")
    if resolved and os.path.isfile(resolved):
        stat = os.stat(resolved)
        return "f{0}-{1}".format(int(stat.st_mtime), stat.st_size)
    return None


def _encode_cut(image, lane, operation, path):
    """Write the picture a lane is.

    The result is measurement, not colour -- a roughness map is not looked at,
    it is read -- so it is marked as data and no transfer function is applied on
    the way out.
    """
    pixels = _cut(image, lane, operation)
    height, width, _ = pixels.shape
    made = bpy.data.images.new(os.path.basename(path), width, height,
                               alpha=True, is_data=True)
    try:
        made.pixels.foreach_set(pixels.reshape(-1))
        made.file_format = "PNG"
        made.filepath_raw = path
        made.save()
    finally:
        bpy.data.images.remove(made)


def _write_cut(image, lane, operation, directory, stem):
    """One lane of a packed image, as a file beside the model.

    Cutting a 2048 square costs a quarter of a second, and a character's worth of
    packed maps is most of a full send. The cut only depends on the image and the
    lane, so it is kept: a later send of an unchanged document links to the
    picture instead of decoding, splitting and re-encoding it.
    """
    path = os.path.join(directory, stem + ".png")
    stamp = _content_stamp(image)
    if stamp is None:
        _encode_cut(image, lane, operation, path)
    else:
        cache = _picture_cache_directory()
        kept = os.path.join(cache, "{0}_{1}_{2}_{3}.png".format(
            _safe(image.name), lane, operation or "plain", stamp))
        if not os.path.isfile(kept):
            os.makedirs(cache, exist_ok=True)
            _encode_cut(image, lane, operation, kept)
        _place(kept, path)
    arena_module.keep_in_memory(path)
    return os.path.basename(path), os.path.getsize(path)


def _safe(name):
    return "".join(character if character.isalnum() or character in "-_." else "_"
                   for character in name)


def publish_into(staging, materials):
    """Write every material's textures into a staging generation.

    Returns the record section: material identity -> the channels it fills and
    the lookups its shader samples, each naming a file beside the model and the
    colour space the image itself declares. Empty when nothing in scope renders
    with an image, which is a real answer and not a failure.
    """
    directory = os.path.join(str(staging.directory), TEXTURE_DIRECTORY_NAME)
    written = {}
    seen = {}
    count = [0]
    total = [0]
    left_behind = {}

    def send(image, material, suffix, lane=None, operation=""):
        """One picture in the session, written once however many materials point
        at it. ``lane`` names the part of a packed image to cut out; without it
        the file crosses as it stands, which is both faster and exact."""
        key = (image.name, lane, operation)
        if key not in seen:
            if not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            try:
                seen[key] = (_write_image(image, directory, suffix) if lane is None
                             else _write_cut(image, lane, operation, directory, suffix))
            except Exception as error:
                LOG.warning("could not send %s for %s: %s", image.name,
                            material.name, error)
                return None
            count[0] += 1
            total[0] += seen[key][1]
        return {
            "file": seen[key][0],
            # The image's own answer for a file that crosses whole. A lane cut
            # out of it is measurement rather than colour, and says so. Nothing
            # here derives colour space from the channel or the file name; both
            # are measured to be wrong.
            "color_space": (image.colorspace_settings.name if lane is None
                            else record_module.COLOR_SPACE_DATA),
            "image": image.name,
            "lane": lane or "",
            "operation": operation or "",
        }

    for material in materials:
        found, raw, packed = images_of(material)
        for slot, why in packed:
            left_behind.setdefault("{0}: {1}".format(slot, why), 0)
            left_behind["{0}: {1}".format(slot, why)] += 1
        if not found and not raw:
            continue
        identity = material.get("ruri_bridge_identity") or material.name
        channels = {}
        for channel, (image, lane, operation) in sorted(found.items()):
            entry = send(image, material, "{0}_{1}".format(_safe(identity), channel),
                         lane, operation)
            if entry is not None:
                channels[channel] = entry
        lookups = {}
        for slot, image in sorted(raw.items()):
            entry = send(image, material, "{0}_{1}".format(_safe(identity), _safe(slot)))
            if entry is not None:
                lookups[slot] = entry
        if channels or lookups:
            written[identity] = {"channels": channels, "lookups": lookups}
    if count[0]:
        LOG.info("sent %d texture(s) for %d material(s), %.1f MB",
                 count[0], len(written), total[0] / 1e6)
    for reason, times in sorted(left_behind.items()):
        LOG.info("kept back, %dx: %s", times, reason)
    return {"directory": TEXTURE_DIRECTORY_NAME, "by_material": written} if written else {}
