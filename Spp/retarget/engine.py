# -*- coding: utf-8 -*-
"""Running a retarget: which passes an anchor calls for, over one material of a document."""

from __future__ import annotations

from collections import Counter

from .. import stream
from . import fills, hits, kinds, strokes
from .context import Context

UV = kinds.UV
SURFACE = kinds.SURFACE

#: What each anchor repairs: the passes that move the data bound to what changed. A kind of paint is carried by
#: adding its pass here and taking its row out of ``kinds``.
PASSES = {
    UV: (strokes.follow_uv, hits.follow_uv, fills.follow_space),
    SURFACE: (strokes.follow_surface, hits.follow_surface, fills.follow_layout),
}

NOT_CARRIED = kinds.NOT_CARRIED


def _module(step):
    return step.__module__.rsplit(".", 1)[-1]


def _chosen(anchor, passes):
    """The passes of ``anchor`` called by the name of the module each lives in (``strokes``, ``hits``, ``fills``), in their
    order; all of them when none are named."""
    if passes is None:
        return PASSES[anchor]
    available = [_module(step) for step in PASSES[anchor]]
    unknown = [name for name in passes if name not in available]
    if unknown:
        raise ValueError("anchor {0} has no pass called {1}; it has {2}".format(anchor, ", ".join(unknown), ", ".join(available)))
    return tuple(step for step in PASSES[anchor] if _module(step) in passes)


def run(material, old, new, anchor, uv_reach=0.05, source=None, source_set=None, uids=None, passes=None):
    """Repair ``material`` -- a ``DataMaterial`` -- in place for the surface having changed from ``old`` to ``new``.

    ``anchor`` says what stayed: ``UV`` the layout, ``SURFACE`` the mesh. ``source`` is the new mesh read with the
    UV set ``source_set`` that holds the layout the paint was made in; paint laid through the layout needs it. The
    report names what each pass did and counts, under ``not carried``, the paint bound to what changed that no pass
    moves. ``uids`` hands out the uids of what the passes add to the document. ``passes`` names the passes to run, by
    module, when the caller leaves the rest to something else; all of the anchor's otherwise.
    """
    if anchor not in PASSES:
        raise ValueError("anchor must be one of {0}, not {1!r}".format(sorted(PASSES), anchor))
    context = Context(old, new, material.text("sceneMaterialName"), uv_reach, source, source_set, uids)
    report = {"{0}.{1}".format(_module(step), step.__name__): step(material, context) for step in _chosen(anchor, passes)}
    left = Counter()
    for item in stream.walk(material):
        for description, applies in kinds.BY_ANCHOR[anchor]:
            if applies(item):
                left[description] += 1
    report[NOT_CARRIED] = dict(left)
    return report
