# -*- coding: utf-8 -*-
"""What a retarget pass is told: the two surfaces, the material, and the indexes of them it asks for."""

from __future__ import annotations

from functools import cached_property

import numpy as np

from . import geometry, selection


#: How far apart, in the UV layout, the old layout and the new mesh's copy of it may lie.
SOURCE_TOLERANCE = 1e-5
#: How many triangles of the material are looked at to check that copy.
SOURCE_SAMPLES = 4000
#: How many triangles of the material are looked at to find the rigid motion between the surfaces.
MOTION_SAMPLES = 4000
#: How far, as a share of the model's diagonal, a polygon may lie from where a rigid motion takes it and still be on it.
MOTION_TOLERANCE = 1e-3
#: Candidate motions are fitted to the polygons around this many seed polygons, this many each.
MOTION_SEEDS = 48
MOTION_NEIGHBOURS = 12


class Context:
    """The surface a material was painted on, the one it is painted on now, and the lookups between them.

    ``source`` is the new mesh read with the UV set ``source_set``: the set that holds the layout the paint was
    made in, which a fill laid through the layout needs to follow the surface to a new one. ``uids`` hands out the
    uids of what a pass adds to the document.
    """

    def __init__(self, old, new, material, uv_reach=0.05, source=None, source_set=None, uids=None):
        self.old = old
        self.new = new
        self.material = material
        self.uv_reach = uv_reach
        self.old_mask = old.mask(material)
        self.new_mask = new.mask(material)
        self.uids = uids
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
    def motion(self):
        """The rigid motion (4x4) most of the material's polygons made from the old surface to the new, and the share of them
        that are where it takes them (to within ``MOTION_TOLERANCE`` of the diagonal): the places of the old layout found
        again in the new. Where half the surface was changed and half left, the left half's motion -- none -- is it."""
        triangles = np.nonzero(self.old_mask)[0]
        triangles = triangles[::max(1, len(triangles) // MOTION_SAMPLES)]
        weights = np.repeat(selection.CENTRE, len(triangles), axis=0)
        place = self.old.point(triangles, weights)
        new_triangle, new_weights, _offset = self.new_layout.query(self.old.uv(triangles, weights), place)
        found = new_triangle >= 0
        if not found.any():
            return np.eye(4), 0.0
        place, new_place = place[found], self.new.point(new_triangle[found], new_weights[found])
        tolerance = MOTION_TOLERANCE * self.diagonal

        def inliers(motion):
            return np.linalg.norm(place @ motion[:3, :3].T + motion[:3, 3] - new_place, axis=1) <= tolerance

        best = (float(inliers(np.eye(4)).mean()), np.eye(4))
        seeds = np.linspace(0, len(place) - 1, min(len(place), MOTION_SEEDS)).astype(np.int64)
        starts = [geometry.rigid_fit(place, new_place)]
        for seed in seeds:
            near = np.argsort(np.linalg.norm(place - place[seed], axis=1))[:MOTION_NEIGHBOURS]
            starts.append(geometry.rigid_fit(place[near], new_place[near]))
        for motion in starts:
            for _round in range(4):
                held = inliers(motion)
                if held.sum() < 3:
                    break
                motion = geometry.rigid_fit(place[held], new_place[held])
            share = float(inliers(motion).mean())
            if share > best[0] + 1e-9:
                best = (share, motion)
        return best[1], best[0]

    @cached_property
    def old_elements(self):
        return selection.elements(self.old)

    @cached_property
    def new_elements(self):
        return selection.elements(self.new)

    @cached_property
    def old_chunks(self):
        return selection.chunks(self.old)

    @cached_property
    def new_chunks(self):
        return selection.chunks(self.new)

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
    def old_layout_exact(self):
        """The old layout without a reach: a place outside every island is nowhere."""
        return geometry.UvIndex(self.old, self.old_mask)

    @cached_property
    def new_space(self):
        return geometry.SpaceIndex(self.new, self.new_mask, self.space_reach)

    @cached_property
    def counterpart(self):
        return geometry.Correspondence(self.old, self.new, 1e-5 * self.diagonal)

    @cached_property
    def counterpart_any_material(self):
        """The same, finding a triangle again when it was given another material (a material split in two)."""
        return geometry.Correspondence(self.old, self.new, 1e-5 * self.diagonal, same_material=False)
