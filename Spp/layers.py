# -*- coding: utf-8 -*-
"""Which layers of a document hold paint: strokes, paths or polygon fills, in the layer's own content or in its mask."""

from __future__ import annotations

from . import stream

LAYER = "DataLayer"
#: The fields of an action that hold what was painted.
PAINT = ("strokes", "strokes3D", "hits")


def _items(owner, field):
    holder = owner[field] if field in owner else None
    return [] if holder is None else [item for item in holder["items"] if item is not None]


def _holds(action):
    """Whether an action, or an action in its sub stack, holds a stroke, a path or a polygon fill."""
    if any(field in action and action[field] for field in PAINT):
        return True
    return any(_holds(child) for child in _items(action, "subStack"))


def painted(document):
    """The uids of the layers of a document (``stream.read``) whose own content or mask holds paint."""
    return {layer.number("uid") for layer in stream.walk(document.root)
            if layer.type.startswith(LAYER) and "uid" in layer
            and any(_holds(action) for field in ("actions", "maskActions") for action in _items(layer, field))}
