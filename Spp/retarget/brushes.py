# -*- coding: utf-8 -*-
"""Brush sizes: what a size is measured against, and giving strokes the size they need after the surface changed.

A brush has one size, measured in the view, in the object or in the texture (``brushSizeSpace``). Measured in the
texture it is the same distance in the UV layout wherever the stroke is; measured in the object or the view it is the
same distance across the surface. So when the layout changes and the surface is to keep its footprint, a size
measured in the texture has to change with the layout; and when the mesh changes and the UV layout is to keep its
footprint, a size measured across the surface has to change with the mesh. The strokes of an action share one brush,
so strokes that need different sizes are split into actions of their own.
"""

from __future__ import annotations

import math

from .. import document, stream

HOLDERS = ("DataActionPaint", "DataActionEraser", "DataActionSmudge")
VIEW = 0
OBJECT = 1
TEXTURE = 2
#: Sizes are kept to within this factor: strokes whose sizes lie in the same step share an action.
STEP = 1.01


def _pieces(action, factors, uids):
    """``action`` as the actions that give each of its strokes the size ``factors`` (by ``id`` of the stroke) asks for."""
    strokes = list(action["strokes"])
    steps = [0 if id(stroke) not in factors else round(math.log(factors[id(stroke)]) / math.log(STEP)) for stroke in strokes]
    if not any(steps):
        return [action]
    runs = []
    for stroke, step in zip(strokes, steps):
        if runs and runs[-1][0] == step:
            runs[-1][1].append(stroke)
        else:
            runs.append((step, [stroke]))
    action["strokes"] = []
    copies = [document.renewed(action, uids)[0] for _run in runs[1:]]
    pieces = []
    for piece, (step, members) in zip([action] + copies, runs):
        piece["strokes"] = members
        brush = piece["brush"]
        brush.set_number("brushSize", brush.number("brushSize") * STEP ** step)
        pieces.append(piece)
    return pieces


def resize(material, factors, spaces, uids):
    """Scale the brushes of the material's actions whose size is measured in one of ``spaces``: each stroke by its
    factor in ``factors`` (``id`` of the stroke -> factor). New actions take their uids from ``uids``."""
    for items in stream.holders(material, HOLDERS):
        rebuilt = []
        for entry in items:
            brush = entry["brush"] if entry.type in HOLDERS else None
            if brush is not None and "brushSizeSpace" in brush and brush.number("brushSizeSpace") in spaces:
                rebuilt.extend(_pieces(entry, factors, uids))
            else:
                rebuilt.append(entry)
        items[:] = rebuilt
