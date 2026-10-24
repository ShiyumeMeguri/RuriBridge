# -*- coding: utf-8 -*-
"""What a retarget does not carry yet, as data: for each anchor, the kinds of paint bound to what changed that
no pass moves. The engine counts them in a material and says so; a pass that learns a kind takes its row out."""

from __future__ import annotations

UV = "uv"
SURFACE = "surface"
#: The key of a material's report under which the engine counts the paint no pass moves.
NOT_CARRIED = "not carried"

_VIEW_2D = 0


def _holds_3d_keys(item):
    return item.type.startswith("DataAction") and "strokes3D" in item and len(item["strokes3D"]) > 0


#: The mesh changed and the UV layout stayed: paint that follows the mesh and is not moved.
MESH_BOUND = (
    ("strokes kept as keys in space", _holds_3d_keys),
    ("strokes with several symmetry duplicates", lambda item: item.type == "DataStroke" and item.number("duplicateCount") > 1),
    ("clone and projection paint, whose source places are not moved",
     lambda item: item.type in ("DataActionClone", "DataActionPaintProj") and len(item["strokes"]) > 0),
)

#: The UV layout changed and the mesh stayed: paint that follows the layout and is not moved.
LAYOUT_BOUND = (
    ("polygon fills in the UV view with a symmetry duplicate",
     lambda item: item.type in ("DataDecalHitPunctual", "DataDecalHitRect") and item.number("viewType") == _VIEW_2D and item.number("duplicateCount") != 0),
)

BY_ANCHOR = {UV: MESH_BOUND, SURFACE: LAYOUT_BOUND}
