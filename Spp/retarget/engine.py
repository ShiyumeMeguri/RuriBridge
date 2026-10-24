# -*- coding: utf-8 -*-
"""Running a retarget: which passes an anchor calls for, over one material of a document."""

from __future__ import annotations

from collections import Counter

from .. import stream
from . import hits, kinds, strokes
from .context import Context

UV = kinds.UV
SURFACE = kinds.SURFACE

#: What each anchor repairs: the passes that move the data bound to what changed. A kind of paint is carried by
#: adding its pass here and taking its row out of ``kinds``.
PASSES = {
    UV: (strokes.follow_uv, hits.follow_uv),
    SURFACE: (strokes.follow_surface,),
}

NOT_CARRIED = "not carried"


def run(material, old, new, anchor, uv_reach=0.05):
    """Repair ``material`` -- a ``DataMaterial`` -- in place for the surface having changed from ``old`` to ``new``.

    ``anchor`` says what stayed: ``UV`` the layout, ``SURFACE`` the mesh. The report names what each pass did and
    counts, under ``not carried``, the paint bound to what changed that no pass moves.
    """
    if anchor not in PASSES:
        raise ValueError("anchor must be one of {0}, not {1!r}".format(sorted(PASSES), anchor))
    context = Context(old, new, material.text("sceneMaterialName"), uv_reach)
    report = {"{0}.{1}".format(step.__module__.rsplit(".", 1)[-1], step.__name__): step(material, context) for step in PASSES[anchor]}
    left = Counter()
    for item in stream.walk(material):
        for description, applies in kinds.BY_ANCHOR[anchor]:
            if applies(item):
                left[description] += 1
    report[NOT_CARRIED] = dict(left)
    return report
