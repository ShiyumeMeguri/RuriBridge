# -*- coding: utf-8 -*-
"""What the open project's file says of its paint, read with ``Spp`` from a copy Painter writes beside the sessions.

Painter's API shows the layer stack but no stroke in it. The strokes, paths and polygon fills are in the document of the
project file, and the copy Painter saves holds them as the open project stands.
"""

from __future__ import annotations

import os

import substance_painter.project

from ...Kernel import arena as arena_module
from ...Spp import layers, stream
from ...Spp import project as project_file

#: Where a copy of the project is written to read its document, beside the sessions.
FOLDER_NAME = "painter_documents"


def painted_layers():
    """The uids of the layers of the open project whose own content or mask holds paint."""
    folder = arena_module.default_root().parent / FOLDER_NAME
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "{0}.spp".format(os.getpid())
    try:
        substance_painter.project.save_as_copy(str(path), substance_painter.project.ProjectSaveMode.Incremental)
        with project_file.Project(str(path)) as opened:
            return layers.painted(stream.read(opened.read(project_file.DOCUMENT)))
    finally:
        path.unlink(missing_ok=True)
