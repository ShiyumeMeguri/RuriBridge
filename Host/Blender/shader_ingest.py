# -*- coding: utf-8 -*-
"""Painter's shader parameters, written back into the materials that speak for its
Texture Sets.

The way back from a pushed shading row: the same material speaks for a Texture Set in
both directions (``mesh_publish.speakers``), and the values land in the material's own
record through the inverse of the declaration that read them out (``write_row``). The
record is the material's content; the stack that compiled the material follows its
record, so a value written here is what the material renders with once Blender has
taken the update.

Painter's shader may be another generation of the material's, or another shader
altogether: then only the names the material holds are written, which is all
``write_row`` ever writes, and it is said.
"""

from __future__ import annotations

from ...Kernel.log import logger
from . import mesh_publish

LOG = logger("blender.shader")


def take(record, objects):
    """Write one pushed shading state into the speaking materials. Returns one line."""
    speaking = mesh_publish.speakers(objects)
    identities = record.get("identity_by_texture_set") or {}
    taken = {}
    skipped = {}
    for texture_set, values in sorted((record.get("by_texture_set") or {}).items()):
        speaker = speaking.get(texture_set)
        if speaker is None:
            skipped[texture_set] = "nothing here paints into it"
            continue
        material = speaker[0]
        _shader, identity = mesh_publish.declared_shader(material)
        if not identity:
            skipped[texture_set] = "{0} is not on a generated shader".format(material.name)
            continue
        if material.library is not None:
            skipped[texture_set] = "{0} is linked from {1}: make it local to take values".format(
                material.name, material.library.filepath)
            continue
        if material.is_runtime_data:
            skipped[texture_set] = ("{0} is this session's stand-in for a linked material, and "
                                    "nothing written into it is saved: make the library "
                                    "material local to take values".format(material.name))
            continue
        written, unheld, refused = mesh_publish.write_row(material, values)
        if written:
            material.update_tag()
        taken[texture_set] = written
        if identities.get(texture_set) != identity:
            LOG.info("%s: Painter runs another shader than %s; same-named parameters only",
                     texture_set, material.name)
        for name, why in sorted(refused.items()):
            LOG.warning("%s: %s not written: %s", material.name, name, why)
        LOG.info("%s: %d parameter(s) written into %s, %d it holds no property for",
                 texture_set, len(written), material.name, len(unheld))
    for texture_set, why in sorted(skipped.items()):
        LOG.warning("%s: shader not taken: %s", texture_set, why)
    line = "took the shader of {0} Texture Set(s) from Painter: {1} parameter(s) changed".format(
        len(taken), sum(len(names) for names in taken.values()))
    if skipped:
        line += "; skipped " + ", ".join("{0} ({1})".format(name, why)
                                         for name, why in sorted(skipped.items()))
    return line
