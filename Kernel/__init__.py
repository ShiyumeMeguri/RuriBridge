# -*- coding: utf-8 -*-
"""Host-free core of the Ruri DCC bridge.

Nothing under this package imports ``bpy`` or ``substance_painter``: it is the
one copy of the arena, the wire contract and the GLB writer that both hosts and
the command line share. The host packages beside it are thin -- they translate
their own objects into what these modules already define, and back.
"""

from __future__ import annotations

__version__ = "1.0.0"

from . import arena, channel, glb, log, painter_host, record, sync  # noqa: F401
