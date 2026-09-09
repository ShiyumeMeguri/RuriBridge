# -*- coding: utf-8 -*-
"""Categorised logging for every part of the bridge.

Neither host lets a library print. Blender's console is a stream Blender owns,
and Painter routes plugin output through ``substance_painter.logging`` with its
own channel column. Both are reachable as a ``logging`` handler, so the bridge
only ever talks to ``logging`` under one root name and each host installs the
sink it can actually deliver to.
"""

from __future__ import annotations

import logging

ROOT_NAME = "ruri.bridge"


def logger(category):
    """The logger for one part of the bridge, named under the shared root."""
    return logging.getLogger(ROOT_NAME + "." + category)


def root():
    """The root every host attaches its own handler to."""
    return logging.getLogger(ROOT_NAME)


def install_stream_sink(level=logging.INFO):
    """Attach a plain stream handler, for hosts whose console is a stream."""
    target = root()
    for existing in list(target.handlers):
        if getattr(existing, "ruri_bridge_sink", False):
            target.removeHandler(existing)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    handler.ruri_bridge_sink = True
    target.addHandler(handler)
    target.setLevel(level)
    target.propagate = False
    return handler


def install_callable_sink(sink, level=logging.INFO):
    """Attach a handler that forwards (level_name, category, message) to a host.

    Painter's log takes a message and a channel name rather than a stream, so
    the sink is a callable instead of a file object.
    """
    target = root()
    for existing in list(target.handlers):
        if getattr(existing, "ruri_bridge_sink", False):
            target.removeHandler(existing)

    class _CallableHandler(logging.Handler):
        ruri_bridge_sink = True

        def emit(self, record):
            category = record.name[len(ROOT_NAME) + 1:] or "bridge"
            sink(record.levelname, category, self.format(record))

    handler = _CallableHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)
    target.setLevel(level)
    target.propagate = False
    return handler
