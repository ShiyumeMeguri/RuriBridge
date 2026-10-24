# -*- coding: utf-8 -*-
"""What a retarget does not carry yet, as data: for each anchor, the kinds of paint bound to what changed that
no pass moves. The engine counts them in a material and says so; a pass that learns a kind takes its row out."""

from __future__ import annotations

UV = "uv"
SURFACE = "surface"

_FILL = "DataActionFill"
_HITS = ("DataDecalHitPunctual", "DataDecalHitRect")
_UV_PROJECTION = 0
_VIEW_2D = 0
_VIEW_3D = 1


def _holds_3d_keys(item):
    return item.type.startswith("DataAction") and "strokes3D" in item and len(item["strokes3D"]) > 0


#: The mesh changed and the UV layout stayed: paint that follows the mesh and is not moved.
MESH_BOUND = (
    ("polygon fills chosen by a rectangle through a camera",
     lambda item: item.type == "DataDecalHitRect" and item.number("viewType") == _VIEW_3D),
    ("fills projected in space", lambda item: item.type == _FILL and item.number("projection") != _UV_PROJECTION),
    ("strokes kept as keys in space", _holds_3d_keys),
)

#: The UV layout changed and the mesh stayed: paint that follows the layout and is not moved.
LAYOUT_BOUND = (
    ("pictures (pixel layers)", lambda item: item.type == "DataSourceBitmap"),
    ("fills laid through the UV layout", lambda item: item.type == _FILL and item.number("projection") == _UV_PROJECTION),
    ("polygon fills chosen in the UV view", lambda item: item.type in _HITS and item.number("viewType") == _VIEW_2D),
)

BY_ANCHOR = {UV: MESH_BOUND, SURFACE: LAYOUT_BOUND}
