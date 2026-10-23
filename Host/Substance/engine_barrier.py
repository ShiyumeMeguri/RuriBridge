# -*- coding: utf-8 -*-
"""Waiting for Painter's engine to catch up with what was written to a stack.

Every write to a stack makes the engine compute it again, in the background. The stacks of a
whole model written at once are computed at once, and Painter keeps some of those results
as every channel of the stack at its default value, without a word; nothing writes the stack
again to have it computed properly, so an export reads the defaults. Written one Texture Set
at a time, with the engine waited for in between, they come out right.

Painter has no query for whether its engine is idle (``project.is_busy`` is about loading and
saving). Its own engine tests wait the way ``settle`` does: a render of the viewport, which
does not return before the engine has computed what the viewport needs.

A stack whose layers are linked to layers of other Texture Sets can still come out of one such
wait with a channel that is not the one its layers make, which an export then writes; the engine
publishes it right as soon as one layer of the stack is touched. ``republish`` touches the
bridge's own layer, which lies under everything else in the stack.
"""

from __future__ import annotations

import os

import _substance_painter.display as native_display
import substance_painter.layerstack as layerstack

from . import held_imports

#: A render of this many pixels a side, what Painter's own engine tests ask for.
SIDE = 512
FILE_NAME = "engine_barrier.bmp"


def settle():
    """Return once the engine has computed everything written so far."""
    path = held_imports.folder() / FILE_NAME
    native_display.save_viewport(str(path), SIDE, SIDE, False)
    os.remove(path)


def republish(layer_uid):
    """Have the engine publish the textures of the stack this layer is in again, and wait for it."""
    node = layerstack.get_node_by_uid(int(layer_uid))
    shown = node.is_visible()
    node.set_visible(not shown)
    node.set_visible(shown)
    settle()
