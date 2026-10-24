# -*- coding: utf-8 -*-
"""Substance Painter project files (``.spp``), read and written without Painter and without any library.

Host-neutral and standard library only, so Blender's Python, Painter's own interpreter and a bare
``python`` all run it. Nothing in here imports another part of the bridge, and nothing outside imports
more than a module's public functions; a module knows one thing and hands data to the next:

* ``murmur`` -- the checksum Painter keeps on every dataset.
* ``hdf5`` -- the container: named datasets of bytes, replaced in place.
* ``project`` -- a project file as named datasets whose checksums stay true.
* ``stream`` -- Painter's self-describing data stream (the document, the viewer settings, the baking
  settings) as a tree that writes back byte for byte.
* ``document`` -- what the streams share: objects with uids that others refer to.
* ``materials`` -- operations on whole materials across the streams that describe them.
* ``scene`` -- the surface the project paints on, its ``scene.assbin``.
* ``fbx`` -- the surface as the ASCII FBX the bridge hands Painter, written and read back (it uses numpy, which Blender carries).
* ``cli`` -- the command line, for people and for an assistant.

``retarget`` uses numpy too, which Blender carries and Painter does not: it moves paint onto a surface whose layout changed and gives
materials whose mesh changed canvases (``retarget.canvas``), built from ``stream``, ``scene``, ``fbx`` and numpy and from nothing else of the
bridge. ``retarget.kinds`` holds the names the others agree on and needs no numpy. ``layers`` reads which layers of a document hold paint.
"""
