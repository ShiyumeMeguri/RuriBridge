# -*- coding: utf-8 -*-
"""Image files as stored values, both ways, with nothing in between.

Lanes cross between the two applications as the bytes they are stored as: a byte of
an sRGB channel stays that byte. So a file is read with no colour transform (Blender
told it is data before it decodes) and written directly, not through an image save,
where a view transform or colour management could touch a lane.
"""

from __future__ import annotations

import os
import struct
import zlib

import bpy
import numpy

_PNG_COLOUR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}


def read(path, size=None):
    """A file's stored values, (height, width, 4) float32, rows bottom first, and
    whether they are wider than eight bits. ``size`` resamples to (width, height)."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        image.colorspace_settings.name = "Non-Color"
        image.reload()
        if size is not None and tuple(image.size) != tuple(size):
            image.scale(int(size[0]), int(size[1]))
        width, height = image.size
        values = numpy.empty(width * height * 4, dtype=numpy.float32)
        image.pixels.foreach_get(values)
        return values.reshape(height, width, 4), bool(image.is_float)
    finally:
        bpy.data.images.remove(image)


def file_of(image):
    """The file an image datablock reads, or empty when it reads none."""
    if image is None or image.packed_file is not None or not image.filepath:
        return ""
    path = os.path.abspath(bpy.path.abspath(image.filepath, library=image.library))
    return path if os.path.isfile(path) else ""


def png(lanes, wide):
    """A PNG of these lanes, (height, width, n) in [0, 1], rows bottom first."""
    height, width, count = lanes.shape
    top_first = numpy.clip(lanes[::-1], 0.0, 1.0)
    if wide:
        stored = numpy.rint(top_first * 65535.0).astype(">u2")
    else:
        stored = numpy.rint(top_first * 255.0).astype(numpy.uint8)
    rows = stored.reshape(height, -1).view(numpy.uint8)
    raw = numpy.zeros((height, rows.shape[1] + 1), dtype=numpy.uint8)
    raw[:, 1:] = rows

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 16 if wide else 8,
                         _PNG_COLOUR_TYPES[count], 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))
