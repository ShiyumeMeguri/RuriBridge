# -*- coding: utf-8 -*-
"""What a retarget pass is told: the two surfaces, the material, and the indexes of them it asks for."""

from __future__ import annotations

from functools import cached_property

import numpy as np

from . import geometry


class Context:
    """The surface a material was painted on, the one it is painted on now, and the lookups between them."""

    def __init__(self, old, new, material, uv_reach=0.05):
        self.old = old
        self.new = new
        self.material = material
        self.uv_reach = uv_reach
        self.old_mask = old.mask(material)
        self.new_mask = new.mask(material)

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
