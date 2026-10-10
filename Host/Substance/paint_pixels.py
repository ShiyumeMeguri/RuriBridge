# -*- coding: utf-8 -*-
"""Paint laid out in UV space, made pixels for a layout change and given back after it.

Paint that takes where it lands from the UVs every time Painter casts it again
(``project_document.bound``) cannot follow its islands into another layout: no layout but the
one it was made in puts it back. Painter's own record of it -- per channel the colour and the
coverage it lays, exported -- goes to Blender with the layout answer (``capture``); Blender writes
pictures of it, and with the new surface a fill of them stands in for the paint (``freeze``): at
the bottom of its stack, Replace, for a layer's or a mask's own strokes, which it covers; right
above an effect, blending as the effect does, the effect hidden. The fill is an ordinary picture
fill, reading the layout the paint was made in like every other. Moving back to that layout
takes the fill away again (``thaw``): the paint, cast where it was made, is what it was. Paint a
fill stands in for is not taken again in between: cast in a layout it was not made in, it is not
what it was.

A layer's own strokes, and an effect isolated over a transparent Replace fill and laid
normally, are exported with their alpha: the colour and the coverage exactly as Painter stores
them. A mask keeps no coverage: its own strokes are exported alone, opaque over the mask's
background, and an effect in it laid normally over 0 and over 1, which gives the coverage and
the colour. Infinite padding carries both past the islands, so what reads a picture across an
island's border reads the island. Every export is 32-bit: a narrower one stores height as
(h + 1) / 2, not as Painter holds it.

Faces carried into another Texture Set take a copy of the layers holding their paint, and in
the copy a fill stands in for the paint that cannot be cast again there (``carried``,
``stand_in``): for good -- the paint was made in a Texture Set the copy does not live in.
"""

from __future__ import annotations

import json
import os
import struct
import zlib

import substance_painter.colormanagement as colormanagement
import substance_painter.js
import substance_painter.layerstack as layerstack
import substance_painter.project
import substance_painter.resource
import substance_painter.textureset as textureset

from ...Kernel import arena as arena_module

from . import held_imports, project_document, project_facts, project_imports

#: Per fill the bridge laid in place of paint laid out in UV space, by uid: the Texture Set it
#: shows in, the layout the paint was made in, the layer and whether its mask holds the paint,
#: and the paint effect it stands in for with whether that was shown, ``{"texture_set", "layout",
#: "layer", "mask", "effect", "visible"}`` -- ``effect`` is None for a stack's own strokes,
#: which the fill covers.
FROZEN_KEY = "frozen"
#: Per fill standing for good in a copy carried into another Texture Set, by uid: the layer, whether
#: its mask holds the paint, and the paint effect it stands in for, ``{"layer", "mask", "effect"}``.
CARRIED_PAINT_KEY = "carried_paint"
#: Where a copy of the project is written to read its document, beside the sessions.
DOCUMENTS_FOLDER = "painter_documents"

#: How Painter reads back a picture of paint, by the name Blender gives the way its values are
#: stored: as they are, as -1..1 stored as 0..1, or as tangent normals one way or the other.
SPACES = {
    "raw": colormanagement.GenericColorSpace.Raw,
    "signed": colormanagement.DataColorSpace.DataSigned,
    "normal_opengl": colormanagement.NormalColorSpace.NormalXYZRight,
    "normal_directx": colormanagement.NormalColorSpace.NormalXYZLeft,
}
#: The channels Painter holds as signed values, -1..1.
_SIGNED = {textureset.ChannelType.Height}
_BITS = 32
#: The name an export knows a layer's mask by, and a picture of paint in a mask is filed under.
MASK = "mask"


class Frozen:
    """Paint of a Texture Set laid out in UV space (``project_document.Bound``), as the stack
    holds it now: its layer, the paint effect when it is one, and a name for it."""

    __slots__ = ("bound", "layer", "effect", "name")

    def __init__(self, bound, layer, effect, name):
        self.bound = bound
        self.layer = layer
        self.effect = effect
        self.name = name


def document():
    """The project's document as it stands: the project written as a copy beside the sessions,
    read, and the copy deleted."""
    folder = arena_module.default_root().parent / DOCUMENTS_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "{0}.spp".format(os.getpid())
    try:
        substance_painter.project.save_as_copy(str(path), substance_painter.project.ProjectSaveMode.Incremental)
        return project_document.read(str(path))
    finally:
        path.unlink(missing_ok=True)


def _shown_elsewhere(node, name):
    """The other Texture Sets showing a node through an instance of its layer or of a group
    holding it."""
    found = set()
    current = node
    while current is not None:
        if isinstance(current, layerstack.LayerNode):
            found |= {instance.get_texture_set().name for instance in current.instances()}
        current = current.get_parent()
    return found - {name}


def _identifiers(texture_set):
    """The channel names Painter's exporter lists for a Texture Set -- a user channel under its
    label; the channel's type name in lower case exports it all the same."""
    return set(json.loads(substance_painter.js.evaluate(
        "JSON.stringify(alg.mapexport.channelIdentifiers([{0}]))".format(json.dumps(texture_set.name)))))


def _standing():
    """What the fills standing in for paint stand in for: the effects, and per layer the stacks
    whose own strokes they cover."""
    records = list(remembered().values()) + [
        record for key, record in dict(project_facts.read(CARRIED_PAINT_KEY) or {}).items() if exists(key)]
    return ({int(record["effect"]) for record in records if record["effect"] is not None},
            {(int(record["layer"]), bool(record["mask"])) for record in records if record["effect"] is None})


def find(texture_set, problems):
    """Every piece of the Texture Set's paint laid out in UV space that no fill stands in for yet,
    ``[Frozen]``; what stands in the way of making one pixels goes into ``problems``."""
    try:
        bound = project_document.bound(document(), texture_set.name)
    except project_document.DocumentError as error:
        problems.append("the project file holds its strokes in a form the bridge does not read: {0}".format(error))
        return []
    return _standing_free(texture_set, bound, problems, True)


def root_of(node):
    """The root layer a node lies under, the node itself for a root layer."""
    while node.get_parent() is not None:
        node = node.get_parent()
    return node


def carried(texture_set, held, roots, problems):
    """Every piece of the Texture Set's paint under these root layers (uids) that a copy of them in
    another Texture Set cannot cast again where it lay -- laid out in UV space, or picked by polygon
    as the Texture Set's own triangles -- and no fill stands in for yet, ``[Frozen]``, read from the
    project's document ``held``; what stands in the way of making one pixels goes into
    ``problems``."""
    bound = [entry for entry in project_document.bound(held, texture_set.name, triangles=True)
             if root_of(layerstack.get_node_by_uid(entry.layer)).uid() in roots]
    return _standing_free(texture_set, bound, problems, False)


def _standing_free(texture_set, bound, problems, alone):
    """The paint of ``bound`` no fill stands in for yet, as the stack holds it now; with ``alone``,
    paint other Texture Sets show through instances is a problem -- it lies elsewhere there."""
    effects_standing, stacks_standing = _standing()
    bound = [entry for entry in bound if entry.uid not in effects_standing
             and not (entry.own and (entry.layer, entry.mask) in stacks_standing)]
    identifiers = _identifiers(texture_set) if bound else set()
    found = []
    for entry in bound:
        layer = layerstack.get_node_by_uid(entry.layer)
        effects = layer.mask_effects() if entry.mask else layer.content_effects()
        effect = next((one for one in effects if one.uid() == entry.uid), None)
        name = layer.get_name() if effect is None else "{0} / {1}".format(layer.get_name(), effect.get_name())
        if (effect is None) != entry.own:
            problems.append("{0}: the project file and the layer stack disagree about where its paint "
                            "lies".format(name))
            continue
        elsewhere = _shown_elsewhere(layer if effect is None else effect, texture_set.name) if alone else set()
        if elsewhere:
            problems.append("{0} holds {1}, and {2} show(s) it through instances, where that paint lies "
                            "elsewhere".format(name, ", ".join(entry.reasons), ", ".join(sorted(elsewhere))))
            continue
        stack = layer.get_stack()
        missing = sorted(channel.name for channel in stack.all_channels()
                         if not entry.mask and channel.name.lower() not in identifiers
                         and stack.get_channel(channel).label() not in identifiers)
        if missing:
            problems.append("{0}: Painter exports no channel named for {1}".format(name, ", ".join(missing)))
            continue
        found.append(Frozen(entry, layer, effect, name))
    return found


def _space(channel):
    if channel == textureset.ChannelType.Normal:
        return "normal"
    return "signed" if channel in _SIGNED else "raw"


def save(uid, channel, path, coverage):
    """Export one layer's channel as Painter holds it, 32-bit, padded past the islands; with
    ``coverage``, the coverage as alpha."""
    substance_painter.js.evaluate("alg.mapexport.save([{0}, {1}], {2}, {3})".format(
        uid, json.dumps(channel), json.dumps(path.replace("\\", "/")),
        json.dumps({"bitDepth": _BITS, "padding": "Infinite", "keepAlpha": coverage})))


def _hidden(nodes):
    """Hide the nodes shown; returns them, to show again."""
    shown = [node for node in nodes if node.is_visible()]
    for node in shown:
        node.set_visible(False)
    return shown


def _above(effects, effect):
    """The effects of a stack above ``effect`` -- all of them for the stack's own content."""
    if effect is None:
        return list(effects)
    return list(effects[:[one.uid() for one in effects].index(effect.uid())])


def _clear_picture(staging):
    """A fully transparent picture, to lay under an effect so it is laid over nothing."""
    path = str(staging.path("clear.png"))
    side = 4
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * side for _ in range(side))

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return project_imports.take_in(path, substance_painter.resource.Usage.TEXTURE,
                                   name="ruri_bridge_clear").identifier()


def _content(frozen, staging, clear):
    """The colour and coverage a layer's own strokes, or a content effect, lay in each channel."""
    layer, effect = frozen.layer, frozen.effect
    channels = sorted(layer.get_stack().all_channels(), key=lambda one: one.name)
    shown = _hidden(_above(layer.content_effects(), effect))
    under = None
    held = {}
    effect_shown = None
    try:
        if effect is not None:
            under = layerstack.insert_fill(layerstack.InsertPosition.below_node(effect))
            under.active_channels = set(channels)
            for channel in channels:
                under.set_source(channel, clear)
                under.set_blending_mode(layerstack.BlendingMode.Replace, channel)
                under.set_opacity(1.0, channel)
            held = {channel: (effect.get_blending_mode(channel), effect.get_opacity(channel)) for channel in channels}
            for channel in channels:
                effect.set_blending_mode(layerstack.BlendingMode.Normal, channel)
                effect.set_opacity(1.0, channel)
            effect_shown = effect.is_visible()
            effect.set_visible(True)
        found = []
        for channel in channels:
            file_name = "frozen_{0}_{1}.exr".format(frozen.bound.uid, channel.name)
            save(layer.uid(), channel.name.lower(), str(staging.path(file_name)), True)
            found.append({"channel": channel.name, "space": _space(channel), "file": file_name})
        return found
    finally:
        if under is not None:
            layerstack.delete_node(under)
        for channel, (mode, opacity) in held.items():
            effect.set_blending_mode(mode, channel)
            effect.set_opacity(opacity, channel)
        if effect_shown is not None:
            effect.set_visible(effect_shown)
        for node in shown:
            node.set_visible(True)


def _mask(frozen, staging):
    """The values a mask's own strokes lay over its background, or the coverage and value an
    effect in a mask lays, from its value over 0 and over 1."""
    layer, effect = frozen.layer, frozen.effect
    shown = _hidden(_above(layer.mask_effects(), effect))
    under = None
    held = None
    effect_shown = None
    stem = "frozen_{0}_mask".format(frozen.bound.uid)
    try:
        if effect is None:
            save(layer.uid(), MASK, str(staging.path(stem + ".exr")), False)
            return [{"channel": MASK, "space": "raw", "file": stem + ".exr"}]
        under = layerstack.insert_fill(layerstack.InsertPosition.below_node(effect))
        under.set_blending_mode(layerstack.BlendingMode.Replace)
        held = (effect.get_blending_mode(), effect.get_opacity())
        effect.set_blending_mode(layerstack.BlendingMode.Normal)
        effect.set_opacity(1.0)
        effect_shown = effect.is_visible()
        effect.set_visible(True)
        files = {}
        for value, key in ((0.0, "zero"), (1.0, "one")):
            under.set_source(None, colormanagement.Color(value, value, value))
            files[key] = "{0}_{1}.exr".format(stem, key)
            save(layer.uid(), MASK, str(staging.path(files[key])), False)
        return [dict(files, channel=MASK, space="raw")]
    finally:
        if under is not None:
            layerstack.delete_node(under)
        if held is not None:
            effect.set_blending_mode(held[0])
            effect.set_opacity(held[1])
        if effect_shown is not None:
            effect.set_visible(effect_shown)
        for node in shown:
            node.set_visible(True)


def capture(found, staging):
    """Painter's own record of each piece of paint, beside the answer: ``{"uid", "layer", "mask",
    "own", "name", "reasons", "channels"}``, each channel ``{"channel", "space", "file"}`` -- an
    export holding the colour and, as its alpha, the coverage -- or for an effect in a mask
    ``{"channel", "space", "zero", "one"}``, the mask with it laid over 0 and over 1. ``space``
    is how the values are stored back: ``raw``, ``signed`` (-1..1) or ``normal``. Each layer
    is shown while it is exported, and everything is put back as it was."""
    clear = None
    entries = []
    for frozen in found:
        layer = frozen.layer
        layer_shown = layer.is_visible()
        layer.set_visible(True)
        try:
            if frozen.bound.mask:
                channels = _mask(frozen, staging)
            else:
                if frozen.effect is not None and clear is None:
                    clear = _clear_picture(staging)
                channels = _content(frozen, staging, clear)
        finally:
            layer.set_visible(layer_shown)
        entries.append({"uid": frozen.bound.uid, "layer": frozen.bound.layer, "mask": frozen.bound.mask,
                        "own": frozen.bound.own, "name": frozen.name, "reasons": list(frozen.bound.reasons),
                        "channels": channels})
    return entries


def exists(uid):
    """Whether a node of that uid is in the project."""
    try:
        layerstack.get_node_by_uid(int(uid))
    except ValueError:
        return False
    return True


def remembered():
    """The fills standing in for paint whose fill, and effect when there is one, are still there."""
    return {key: record for key, record in dict(project_facts.read(FROZEN_KEY) or {}).items()
            if exists(key) and (record["effect"] is None or exists(record["effect"]))}


def thawing(texture_set):
    """The fills standing in for paint of the Texture Set, each with the layout the paint was
    made in, ``[{"uid", "layout"}]``: moving there takes them away again."""
    return [{"uid": int(key), "layout": record["layout"]} for key, record in sorted(remembered().items())
            if record["texture_set"] == texture_set]


def problem(uid, target):
    """What stands in the way of laying a fill in place of paint Blender made pixels, or empty."""
    try:
        layer = layerstack.get_node_by_uid(int(target["layer"]))
    except ValueError:
        return "the layer holding {0} is gone".format(target["name"])
    if target["own"]:
        if target["mask"] and not layer.has_mask():
            return "{0} has no mask any more".format(target["name"])
        if not target["mask"] and not isinstance(layer, layerstack.PaintLayerNode):
            return "{0} is no paint layer any more".format(target["name"])
        return ""
    effects = layer.mask_effects() if target["mask"] else layer.content_effects()
    if uid not in {one.uid() for one in effects}:
        return "{0} is gone".format(target["name"])
    return ""


def held(target, directory):
    """A piece of paint Blender made pixels, its pictures -- delivered beside the record in
    ``directory``, ``{"file", "hash", "space"}`` -- where Painter imports them from, ``{"path",
    "space"}``."""
    return dict(target, pictures={name: {"path": held_imports.delivered(directory, picture), "space": picture["space"]}
                                  for name, picture in target["pictures"].items()})


def freeze(uid, target, records):
    """Lay the pictures Blender made of a piece of paint in a fill standing in for it
    (``stand_in``) and remember it (``records``). Returns the fill, still reading set 0."""
    layer = layerstack.get_node_by_uid(int(target["layer"]))
    effect = None if target["own"] else layerstack.get_node_by_uid(uid)
    shown = True if effect is None else effect.is_visible()
    fill = stand_in(layer, effect, target)
    records[str(fill.uid())] = {"texture_set": target["texture_set"], "layout": target["layout"],
                                "layer": int(target["layer"]), "mask": bool(target["mask"]),
                                "effect": None if effect is None else uid, "visible": shown}
    return fill


def stand_in(layer, effect, target):
    """Lay the pictures Blender made of a piece of paint in a fill standing in for it -- at the
    bottom of ``layer``'s stack, Replace, for the stack's own strokes (no ``effect``); right above
    ``effect``, blending as it does, the effect hidden -- each picture read back the way its values
    are stored. ``target`` says whether the mask holds the paint and names the pictures, ``{"mask",
    "pictures": {channel: {"path", "space"}}}``. Returns the fill, still reading set 0."""
    if effect is None:
        effects = layer.mask_effects() if target["mask"] else layer.content_effects()
        position = (layerstack.InsertPosition.below_node(effects[-1]) if effects else
                    layerstack.InsertPosition.inside_node(
                        layer, layerstack.NodeStack.Mask if target["mask"] else layerstack.NodeStack.Content))
    else:
        position = layerstack.InsertPosition.above_node(effect)
    pictures = {name: (project_imports.take_in(entry["path"], substance_painter.resource.Usage.TEXTURE,
                                               name=os.path.splitext(os.path.basename(entry["path"]))[0]).identifier(),
                       SPACES[entry["space"]])
                for name, entry in target["pictures"].items()}
    fill = layerstack.insert_fill(position)
    if target["mask"]:
        identifier, space = pictures[MASK]
        fill.set_source(None, identifier).set_color_space(space)
        fill.set_blending_mode(layerstack.BlendingMode.Replace if effect is None else effect.get_blending_mode())
        fill.set_opacity(1.0 if effect is None else effect.get_opacity())
    else:
        channels = {getattr(textureset.ChannelType, name): name for name in pictures}
        fill.active_channels = set(channels)
        for channel, name in channels.items():
            identifier, space = pictures[name]
            fill.set_source(channel, identifier).set_color_space(space)
            fill.set_blending_mode(layerstack.BlendingMode.Replace if effect is None
                                   else effect.get_blending_mode(channel), channel)
            fill.set_opacity(1.0 if effect is None else effect.get_opacity(channel), channel)
    shown = True if effect is None else effect.is_visible()
    fill.set_name("{0} (pixels)".format((layer if effect is None else effect).get_name()))
    fill.set_visible(shown)
    if effect is not None:
        effect.set_visible(False)
    return fill


def rename(renames):
    """Texture Sets renamed, old name to new: the fills standing in for paint follow the name."""
    records = dict(project_facts.read(FROZEN_KEY) or {})
    if any(record["texture_set"] in renames for record in records.values()):
        project_facts.write(FROZEN_KEY, {uid: dict(record, texture_set=renames.get(record["texture_set"],
                                                                                   record["texture_set"]))
                                         for uid, record in records.items()})


def thaw(uid, records):
    """Take away a fill standing in for paint, showing the effect it stood in for as it was."""
    record = records.pop(str(uid))
    if record["effect"] is not None:
        layerstack.get_node_by_uid(int(record["effect"])).set_visible(bool(record["visible"]))
    layerstack.delete_node(layerstack.get_node_by_uid(int(uid)))
