# -*- coding: utf-8 -*-
"""Laying a material's own pictures out again when its UV maps swap.

A picture the material samples through one of the two swapped UV maps, one to one --
no tiling, turning or offset -- is made for that map's layout, and after the swap the
map holds the other layout. It is laid out again in what the map holds after the swap
(``chart_resample``), written beside the original as a new file, and the material
takes the new picture; the original file and datablock stay as they are, for other
materials and for an undo. A picture sampled through a transformation is a tile: it
keeps tiling the map, like a tile in Painter. A picture sampled at coordinates that do
not come from a UV map -- a ramp read at a lighting value -- has no layout at all.

What samples what is read off the material's node tree: an Image Texture node's
vector input is followed upstream through vectors only, so a ramp read at a computed
scalar is not mistaken for a UV lookup. A generated material names its pictures by
slot in its own record and states each slot's transformation in its row as
``<slot>_ST``; its tangent-space normals are the slots its packing unpacks as normals.
Any other material's tangent normals are the pictures feeding a Normal Map node. A
tangent normal is decoded in the tangent frame of the map its Normal Map nodes name --
the render map when they name none -- and is carried into the frame that map gives
after the swap (``layout_triangles``).
"""

from __future__ import annotations

import hashlib
import os

import bpy
import numpy

from ...Kernel.log import logger

from . import chart_resample, layout_triangles, mesh_publish, pixels

LOG = logger("blender.relayout")

#: Node types a UV coordinate passes through unchanged as far as which map it comes from.
_PASSING = frozenset(("REROUTE", "MAPPING", "VECT_MATH", "GROUP", "SEPXYZ", "COMBXYZ"))
_TOLERANCE = 1e-6
#: What a picture's layer is when it reads a UV map the swap does not touch.
_UNMOVED = None


def _sources(socket, depth=0):
    """The UV maps a vector socket's value is read from, by name (``""`` for the map the
    object renders with), and whether a transformation stands in between."""
    if not socket.is_linked or depth > 32:
        return set(), False
    link = socket.links[0]
    node = link.from_node
    if node.type == "TEX_COORD":
        return ({""} if link.from_socket.name == "UV" else set()), False
    if node.type == "UVMAP":
        return {node.uv_map}, False
    if node.type == "ATTRIBUTE":
        return {node.attribute_name}, False
    if node.type not in _PASSING:
        return set(), False
    found = set()
    transformed = node.type == "MAPPING" and not _identity_mapping(node)
    for one in node.inputs:
        if one.is_linked and one.type == "VECTOR":
            names, turned = _sources(one, depth + 1)
            found |= names
            transformed = transformed or turned
    return found, transformed


def _identity_mapping(node):
    values = {one.name: one.default_value for one in node.inputs if not one.is_linked}
    location = values.get("Location", (0.0, 0.0, 0.0))
    rotation = values.get("Rotation", (0.0, 0.0, 0.0))
    scale = values.get("Scale", (1.0, 1.0, 1.0))
    return (all(abs(value) <= _TOLERANCE for value in location)
            and all(abs(value) <= _TOLERANCE for value in rotation)
            and all(abs(value - 1.0) <= _TOLERANCE for value in scale)
            and not any(one.is_linked for one in node.inputs if one.name in ("Location", "Rotation", "Scale")))


class Picture:
    """One picture a material lays out through a swapped map, where it sits, and the map
    whose tangent frame it is decoded in when it is a tangent normal."""

    __slots__ = ("image", "layer", "places", "normal", "frame")

    def __init__(self, image, layer, normal, frame):
        self.image = image
        self.layer = layer
        self.places = []
        self.normal = normal
        self.frame = frame


def _layers(names, render_names, target_layer):
    """The maps a set of read map names is, as the swap sees them: ``""`` for the render
    map (any mesh's own name for it, or the unnamed one), the target map's name, and
    ``_UNMOVED`` for any map the swap leaves alone."""
    found = set()
    for name in names:
        if name == "" or name in render_names:
            found.add("")
        elif name == target_layer:
            found.add(target_layer)
        else:
            found.add(_UNMOVED)
    return found


def _layer(material, what, names, render_names, target_layer):
    """The one swapped map a picture is laid out in, or ``_UNMOVED`` when it reads none."""
    found = _layers(names, render_names, target_layer)
    moved = found - {_UNMOVED}
    if not moved:
        return _UNMOVED
    if len(found) > 1:
        raise RuntimeError("{0} samples {1} through {2}; laid out again for one of them it is "
                           "wrong for the other".format(material.name, what, ", ".join(
                               sorted(repr(name) for name in names))))
    return next(iter(moved))


def _frame(material, render_names, target_layer):
    """The map whose tangent frame the material decodes its tangent normals in."""
    names = {node.uv_map for node in material.node_tree.nodes
             if node.type == "NORMAL_MAP" and node.space == "TANGENT"} or {""}
    found = _layers(names, render_names, target_layer)
    if len(found) > 1:
        raise RuntimeError("{0} decodes tangent normals in the frames of {1} at once; a "
                           "picture can follow only one".format(material.name, ", ".join(
                               sorted(repr(name) for name in names))))
    return next(iter(found))


def _generated_pictures(material, render_names, target_layer, found):
    declaration = material[mesh_publish.SHADING_DECLARATION]
    images = dict(declaration["images"])
    group = str(images["group"])
    held = dict(material.get(group) or {})
    packing = dict(images.get("packing") or {})
    row = (mesh_publish.declared_row(material) or {}).get("parameters", {})
    by_slot = {}
    for node in material.node_tree.nodes if material.node_tree is not None else ():
        if node.type == "TEX_IMAGE" and node.label in held:
            names, transformed = _sources(node.inputs["Vector"])
            entry = by_slot.setdefault(node.label, [set(), False])
            entry[0] |= names
            entry[1] = entry[1] or transformed
    frame = _frame(material, render_names, target_layer)
    for slot, image in held.items():
        if image is None or slot not in by_slot:
            continue
        names, transformed = by_slot[slot]
        layer = _layer(material, slot, names, render_names, target_layer)
        if layer is _UNMOVED or transformed:
            continue
        transform = row.get("{0}_ST".format(slot))
        if transform is not None and not _identity_st(transform):
            continue
        unpack = [lane for lane in packing.get(slot) or [] if str(lane[2]).startswith("unpack_normal")]
        normal = ("packed", unpack[0][0], unpack[0][2]) if unpack else None
        key = (image.as_pointer(), layer)
        picture = found.setdefault(key, Picture(image, layer, normal, frame))
        picture.places.append(("slot", material, group, slot))


def _identity_st(transform):
    values = list(transform)
    return (abs(values[0] - 1.0) <= _TOLERANCE and abs(values[1] - 1.0) <= _TOLERANCE
            and abs(values[2]) <= _TOLERANCE and abs(values[3]) <= _TOLERANCE)


def _normal_map_frame(tree, node, render_names, target_layer, material):
    """The map of the tangent Normal Map node this picture feeds, or False when it feeds none."""
    names = {link.to_node.uv_map for link in tree.links
             if link.from_node == node and link.to_node.type == "NORMAL_MAP"
             and link.to_node.space == "TANGENT"}
    if not names:
        return False
    found = _layers(names, render_names, target_layer)
    if len(found) > 1:
        raise RuntimeError("{0} decodes {1} in the frames of {2} at once".format(
            material.name, node.name, ", ".join(sorted(repr(name) for name in names))))
    return next(iter(found))


def _node_pictures(material, render_names, target_layer, found):
    tree = material.node_tree
    if tree is None:
        return
    for node in tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None:
            continue
        socket = node.inputs["Vector"]
        names, transformed = _sources(socket) if socket.is_linked else ({""}, False)
        layer = _layer(material, node.name, names, render_names, target_layer)
        if layer is _UNMOVED or transformed:
            continue
        frame = _normal_map_frame(tree, node, render_names, target_layer, material)
        normal = ("rgb",) if frame is not False else None
        key = (node.image.as_pointer(), layer)
        picture = found.setdefault(key, Picture(node.image, layer, normal,
                                                _UNMOVED if frame is False else frame))
        picture.places.append(("node", material, node.name))


def _decode(values, normal):
    """A picture's tangent normals as xyz in [-1, 1], and how to put them back."""
    if normal[0] == "rgb":
        return values[..., :3] * 2.0 - 1.0
    lanes, operation = normal[1], normal[2]
    picked = [values[..., "rgba".index(letter)] for letter in lanes]
    x = picked[0] * (values[..., 3] if operation == "unpack_normal_alpha_green" else 1.0)
    y = picked[1]
    x = x * 2.0 - 1.0
    y = y * 2.0 - 1.0
    return numpy.stack((x, y, numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))), axis=-1)


def _encode(values, vectors, normal):
    out = values.copy()
    if normal[0] == "rgb":
        out[..., :3] = vectors * 0.5 + 0.5
        return out
    lanes, operation = normal[1], normal[2]
    x = vectors[..., 0] * 0.5 + 0.5
    y = vectors[..., 1] * 0.5 + 0.5
    first = "rgba".index(lanes[0])
    if operation == "unpack_normal_alpha_green":
        if numpy.all(values[..., 3] == 1.0):
            out[..., first] = x
        elif numpy.all(values[..., first] == 1.0):
            out[..., 3] = x
        else:
            raise RuntimeError("a normal stored as x = R x A with neither lane constant cannot be "
                               "written back exactly")
    else:
        out[..., first] = x
    out[..., "rgba".index(lanes[1])] = y
    return out


def _relaid(picture, triangles):
    path = pixels.file_of(picture.image)
    if not path:
        values = numpy.empty(picture.image.size[0] * picture.image.size[1] * 4, dtype=numpy.float32)
        picture.image.pixels.foreach_get(values)
        values = values.reshape(picture.image.size[1], picture.image.size[0], 4)
        if bool((values == values[0, 0]).all()):
            return None
        raise RuntimeError("{0} is not a file on disk; save it before its layout can change".format(
            picture.image.name))
    values, wide = pixels.read(path)
    if bool((values == values[0, 0]).all()):
        return None
    if values.min() < 0.0 or values.max() > 1.0:
        raise RuntimeError("{0} holds values outside 0..1, which a re-laid PNG cannot keep".format(
            picture.image.name))
    old, new = ((triangles.render, triangles.target) if picture.layer == ""
                else (triangles.target, triangles.render))
    laid = chart_resample.relaid(values, old, new, "value")
    if picture.normal is not None and picture.frame is not _UNMOVED:
        vectors = _decode(values, picture.normal)
        laid_normal = chart_resample.relaid(vectors * 0.5 + 0.5, old, new, "tangent",
                                            triangles.frames(picture.frame))
        laid = _encode(laid, laid_normal * 2.0 - 1.0, picture.normal)
    return laid, wide, path


def relay(materials, wearers, render_names, target_layer, directory):
    """Lay out again every picture these materials sample through the render map or the
    target map, for the swap about to happen, writing each into ``directory`` under a name
    its bytes give it. Nothing of the document changes yet: returns what ``install`` puts
    in place."""
    found = {}
    names = set(render_names.values())
    for material in materials:
        if material.node_tree is None:
            continue
        if material.get(mesh_publish.SHADING_DECLARATION) is not None:
            _generated_pictures(material, names, target_layer, found)
        else:
            _node_pictures(material, names, target_layer, found)
    if not found:
        return []
    if not directory:
        raise RuntimeError("this .blend has never been saved, so the pictures laid out again "
                           "have no textures folder to go to")
    tangent = any(picture.normal is not None and picture.frame is not _UNMOVED
                  for picture in found.values())
    triangles = layout_triangles.gather([object_reference for object_reference, _polygons in wearers],
                                        materials, render_names, target_layer, frames=tangent)
    if triangles is None:
        return []
    os.makedirs(directory, exist_ok=True)
    made = []
    for picture in found.values():
        result = _relaid(picture, triangles)
        if result is None:
            continue
        values, wide, path = result
        stem = os.path.splitext(os.path.basename(path))[0]
        data = pixels.png(numpy.clip(values, 0.0, 1.0), wide)
        target = os.path.join(directory, "{0}_{1}.png".format(stem, hashlib.sha1(data).hexdigest()[:12]))
        with open(target, "wb") as handle:
            handle.write(data)
        made.append((picture, target))
    return made


def install(made):
    """Give every material the pictures ``relay`` laid out for it."""
    for picture, target in made:
        image = bpy.data.images.load(target, check_existing=False)
        try:
            image.filepath = bpy.path.relpath(target)
        except ValueError:
            pass
        image.colorspace_settings.name = picture.image.colorspace_settings.name
        image.alpha_mode = picture.image.alpha_mode
        for place in picture.places:
            if place[0] == "slot":
                _kind, material, group, slot = place
                held = dict(material.get(group) or {})
                held[slot] = image
                material[group] = held
                material.update_tag()
            else:
                _kind, material, node_name = place
                material.node_tree.nodes[node_name].image = image
        LOG.info("laid %s out again for the new layout as %s", picture.image.name,
                 os.path.basename(target))
    return len(made)
