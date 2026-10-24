# -*- coding: utf-8 -*-
"""A Painter project file as named datasets whose checksums stay true.

Every dataset of a project carries the checksum of its bytes (``murmur``), and a dataset replaced here gets
its checksum replaced with it, so Painter takes the file as one it saved.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import struct

from . import hdf5, murmur

#: The attribute of each dataset holding the two 64-bit halves of its checksum, most significant byte first.
CHECKSUM = "m3_x64_128"
#: Where the document, the viewer settings and the baking settings live.
DOCUMENT = "paint/document.bin"
VIEWER_SETTINGS = "editor/viewersettings2.ini"
BAKING_SETTINGS = "baking/baking.ini"
SCENE = "paint/scene.assbin"


class Project:
    """A project file opened to read, or to read and replace its datasets."""

    def __init__(self, path, writable=False):
        self.path = path
        self._file = hdf5.File(path, writable)

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def close(self):
        self._file.close()

    def names(self):
        """The name of every dataset in the project."""
        return self._file.names()

    def read(self, name):
        """The bytes of the dataset ``name``."""
        return self._file.read(name)

    def checksum(self, name):
        """The checksum the project stores for ``name``, as ``(low, high)``."""
        return struct.unpack(">QQ", self._file.attribute(name, CHECKSUM))

    def intact(self, name):
        """Whether the bytes of ``name`` hash to the checksum stored with them."""
        return murmur.hash128(self.read(name)) == self.checksum(name)

    def write(self, name, data):
        """Replace the bytes of ``name`` and the checksum that goes with them."""
        self._file.write(name, data)
        self._file.set_attribute(name, CHECKSUM, struct.pack(">QQ", *murmur.hash128(data)))


@contextlib.contextmanager
def edited(path, destination=None):
    """A writable ``Project`` that is a copy of the one at ``path``.

    The copy becomes the file at ``destination`` -- the file at ``path`` when there is none -- when the block
    completes, and is thrown away when it does not, so a project is never left half changed.
    """
    target = destination or path
    working = target + ".editing"
    shutil.copyfile(path, working)
    changing = Project(working, writable=True)
    try:
        yield changing
        changing.close()
        os.replace(working, target)
    except BaseException:
        changing.close()
        if os.path.exists(working):
            os.remove(working)
        raise
