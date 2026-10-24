# -*- coding: utf-8 -*-
"""Fills: an image or a procedural laid over the surface through a projection.

A fill laid through the UV layout puts its content at fixed places of the layout, so a layout that moves the
surface leaves the content where it was. The UV set to UV set projection takes the content from another UV set of
the mesh and lays it on the surface by way of that set; a fill given the set that still holds the layout the paint
was made in follows the surface through any layout the mesh has now, live, with nothing baked.

* ``follow_layout`` -- the layout changed, the mesh stayed: the fills laid through the UV layout are switched to
  the UV set to UV set projection from the set of the new mesh that holds the old layout.
"""

from __future__ import annotations

from collections import Counter

from .. import stream

FILL = "DataActionFill"
UV_PROJECTION = 0
#: The projection that fits the content to each UV tile: it lays the content through the UV layout as the UV projection does.
TILE_PROJECTION = 6
UV_SET_TO_UV_SET = 9
LAYOUT_PROJECTIONS = (UV_PROJECTION, TILE_PROJECTION)


def follow_layout(material, context):
    """Lay every fill that goes through the UV layout (the UV projection and the one that fits the content to each UV tile)
    through the set that holds the layout it was painted in."""
    counts = Counter()
    for item in stream.walk(material):
        if item.type != FILL or item.number("projection") not in LAYOUT_PROJECTIONS:
            continue
        item.set_number("projection", UV_SET_TO_UV_SET)
        item.set_number("uvTransferChannel", context.source_set)
        counts["fills"] += 1
    return dict(counts)

