# -*- coding: utf-8 -*-
"""What a retarget does not carry yet, as data: the kinds of paint bound to the UV layout, which changed, that no pass moves. The
engine counts them in a material and says so; a pass that learns a kind takes its row out."""

from __future__ import annotations

#: The key of a material's report under which the engine counts the paint no pass moves.
NOT_CARRIED = "not carried"

_VIEW_2D = 0


#: Paint that follows the layout and is not moved.
LAYOUT_BOUND = (
    ("polygon fills in the UV view with a symmetry duplicate",
     lambda item: item.type in ("DataDecalHitPunctual", "DataDecalHitRect") and item.number("viewType") == _VIEW_2D and item.number("duplicateCount") != 0),
)
