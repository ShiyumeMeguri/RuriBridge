# -*- coding: utf-8 -*-
"""Moving what is painted onto a surface that changed, without baking it to pixels.

Paint in a project is held as data that is replayed on whatever surface the project has: a stroke is the path
it took across a camera's view, a fill is a rule, a bitmap is a picture in the UV layout, a polygon fill is the
rectangle or click that chose its polygons. When the surface changes, some of that data keeps its meaning and some
does not; which depends on what stayed.

* **Anchor the UV layout** -- the mesh moved, the UVs did not. What follows the layout (pictures, strokes
  drawn in the UV view) is right as it is; what follows the mesh (strokes and polygon fills made through a camera,
  fills laid through a frame in space) has to be made to land on the same places of the layout again.
* **Anchor the mesh** -- the UVs moved, the surface did not. What follows the mesh is right as it is; what
  follows the layout (fills laid through it, strokes and polygon fills made in the UV view, brush sizes measured in
  the texture) has to be carried to where the layout put the same surface.

A handler per kind of data does its half; ``geometry`` finds the places, ``selection`` knows how a polygon fill
chooses polygons and ``engine`` runs the handlers. ``prune`` cleans a material that was split off another. They
know the project only as a stream tree (``Spp.stream``) and two surfaces, and nothing of the bridge.
"""
