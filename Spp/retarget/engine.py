# -*- coding: utf-8 -*-
"""Running a retarget: the passes of a layout that changed under a mesh that stayed, over one material of a document."""

from __future__ import annotations

from collections import Counter

from .. import stream
from . import fills, hits, kinds, strokes
from .context import Context

#: The passes that move the data bound to the layout. A kind of paint is carried by adding its pass here and taking its row out of
#: ``kinds``.
PASSES = (strokes.follow_surface, hits.follow_surface, fills.follow_layout)

NOT_CARRIED = kinds.NOT_CARRIED


def _module(step):
    return step.__module__.rsplit(".", 1)[-1]


def _chosen(passes):
    """The passes called by the name of the module each lives in (``strokes``, ``hits``, ``fills``), in their order; all of them when
    none are named."""
    if passes is None:
        return PASSES
    available = [_module(step) for step in PASSES]
    unknown = [name for name in passes if name not in available]
    if unknown:
        raise ValueError("there is no pass called {0}; there are {1}".format(", ".join(unknown), ", ".join(available)))
    return tuple(step for step in PASSES if _module(step) in passes)


def run(material, old, new, uv_reach=0.05, source=None, source_set=None, uids=None, passes=None):
    """Repair ``material`` -- a ``DataMaterial`` -- in place for the layout having changed under a mesh that stayed, from ``old`` to
    ``new``. ``source`` is the new mesh read with the UV set ``source_set`` that holds the layout the paint was made in; paint laid
    through the layout needs it. The report names what each pass did and counts, under ``not carried``, the paint bound to the layout
    that no pass moves. ``uids`` hands out the uids of what the passes add to the document. ``passes`` names the passes to run, by
    module, when the caller leaves the rest to something else; all of them otherwise.
    """
    context = Context(old, new, material.text("sceneMaterialName"), uv_reach, source, source_set, uids)
    report = {"{0}.{1}".format(_module(step), step.__name__): step(material, context) for step in _chosen(passes)}
    left = Counter()
    for item in stream.walk(material):
        for description, applies in kinds.LAYOUT_BOUND:
            if applies(item):
                left[description] += 1
    report[NOT_CARRIED] = dict(left)
    return report
