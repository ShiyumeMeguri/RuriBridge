# -*- coding: utf-8 -*-
"""Where the bridge keeps its facts about a project, inside the project.

Painter saves project metadata with the .spp, so a fact written here travels with
the layers it is about. The keys never change spelling: a project saved by one
build is opened by the next.
"""

from __future__ import annotations

import substance_painter.project

#: The metadata context every key of the bridge lives under.
METADATA_CONTEXT = "RuriBridge"


def read(key, default=None):
    """One fact of the open project, or ``default`` when it was never written."""
    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    if key not in metadata.list():
        return default
    return metadata.get(key)


def write(key, value):
    substance_painter.project.Metadata(METADATA_CONTEXT).set(key, value)
