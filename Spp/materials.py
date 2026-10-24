# -*- coding: utf-8 -*-
"""Whole materials of a project: listed, and copied across the streams that describe each of them.

A material is its ``DataMaterial`` in the document -- stacks, layers, brushes, strokes -- together with the
``ShaderData`` of the viewer settings, which the material names by index (``shaderId``), and the
``BakingMaterial`` of the baking settings, which names the material by uid (``materialUID``) and owns the
tweak lists no other baking material refers to.
"""

from __future__ import annotations

from collections import Counter, namedtuple

from . import document, murmur, project, stream

Info = namedtuple("Info", "name uid scene_uid shader layers actions strokes")

_NAME = "sceneMaterialName"
_SCENE_UID = "sceneMaterialUID"
_SHADER = "shaderId"
_MASK = 0xFFFFFFFFFFFFFFFF


class Streams:
    """The three streams that describe a project's materials, read together and written together."""

    def __init__(self, source):
        self.document = stream.read(source.read(project.DOCUMENT))
        self.viewer = stream.read(source.read(project.VIEWER_SETTINGS))
        self.baking = stream.read(source.read(project.BAKING_SETTINGS))

    @property
    def materials(self):
        return self.document.root["materials"]

    @property
    def shaders(self):
        return self.viewer.root["materialSettings"]["shaders"]

    def save(self, target):
        for name, found in (("the document", self.document), ("the viewer settings", self.viewer), ("the baking settings", self.baking)):
            document.require_unique(found.root, name)
        target.write(project.DOCUMENT, stream.write(self.document))
        target.write(project.VIEWER_SETTINGS, stream.write(self.viewer))
        target.write(project.BAKING_SETTINGS, stream.write(self.baking))


def describe(streams):
    """An ``Info`` for each material of the document, in the order the document holds them."""
    found = []
    for material in streams.materials:
        types = Counter(item.type for item in stream.walk(material))
        found.append(Info(
            material.text(_NAME), material.number(document.UID), material.number(_SCENE_UID), material.number(_SHADER),
            sum(count for kind, count in types.items() if kind.startswith("DataLayer")),
            sum(count for kind, count in types.items() if kind.startswith("DataAction")),
            types["DataStroke"]))
    return found


def _material(streams, name):
    found = [material for material in streams.materials if material.text(_NAME) == name]
    if len(found) != 1:
        raise document.DocumentError("{0} materials are called {1}, not one".format(len(found), name))
    return found[0]


def _scene_uid(name, streams):
    taken = {material.number(_SCENE_UID) for material in streams.materials}
    value = murmur.hash128(name.encode("utf-8"))[0]
    while value in taken or value == 0:
        value = (value + 1) & _MASK
    return value


def clone(streams, source, name):
    """Add a material called ``name`` that is a copy of the one called ``source``; the uid of the new one.

    Everything under the copy takes new uids, so it shares nothing with the original but the pictures its
    layers and shader name by URL, which a project holds once however many materials use them. What inside the
    copy names something else inside it -- an anchor a fill reads, a layer an instance shows -- names the
    copy's own; what names something outside stays as it was.
    """
    original = _material(streams, source)
    if any(material.text(_NAME) == name for material in streams.materials):
        raise document.DocumentError("there is already a material called {0}".format(name))
    shader = _clone_shader(streams, original.number(_SHADER), name)
    copy, mapping = document.renewed(original, document.Uids(streams.document.root))
    for item, field, uid in document.references(copy):
        if uid in mapping:
            item.set_number(field, mapping[uid])
    copy.set_text(_NAME, name)
    copy.set_number(_SCENE_UID, _scene_uid(name, streams))
    copy.set_number(_SHADER, shader)
    streams.materials.append(copy)
    _clone_baking(streams.baking.root, original.number(document.UID), copy.number(document.UID))
    return copy.number(document.UID)


def _clone_shader(streams, index, name):
    copy, _mapping = document.renewed(streams.shaders[index], document.Uids(streams.viewer.root))
    copy.set_text("name", name)
    streams.shaders.append(copy)
    return len(streams.shaders) - 1


def _clone_baking(root, source, material):
    originals = [item for item in root["materials"] if item.number("materialUID") == source]
    if len(originals) != 1:
        raise document.DocumentError("{0} baking materials belong to the material, not one".format(len(originals)))
    original = originals[0]
    lists = {item.number(document.UID): item for item in root["tweakLists"]}
    uses = Counter(uid for _item, _field, uid in document.references(root))
    inside = Counter(uid for _item, _field, uid in document.references(original))
    owned = {uid for uid in inside if uid in lists and uses[uid] == inside[uid]}
    uids = document.Uids(root)
    copy, mapping = document.renewed(original, uids)
    copies = {}
    for item, field, uid in document.references(copy):
        if uid in mapping:
            item.set_number(field, mapping[uid])
        elif uid in owned:
            if uid not in copies:
                copies[uid], _mapping = document.renewed(lists[uid], uids)
                root["tweakLists"].append(copies[uid])
            item.set_number(field, copies[uid].number(document.UID))
        elif uid not in lists:
            raise document.DocumentError("{0}.{1} names {2}, which is no tweak list".format(item.type, field, uid))
    copy.set_number("materialUID", material)
    root["materials"].append(copy)
