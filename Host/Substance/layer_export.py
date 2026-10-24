# -*- coding: utf-8 -*-
"""Exporting what one layer lays, as Painter holds it: a channel with the coverage as alpha, or the mask.

Painter's exports are Texture Set wide, so a layer is exported by being the only one that lays anything while it is
(``_alone``); every channel is 32-bit, padded past the islands, and what was changed for the export is put back.
"""

from __future__ import annotations

import json

import substance_painter.colormanagement as colormanagement
import substance_painter.js
import substance_painter.layerstack as layerstack
import substance_painter.textureset as textureset

_BITS = 32
#: The name an export knows a layer's mask by.
MASK = "mask"


def save(uid, channel, path, coverage):
    """Export one layer's channel as Painter holds it, 32-bit, padded past the islands; with
    ``coverage``, the coverage as alpha. A layer's mask is exported by ``_save_mask``."""
    if channel == MASK:
        _save_mask(uid, path)
        return
    _evaluate_save(uid, channel, path, coverage)


def _evaluate_save(uid, channel, path, coverage):
    substance_painter.js.evaluate("alg.mapexport.save([{0}, {1}], {2}, {3})".format(
        uid, json.dumps(channel), json.dumps(path.replace("\\", "/")),
        json.dumps({"bitDepth": _BITS, "padding": "Infinite", "keepAlpha": coverage})))


_WHITE = colormanagement.Color(1.0, 1.0, 1.0)


def _alone(node, changed):
    """Leave ``node`` the only layer of its Texture Set that lays anything: every other layer is hidden,
    the folders it lies in and it are shown, and no effect of a content stack along its way is applied.
    What is changed goes into ``changed``, ``[(node, was visible)]``, as it happens, to put back."""
    way = set()
    current = node
    while current is not None:
        way.add(current.uid())
        current = current.get_parent()

    def show(one, shown):
        if one.is_visible() != shown:
            changed.append((one, not shown))
            one.set_visible(shown)

    def visit(layers):
        for layer in layers:
            if layer.uid() not in way:
                show(layer, False)
                continue
            show(layer, True)
            for effect in layer.content_effects():
                show(effect, False)
            if isinstance(layer, layerstack.GroupLayerNode):
                visit(layer.sub_layers())
    visit(layerstack.get_root_layer_nodes(node.get_texture_set().get_stack()))


def _save_mask(uid, path):
    """Export a layer's mask as the picture it lays on the layer: 1 where it lets the layer through.

    Painter 12 exports no layer's mask: asked for it, it says "not available" of a Texture Set without
    a Blending mask channel and writes zeros for one with, and its per-layer exports leave every mask
    out. A Texture Set's own channels do take every mask in, so the layer is made to lay white into
    the Blending mask channel -- a folder through a fill laid in it -- and the set's channel is
    exported: white where the mask lets the layer through. The channel is lent for the export and
    taken back, and with it the layer's white; so are the fill and the masks of the layers around it,
    which are not this one's. Other layers of the Texture Set lay into the lent channel too, so for
    the export the layer is the only one shown (``_alone``). Nothing else of the layer is touched:
    saying which channels a fill lays (``active_channels``) would give every channel of it a fresh
    colour, so the white is only set for the one channel, which turns it on."""
    node = layerstack.get_node_by_uid(int(uid))
    texture_set = node.get_texture_set()
    stack = texture_set.get_stack()
    channel = textureset.ChannelType.BlendingMask
    if stack.has_channel(channel):
        raise RuntimeError("{0} holds a Blending mask channel of its own, which a mask export would lay white "
                           "into".format(texture_set.name))
    helper = None
    ancestors = []
    changed = []
    stack.add_channel(channel, textureset.ChannelFormat.L16F)
    try:
        above = node.get_parent()
        while above is not None:
            if isinstance(above, layerstack.LayerNode) and above.has_mask() and above.is_mask_enabled():
                above.enable_mask(False)
                ancestors.append(above)
            above = above.get_parent()
        _alone(node, changed)
        if isinstance(node, layerstack.GroupLayerNode):
            helper = layerstack.insert_fill(layerstack.InsertPosition.inside_node(node, layerstack.NodeStack.Substack))
            helper.set_source(channel, _WHITE)
        elif isinstance(node, layerstack.FillLayerNode):
            node.set_source(channel, _WHITE)
        else:
            raise RuntimeError("{0} is a {1}, and only the mask of a fill layer or a folder can be "
                               "exported".format(node.get_name(), type(node).__name__))
        substance_painter.js.evaluate("alg.mapexport.save([{0}, \"blendingmask\"], {1}, {2})".format(
            json.dumps(texture_set.name), json.dumps(path.replace("\\", "/")),
            json.dumps({"bitDepth": _BITS, "padding": "Infinite", "keepAlpha": False})))
    finally:
        if helper is not None:
            layerstack.delete_node(helper)
        for one, shown in reversed(changed):
            one.set_visible(shown)
        for above in ancestors:
            above.enable_mask(True)
        stack.remove_channel(channel)
