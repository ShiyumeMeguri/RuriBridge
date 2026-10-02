# -*- coding: utf-8 -*-
"""Standing a generated material's own textures up again from Painter's channels.

A generated material samples the textures it was generated against -- a base map with
opacity in its alpha, a packed normal, a gloss map whose alpha is one minus roughness
-- and reads them through its own record, packed the way its shader reads them. Painter
paints channels. The textures record says, lane by lane, which exported map stands each
of those textures up and through which operation (the shader manifest's own table, see
the Painter side's ``slot_recipe``); this module does it per pixel, writes the texture
into the document's textures folder beside Painter's maps, and puts it into the slot of
the material's record. The material's runtime follows its record.

What Painter does not have is not made up:

* a lane whose channel this Painter stack lacks keeps what the material's own texture
  says there -- Painter has no opinion about it, so nothing changes;
* a lane the manifest keeps verbatim (a texture's leftover lanes) is the material's own;
* a texture whose every map from Painter is one flat value has nothing painted into it,
  and the material keeps its own -- standing it up from a blank channel would paint
  over the material with Painter's defaults. A Texture Set where that holds for every
  texture is reported as having nothing painted at all.

Values cross as stored: a byte of an sRGB channel stays the same byte in the texture,
which is also how the import put it into the channel. Files are written directly, not
through an image save, so no view transform and no colour management touch a lane.
"""

from __future__ import annotations

import concurrent.futures
import os

import bpy
import numpy

from ...Kernel.log import logger
from . import pixels

LOG = logger("blender.slots")

_LANE_LETTERS = "rgba"


def _relative(path):
    if not bpy.data.filepath:
        return path
    try:
        return bpy.path.relpath(path)
    except ValueError:
        return path


def _image_on(path, colour_space):
    """The image datablock that reads this file, made or reused, with the file's
    current pixels and the colour space the slot reads it in."""
    wanted = os.path.normcase(os.path.abspath(path))
    image = next((one for one in bpy.data.images
                  if pixels.file_of(one) and os.path.normcase(pixels.file_of(one)) == wanted), None)
    if image is None:
        image = bpy.data.images.load(path, check_existing=False)
        image.filepath = _relative(path)
    image.colorspace_settings.name = colour_space
    image.reload()
    return image


class _Painter:
    """The Texture Set's exported maps, read once each."""

    def __init__(self, directory, maps):
        self.directory = directory
        self.maps = {entry["channel"]: entry for entry in maps}
        self.read = {}

    def lanes(self, key):
        if key not in self.read:
            entry = self.maps[key]
            if len(entry["files"]) != 1:
                raise RuntimeError("{0} is tiled; a generated material samples one texture".format(
                    key))
            values, _wide = pixels.read(os.path.join(self.directory, entry["files"][0]))
            self.read[key] = values
        return self.read[key]

    def wide(self, key):
        return self.maps[key]["bit_depth"] != "8"

    def colour_space(self, key):
        return self.maps[key]["color_space"]

    def flat(self, keys):
        """Whether every one of these maps is one value over the whole texture."""
        for key in keys:
            values = self.lanes(key)
            if bool((values.max(axis=(0, 1)) != values.min(axis=(0, 1))).any()):
                return False
        return True


def _stand_up(recipe, painter, original, size):
    """One texture's lanes per its recipe. ``original`` is the material's own texture in
    this size, or None when the slot has none; returns (lanes, wide) or raises with why."""
    height, width = size[1], size[0]
    planes = []
    wide = original is not None and original[1]
    for index, lane in enumerate(recipe):
        if "raw" in lane:
            if original is None:
                planes.append(numpy.full((height, width), lane["absent"], dtype=numpy.float32))
            else:
                planes.append(original[0][..., _LANE_LETTERS.index(lane["raw"][lane["component"]])])
            continue
        if not lane["map"]:
            if original is None:
                planes.append(numpy.full((height, width), lane["absent"], dtype=numpy.float32))
            else:
                planes.append(original[0][..., index])
            continue
        values = painter.lanes(lane["map"])[..., lane["component"]]
        wide = wide or painter.wide(lane["map"])
        if lane["operation"] == "invert":
            values = 1.0 - values
        elif lane["operation"] == "repack_normal":
            # The export writes the tangent normal as n * 0.5 + 0.5 already, which is
            # exactly what repacking makes of the unpacked normal the shader reads.
            if lane["encoding"] != "Normal":
                raise RuntimeError("it repacks {0}, which is not a normal".format(lane["map"]))
        planes.append(values)
    return numpy.stack(planes, axis=-1), wide


def _needs_original(recipe):
    return any("raw" in lane or not lane["map"] for lane in recipe)


def _colour_space(recipe, painter, original_image):
    """What the slot reads the texture as: what its own texture was read as, else what
    the maps its colour lanes come from are stored as."""
    if original_image is not None:
        return original_image.colorspace_settings.name
    spaces = {painter.colour_space(lane["map"]) for lane in recipe[:3]
              if "raw" not in lane and lane["map"]}
    return spaces.pop() if len(spaces) == 1 else "Non-Color"


def compose(entry, directory, materials, declaration_key):
    """Stand the generated materials' textures up from one Texture Set's export.

    ``materials`` are the generated materials painting into it. Returns
    ``{"taken": {material: [slots]}, "flat": bool, "refused": {slot: why}}``.
    """
    name = entry["name"]
    report = {"taken": {}, "flat": False, "refused": dict(entry.get("slots_refused") or {})}
    painter = _Painter(directory, entry["maps"])
    recipes = {}
    for slot, recipe in entry["slots"].items():
        painted = sorted({lane["map"] for lane in recipe if "raw" not in lane and lane["map"]})
        if painted and not painter.flat(painted):
            recipes[slot] = recipe
    if not recipes:
        report["flat"] = True
        return report
    size = tuple(entry["resolution"])
    written = {}
    jobs = []
    plans = []
    for material in materials:
        images = dict(material[declaration_key]["images"])
        group = str(images["group"])
        held = dict(material.get(group) or {})
        for slot in sorted(set(dict(images["packing"])) & set(recipes)):
            recipe = recipes[slot]
            original_image = held.get(slot)
            original = None
            if _needs_original(recipe) and original_image is not None:
                path = pixels.file_of(original_image)
                if not path:
                    report["refused"][slot] = "{0} keeps lanes of {1}, which is not a file".format(
                        material.name, original_image.name)
                    continue
                original = pixels.read(path, size)
            key = (slot, original_image.name_full if original_image is not None else "")
            if key not in written:
                stem = "{0}_{1}".format(name, slot.lstrip("_"))
                if any(path == stem for path in written.values()):
                    stem = "{0}_{1}_{2}".format(name, material.name, slot.lstrip("_"))
                try:
                    lanes, wide = _stand_up(recipe, painter, original, size)
                except RuntimeError as error:
                    report["refused"][slot] = str(error)
                    continue
                written[key] = stem
                jobs.append((os.path.join(directory, stem + ".png"), lanes, wide))
            plans.append((material, group, slot, key,
                          _colour_space(recipe, painter, original_image)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        encoded = list(pool.map(lambda job: (job[0], pixels.png(job[1], job[2])), jobs))
    for path, payload in encoded:
        with open(path, "wb") as handle:
            handle.write(payload)
    for material, group, slot, key, colour_space in plans:
        if key not in written:
            continue
        image = _image_on(os.path.join(directory, written[key] + ".png"), colour_space)
        held = dict(material.get(group) or {})
        held[slot] = image
        material[group] = held
        material.update_tag()
        report["taken"].setdefault(material.name, []).append(slot)
    LOG.info("%s: %s", name, ", ".join("{0} took {1}".format(material, ", ".join(slots))
                                       for material, slots in sorted(report["taken"].items()))
             or "nothing taken")
    return report
