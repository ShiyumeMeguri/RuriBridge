# -*- coding: utf-8 -*-
"""Which of this document's UV layers hold each Texture Set's charts.

The table (see ``Kernel.layout``) is written on every material that paints into the
Texture Set: a material is the one datablock both the faces and the Texture Set
name are reached from, and several materials painting one Texture Set are one
surface over there, so they carry one table between them. It is content -- it says
which coordinates the texturing project's layers are laid out in -- and is saved
with the document. Undoing a retarget undoes it with the coordinates it describes.
"""

from __future__ import annotations

from ...Kernel import layout as layout_module

#: A key written into .blend files, so it never changes spelling.
LAYOUT_PROPERTY = "ruri_bridge_layout"


def table_of(material):
    """One material's table; a material that never took part in a retarget has the
    empty one."""
    stored = material.get(LAYOUT_PROPERTY)
    if stored is None:
        return layout_module.empty()
    return layout_module.normalized({"layout": stored.get("layout"),
                                     "extra": {str(index): dict(entry) for index, entry
                                               in dict(stored.get("extra") or {}).items()}})


def table_of_texture_set(texture_set, materials):
    """The table the materials painting one Texture Set state together."""
    tables = {material.name: table_of(material) for material in materials}
    distinct = {repr(sorted(table["extra"].items())) + table["layout"] for table in tables.values()}
    if len(distinct) > 1:
        raise RuntimeError(
            "the materials painting {0} state different UV layouts ({1}); they are one "
            "surface in Painter and have to state one".format(texture_set, ", ".join(sorted(tables))))
    return next(iter(tables.values())) if tables else layout_module.empty()


def write(materials, table):
    """Make every material painting one Texture Set state this table."""
    table = layout_module.normalized(table)
    for material in materials:
        if table == layout_module.empty():
            if LAYOUT_PROPERTY in material.keys():
                del material[LAYOUT_PROPERTY]
            continue
        material[LAYOUT_PROPERTY] = {"layout": table["layout"], "extra": table["extra"]}


def layers_of(table, render_layer, count):
    """The UV layer each of ``count`` UV sets is read from for this Texture Set."""
    return [render_layer if str(index) not in table["extra"] else table["extra"][str(index)]["layer"]
            for index in range(count)]
