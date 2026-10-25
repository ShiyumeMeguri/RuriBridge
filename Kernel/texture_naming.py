# -*- coding: utf-8 -*-
"""How a texture the bridge writes is named: one profile per generated shader, read from ``TextureNaming.json``.

A texture stands for one shader slot of one Texture Set. Its stem is the set's name with the profile's material
prefix swapped for its texture prefix, then an optional variant, then the slot's suffix: ``M_actor_x_hair_01`` and
``_SplitNormalMap`` are ``T_actor_x_hair_01_HN`` under a profile whose material prefix is ``M_``, texture prefix
``T_`` and suffix for that slot ``HN``. A set that does not start with the material prefix keeps its own name and
only takes the suffix. A shader without a profile of its own takes ``Default``, which names a texture
``<set>_<slot>``.
"""

from __future__ import annotations

import functools
import json
import os

DATA_FILE = "TextureNaming.json"
VERSION = 1
SEPARATOR = "_"
PROFILE_FIELDS = ("MaterialPrefix", "TexturePrefix", "Suffixes")


class NamingError(ValueError):
    pass


def _checked(label, profile):
    missing = [field for field in PROFILE_FIELDS if field not in profile]
    if missing:
        raise NamingError("{0} lacks {1}".format(label, ", ".join(missing)))
    suffixes = dict(profile["Suffixes"])
    if any(not suffix for suffix in suffixes.values()):
        raise NamingError("{0} gives a slot an empty suffix".format(label))
    if len(set(suffixes.values())) != len(suffixes):
        raise NamingError("{0} gives two slots the same suffix, so their textures would share a name".format(label))
    return {"MaterialPrefix": str(profile["MaterialPrefix"]), "TexturePrefix": str(profile["TexturePrefix"]), "Suffixes": suffixes}


@functools.cache
def _table():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_FILE)
    with open(path, encoding="utf-8") as stream:
        table = json.load(stream)
    if table.get("Version") != VERSION:
        raise NamingError("{0} is version {1}, this reads version {2}".format(DATA_FILE, table.get("Version"), VERSION))
    return {"Default": _checked("Default", table["Default"]),
            "Shaders": {name: _checked(name, profile) for name, profile in table["Shaders"].items()}}


def profile_of(shader):
    table = _table()
    return table["Shaders"].get(shader, table["Default"])


def stem(shader, texture_set, slot, variant=""):
    """The file stem of ``slot``'s texture in ``texture_set`` for a material running ``shader``."""
    profile = profile_of(shader)
    prefix = profile["MaterialPrefix"]
    head = profile["TexturePrefix"] + texture_set[len(prefix):] if texture_set.startswith(prefix) else texture_set
    parts = [head]
    if variant:
        parts.append(variant)
    parts.append(profile["Suffixes"].get(slot) or slot.lstrip("_"))
    return SEPARATOR.join(parts)
