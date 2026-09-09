# -*- coding: utf-8 -*-
"""RuriBridge — one package, one session, every application that can reach it.

The same folder is loaded by every host: Blender takes it as an add-on and calls
``register()``, Substance Painter takes it through a directory junction and calls
``start_plugin()``, and Cascadeur runs one command out of it. Which application
this interpreter IS gets read off the interpreter (:func:`Host.detect`).

Nothing here does any work, and nothing here imports a driver until it is asked
to: importing a driver imports that application's API, so doing it while merely
reading ``bl_info`` would make listing the add-on cost as much as running it.
"""

bl_info = {
    "name": "RuriBridge",
    "author": "ShiyumeMeguri",
    "version": (2, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > N-panel > RuriBridge",
    "description": "Zero-copy shared-memory bridge between Blender, Substance 3D "
                   "Painter and Cascadeur: what one publishes lands in pages the "
                   "others already map.",
    "category": "Import-Export",
}

from . import Host


def register():
    """Blender's entry point."""
    Host.driver().register()


def unregister():
    Host.driver().unregister()


def start_plugin():
    """Substance Painter's entry point."""
    Host.driver().start_plugin()


def close_plugin():
    Host.driver().close_plugin()
