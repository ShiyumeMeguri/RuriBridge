# -*- coding: utf-8 -*-
"""A canvas: the paint of a material, kept where it was painted and read from there by the material that replaced it.

Painter replays every stroke and every polygon fill of a project against the surface it holds, so paint painted on one
surface and replayed on another lands differently, in ways no edit of the strokes undoes everywhere: a stroke that
crossed a seam, a symmetry copy, a projection through a camera, a clone, a pass of a generator that read the mesh.
The root of all of it is that the surface the paint is replayed on is not the one it was painted on.

A canvas gives the paint back its surface. For a material ``M`` whose mesh changed while its UV layout stayed:

* the material is copied under a new name, the canvas ``G``, with every stack, layer, brush and stroke it has, and the
  layers of each stack go into one folder that ends in an anchor point, so that the stack's result is a thing others can read;
* the surface ``G`` paints on is a copy of the one ``M`` was painted on, placed where it was and called what it was
  called (``canvas_geometry``), so that every stroke of ``G`` replays on the surface it was made on, whatever Painter does
  with it, and ``G`` shows what ``M`` showed;
* ``M`` keeps one layer per stack, a fill that reads the anchor of ``G`` through the UV layout, which stayed: every texel of
  ``M`` is the texel of ``G`` at the same place of the layout, carried over without a resample.

Painter's interface refuses a layer of one Texture Set reading an anchor of another as a cycle; its document does not,
and a project saved with such a reference opens and evaluates it. So the references are written into the document, here.

What a canvas is made of -- a folder, an anchor, a fill, a reference -- is data (``templates``): one object of each kind
Painter itself wrote, kept in ``canvas_templates.bin`` and refreshed from a project Painter saved
(``make_templates``). What the document defines is checked against them before anything is written, so a Painter whose
schema differs from the one they were made for refuses rather than writes a document it may not open.
"""

from __future__ import annotations

import os

from .. import document as document_module
from .. import materials, stream

TEMPLATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "canvas_templates.bin")
#: What a canvas is called: the name of the material it is a copy of, this, and a number.
CANVAS_MARK = "~canvas"
GROUP_LABEL = "画布"
ANCHOR_LABEL = "画布锚点"
TRANSFER_LABEL = "读取画布"

_NAME = "sceneMaterialName"
_ROOT = "RuriCanvasTemplates"
_KINDS = ("group", "anchor", "source", "fill", "layer")
_GROUP = "DataLayerGroup"
_ANCHOR = "DataActionAnchor"
_REFERENCE = "DataSourceReference"
_FILL = "DataActionFill"
_LAYER = "DataLayerColor"
#: The types of what a canvas takes along that other things of the document may name: layers, and the effects and masks of layers.
LAYERS = ("DataLayer", "DataAction")
_UV_PROJECTION = 0
_ALL_CHANNELS = bytes([0xFF] * 16)
_CHANNEL_BYTES = 16


class CanvasError(document_module.DocumentError):
    """A canvas that cannot be made, and why."""


class Templates:
    """One object of each kind a canvas is made of."""

    def __init__(self, objects):
        self.objects = objects

    def conform(self, root):
        """Raise ``CanvasError`` unless every type these objects use is defined, in the stream under ``root``, as they define it."""
        used = _definitions(list(self.objects.values()))
        held = _definitions(root)
        different = sorted(name for name, definition in used.items() if name in held and held[name].signature != definition.signature)
        if different:
            raise CanvasError("the canvas templates were made for another document schema: {0} differ; make them again from a project this "
                              "Painter saved".format(", ".join(different)))


def _definitions(root):
    """The definition of every type under ``root`` -- an object, or a list of them -- by name."""
    found = {}
    for item in stream.walk(root):
        found.setdefault(item.definition.name, item.definition)
        for value in item.values:
            if isinstance(value, stream.Packed):
                found.setdefault(value.definition.name, value.definition)
    return found


def templates():
    """The templates in ``canvas_templates.bin``."""
    with open(TEMPLATES_FILE, "rb") as handle:
        read = stream.read(handle.read())
    return Templates({kind: read.root[kind] for kind in _KINDS})


def make_templates(document):
    """The bytes of a templates file made from a document Painter wrote (a ``stream.read``), which holds a folder whose effects
    hold an anchor, a fill that reads references and a fill layer: one object of each kind, emptied of what belongs to the
    project it came from."""
    group = next((item for item in stream.walk(document.root) if item.type == _GROUP and item["actions"] is not None
                  and any(action.type == _ANCHOR for action in item["actions"]["items"])), None)
    source = next((item for item in stream.walk(document.root) if item.type == _REFERENCE), None)
    fill = next((item for item in stream.walk(document.root) if item.type == _FILL
                 and any(entry is not None and entry.type == _REFERENCE for entry in item["sources"])), None)
    layer = next((item for item in stream.walk(document.root) if item.type == _LAYER and item["actions"] is not None
                  and item["maskActions"] is None and any(action.type == _FILL for action in item["actions"]["items"])), None)
    missing = [name for name, found in (("folder with an anchor in its effects", group), ("reference source", source),
                                        ("fill that reads references", fill), ("fill layer", layer)) if found is None]
    if missing:
        raise CanvasError("the project holds no {0}".format(", ".join(missing)))
    group, fill, layer = group.copy(), fill.copy(), layer.copy()
    anchor = next(action for action in group["actions"]["items"] if action.type == _ANCHOR).copy()
    for owner, field in ((group, "actions"), (group, "maskActions"), (group, "subStack"), (layer, "actions"), (layer, "maskActions")):
        if owner[field] is not None:
            owner[field]["items"] = []
    fill["sources"] = []
    definition = stream.Definition(_ROOT, [(kind, stream.OBJECT) for kind in _KINDS])
    root = stream.Object(definition, [group, anchor, source.copy(), fill, layer])
    return stream.write(stream.Stream(document.version, document.schema, stream.OBJECT, root))


def _mask(code):
    """The block of channel types holding only the one with this code."""
    block = bytearray(_CHANNEL_BYTES)
    block[code // 8] |= 1 << (code % 8)
    return bytes(block)


def _renewed(template, uids, label=None):
    found, _mapping = document_module.renewed(template, uids)
    if label is not None:
        found.set_text("label", label)
    return found


def _folder(made, layers, anchor, uids):
    """A folder holding ``layers`` that the anchor ends. It blends nothing and masks nothing: what is in it is the stack."""
    group = _renewed(made.objects["group"], uids, GROUP_LABEL)
    group.set_number("maskEnabled", 0)
    group["actions"]["items"] = [anchor]
    group["subStack"]["items"] = layers
    return group


def _transfer(made, codes, anchor, uids):
    """A fill layer that reads each channel of ``codes`` from the anchor, through the UV layout."""
    layer = _renewed(made.objects["layer"], uids, TRANSFER_LABEL)
    fill = _renewed(made.objects["fill"], uids)
    sources = []
    for code in codes:
        source = _renewed(made.objects["source"], uids)
        source.set_number("channelTypes", _mask(code))
        reference = source["reference"]
        reference.set_number("source", anchor.number(document_module.UID))
        reference["bindings"] = stream.Packed.of(reference["bindings"].definition, [(code, code)])
        sources.append(source)
    fill["sources"] = sources
    fill.set_number("projection", _UV_PROJECTION)
    fill.set_number("uvTransferChannel", 0)
    fill.set_number("channelTypes", _ALL_CHANNELS)
    layer["actions"]["items"] = [fill]
    return layer


def _owners(streams):
    """The name of the material each anchor point of the document is in, by uid."""
    return {item.number(document_module.UID): material.text(_NAME)
            for material in streams.materials for item in stream.walk(material) if item.type == _ANCHOR}


def _foreign_reads(material, owners):
    """The materials whose anchor points ``material`` has references to."""
    name = material.text(_NAME)
    return {owners[uid] for item in stream.walk(material) if item.type == _REFERENCE
            for uid in (item["reference"].number("source"),) if uid in owners and owners[uid] != name}


def canvases(streams):
    """The materials that others read, with the materials that read each: ``{canvas: [reader, ...]}`` by name. A material is a
    canvas when a reference of another material names an anchor point in it; nothing else says so."""
    owners = _owners(streams)
    found = {}
    for material in streams.materials:
        for canvas in sorted(_foreign_reads(material, owners)):
            found.setdefault(canvas, []).append(material.text(_NAME))
    return found


def _reads_only_canvases(entry, owners, name):
    """Whether a stack is nothing but one fill layer that reads other materials' anchor points."""
    layers = entry["stack"]["items"]
    if len(layers) != 1 or layers[0].type != _LAYER or layers[0]["maskActions"] is not None:
        return False
    fills = [action for action in layers[0]["actions"]["items"] if action.type == _FILL]
    if len(fills) != 1 or len(layers[0]["actions"]["items"]) != 1 or not fills[0]["sources"]:
        return False
    return all(source.type == _REFERENCE and owners.get(source["reference"].number("source"), name) != name for source in fills[0]["sources"])


def _free_name(streams, name):
    taken = {material.text(_NAME) for material in streams.materials}
    number = 1
    while "{0}{1}{2}".format(name, CANVAS_MARK, number) in taken:
        number += 1
    return "{0}{1}{2}".format(name, CANVAS_MARK, number)


def lay(streams, name, made):
    """Give the material called ``name`` a canvas, in the streams of an open project: ``(canvas, mapping)``, the canvas's name and the
    new uid of each uid the material's layers had.

    Every stack of the material that holds layers moves to the canvas, and the material is left reading it; whatever else of the
    document named a layer that moved -- an instance of it in another material -- names it where it is now. ``(None, {})`` is
    returned, and nothing is changed, for a material with nothing in its stacks or nothing but what a canvas already holds.
    """
    found = [material for material in streams.materials if material.text(_NAME) == name]
    if len(found) != 1:
        raise CanvasError("{0} materials are called {1}, not one".format(len(found), name))
    material = found[0]
    if material.number("useUVTiles"):
        raise CanvasError("{0} is laid out in UV tiles, which a canvas does not read".format(name))
    owners = _owners(streams)
    stacks = [entry for entry in material["stacks"] if entry["stack"]["items"]]
    if not stacks or all(_reads_only_canvases(entry, owners, name) for entry in stacks):
        return None, {}
    made.conform(streams.document.root)
    canvas = _free_name(streams, name)
    uid, mapping = materials.clone(streams, name, canvas)
    moving = {item.number(document_module.UID) for entry in stacks for item in stream.walk(entry["stack"])
              if document_module.UID in item and item.type.startswith(LAYERS)}
    mapping = {old: new for old, new in mapping.items() if old in moving}
    held = next(item for item in streams.materials if item.number(document_module.UID) == uid)
    uids = document_module.Uids(streams.document.root)
    for own, copied in zip(material["stacks"], held["stacks"]):
        layers = list(copied["stack"]["items"])
        if not layers:
            continue
        anchor = _renewed(made.objects["anchor"], uids, ANCHOR_LABEL)
        codes = [channel.number("type") for channel in copied["channels"]]
        copied["stack"]["items"] = [_folder(made, layers, anchor, uids)]
        own["stack"]["items"] = [_transfer(made, codes, anchor, uids)]
    for item, field, named in document_module.references(streams.document.root):
        if named in mapping:
            item.set_number(field, mapping[named])
    return canvas, mapping
