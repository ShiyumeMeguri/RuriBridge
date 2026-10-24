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
* ``document`` -- what the document is: materials, stacks, layers, actions, strokes.
* ``materials`` -- operations on whole materials across the streams that describe them.
* ``cli`` -- the command line, for people and for an assistant.

The retarget feature sits beside these (``Spp.retarget``), built from the same pieces and from nothing
else of the bridge.
"""
