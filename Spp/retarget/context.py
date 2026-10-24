# -*- coding: utf-8 -*-
"""What a retarget pass is told: the two surfaces, the material, and the indexes of them it asks for."""

from __future__ import annotations

from functools import cached_property

import numpy as np

from . import geometry


#: How far apart, in the UV layout, the old layout and the new mesh's copy of it may lie.
SOURCE_TOLERANCE = 1e-5
#: How many triangles of the material are looked at to check that copy.
SOURCE_SAMPLES = 4000


class Context:
    """The surface a material was painted on, the one it is painted on now, and the lookups between them.

    ``source`` is the new mesh read with the UV set ``source_set``: the set that holds the layout the paint was
    made in, which a fill laid through the layout needs to follow the surface to a new one.
    """

    def __init__(self, old, new, material, uv_reach=0.05, source=None, source_set=None):
        self.old = old
        self.new = new
        self.material = material
        self.uv_reach = uv_reach
        self.old_mask = old.mask(material)
        self.new_mask = new.mask(material)
        self._source = source
        self._source_set = source_set

    @cached_property
    def source_set(self):
        """The UV set of the new mesh that holds the layout the paint was made in, checked to hold it."""
        if self._source is None or self._source_set is None:
            raise ValueError("the paint of {0} goes through the UV layout, which needs the UV set of the new mesh that holds the layout it was painted in".format(self.material))
        triangles = np.nonzero(self.old_mask)[0]
        triangles = triangles[np.linspace(0, len(triangles) - 1, min(len(triangles), SOURCE_SAMPLES)).astype(np.int64)]
        weights = np.full((len(triangles), 3), 1.0 / 3.0)
        found, carried = self.counterpart.carry(triangles, weights)
        lost = found < 0
        if lost.any():
            raise ValueError("{0} of {1} triangles of {2} are not in the new mesh: it is not the same surface".format(int(lost.sum()), len(triangles), self.material))
        difference = np.abs(self.old.uv(triangles, weights) - self._source.uv(found, carried)).max(axis=1)
        if difference.max() > SOURCE_TOLERANCE:
            raise ValueError("UV set {0} of the new mesh is not the layout {1} was painted in: it differs by up to {2:.6f} over {3:.1f}% of the triangles".format(
                self._source_set, self.material, float(difference.max()), 100.0 * float((difference > SOURCE_TOLERANCE).mean())))
        return self._source_set

    @cached_property
    def diagonal(self):
        return float(np.linalg.norm(self.new.positions.max(axis=0) - self.new.positions.min(axis=0)))

    @cached_property
    def space_reach(self):
        """How far from the surface a point may lie and still be taken for a point of it."""
        return 1e-3 * self.diagonal

    @cached_property
    def old_layout(self):
        return geometry.UvIndex(self.old, self.old_mask, self.uv_reach)

    @cached_property
    def new_layout(self):
        return geometry.UvIndex(self.new, self.new_mask)

    @cached_property
    def new_space(self):
        return geometry.SpaceIndex(self.new, self.new_mask, self.space_reach)

    @cached_property
    def counterpart(self):
        return geometry.Correspondence(self.old, self.new, 1e-5 * self.diagonal)
