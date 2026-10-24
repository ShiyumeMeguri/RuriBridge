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

NOT_CARRIED = "not carried"


def run(material, old, new, anchor, uv_reach=0.05, source=None, source_set=None, uids=None):
    """Repair ``material`` -- a ``DataMaterial`` -- in place for the surface having changed from ``old`` to ``new``.

    ``anchor`` says what stayed: ``UV`` the layout, ``SURFACE`` the mesh. ``source`` is the new mesh read with the
    UV set ``source_set`` that holds the layout the paint was made in; paint laid through the layout needs it. The
    report names what each pass did and counts, under ``not carried``, the paint bound to what changed that no pass
    moves. ``uids`` hands out the uids of what the passes add to the document.
    """
    if anchor not in PASSES:
        raise ValueError("anchor must be one of {0}, not {1!r}".format(sorted(PASSES), anchor))
    context = Context(old, new, material.text("sceneMaterialName"), uv_reach, source, source_set, uids)
    report = {"{0}.{1}".format(step.__module__.rsplit(".", 1)[-1], step.__name__): step(material, context) for step in PASSES[anchor]}
    left = Counter()
    for item in stream.walk(material):
        for description, applies in kinds.BY_ANCHOR[anchor]:
            if applies(item):
                left[description] += 1
    report[NOT_CARRIED] = dict(left)
    return report
