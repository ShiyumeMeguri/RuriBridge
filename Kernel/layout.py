# -*- coding: utf-8 -*-
"""Which UV coordinates every UV-addressed thing in a Texture Set is laid out in.

A texturing tool keeps up to eight UV sets on its surface. Set 0 is the layout the
Texture Set is painted in; any other set is a **chart** the tool can read content
through -- a fill authored for the old layout keeps its pixels and reads them
through the old coordinates instead of being resampled. Charts are the same
coordinates Blender keeps in its UV layers, so both sides need one statement of
which coordinates are which.

That statement is a table per Texture Set::

    {"layout": "<chart>", "extra": {"<index>": {"layer": "<UV layer>", "chart": "<chart>"}}}

A chart is a name for one set of coordinates of the Texture Set's faces, minted
when a retarget makes new ones; the empty name is the layout the Texture Set had
before any retarget. ``layout`` is the chart set 0 holds -- Blender's render UV
layer. ``extra`` says which further UV sets the Texture Set needs and which of
Blender's layers holds each. An index a table does not declare holds the
Texture Set's own layout, so content shared with another Texture Set (an instance
layer) that reads an index this one never declared reads this one's layout and
looks exactly as it would through set 0.

Blender owns the tables -- the coordinates are its data, and a retarget changes
both in one step -- and sends them with every surface. The texturing
side keeps the tables it last applied and, before a surface goes in, works out
where everything addressed through a chart has to read from now (``rebind``).
"""

from __future__ import annotations

import uuid

#: UV sets a texturing tool keeps on one surface. Painter's shaders see
#: ``multi_tex_coord[8]`` and its importer refuses a mesh with more.
MAX_UV_SETS = 8
#: The chart a Texture Set is laid out in before any retarget.
ORIGINAL_CHART = ""


class LayoutError(RuntimeError):
    """A layout change that cannot keep everything where it is."""


def empty():
    return {"layout": ORIGINAL_CHART, "extra": {}}


def normalized(table):
    """A table in its one canonical shape: string indices, layer and chart named."""
    if not table:
        return empty()
    extra = {}
    for index, entry in dict(table.get("extra") or {}).items():
        extra[str(int(index))] = {"layer": str(entry["layer"]), "chart": str(entry["chart"])}
    return {"layout": str(table.get("layout") or ORIGINAL_CHART), "extra": extra}


def charts_only(table):
    """What the texturing side needs of a table: the chart at each declared index."""
    table = normalized(table)
    return {"layout": table["layout"],
            "extra": {index: entry["chart"] for index, entry in table["extra"].items()}}


def charts(table):
    """A table of charts only (``charts_only``) in its one canonical shape."""
    if not table:
        return {"layout": ORIGINAL_CHART, "extra": {}}
    return {"layout": str(table.get("layout") or ORIGINAL_CHART),
            "extra": {str(int(index)): str(chart) for index, chart in dict(table.get("extra") or {}).items()}}


def chart_at(table, index):
    """The chart a UV set holds for a Texture Set: its layout unless declared otherwise.

    ``table`` is either shape -- with layers, or charts only."""
    if index == 0:
        return table["layout"]
    entry = table["extra"].get(str(index))
    if entry is None:
        return table["layout"]
    return entry if isinstance(entry, str) else entry["chart"]


def uv_set_count(tables):
    """How many UV sets a surface carries: one past the highest index any table declares."""
    highest = 0
    for table in tables:
        for index in table["extra"]:
            highest = max(highest, int(index))
    return highest + 1


def new_chart():
    return uuid.uuid4().hex


def retargeted(table, target_layer, used, taken):
    """The table after a retarget swaps the render layer's coordinates with
    ``target_layer``'s on this Texture Set's faces.

    The render layer then holds what ``target_layer`` held -- a chart the table
    already names when that layer was one of its extras, else a new one -- and
    ``target_layer`` holds the layout the Texture Set had. Everything the texturing
    side reads through that old layout must find it again: content laid out in set
    0 moves to an index holding it, and so does content shared with another Texture
    Set that reads an index this table never declared (``used``: every index the
    Texture Set's content reads, as the texturing side reports it). A new index is
    one nothing in the project uses (``taken``).
    """
    table = normalized(table)
    old = table["layout"]
    extra = dict(table["extra"])
    held = [index for index, entry in extra.items() if entry["layer"] == target_layer]
    layout = extra[held[0]]["chart"] if held else new_chart()
    for index in held:
        extra[index] = {"layer": target_layer, "chart": old}
    implicit = sorted(str(index) for index in used if int(index) > 0 and str(index) not in extra)
    for index in implicit:
        extra[index] = {"layer": target_layer, "chart": old}
    if not held and not implicit:
        reserved = {int(index) for index in extra} | {int(index) for index in taken}
        free = [index for index in range(1, MAX_UV_SETS) if index not in reserved]
        if not free:
            raise LayoutError(
                "every one of the {0} UV sets a surface can carry is in use; a Texture Set "
                "cannot keep another layout".format(MAX_UV_SETS))
        extra[str(free[0])] = {"layer": target_layer, "chart": old}
    return {"layout": layout, "extra": extra}


def rebind(fills, applied, desired, count):
    """Where every chart-addressed fill reads from once ``desired`` is in place.

    ``fills`` is ``[(uid, index, members, following)]``: the UV set a fill reads now,
    the Texture Sets that show it -- its own, and every one holding an instance of it
    -- and those of them it follows the layout of: a picture that is a Texture Set's
    own mesh map is that Texture Set's mesh map wherever it is shown there, and the
    mesh maps are laid out in the layout. ``applied`` and ``desired`` are tables by
    Texture Set (charts only); one missing from ``applied`` was never retargeted.
    Returns ``{uid: new index}`` for every fill that has to move, keeping one that can
    stay. A fill that no index of the new surface can serve -- the chart it reads is
    gone for one of its Texture Sets, or its Texture Sets need it at different indices
    -- is a ``LayoutError`` naming them all, raised before anything moves.
    """
    moves = {}
    stranded = []
    for uid, index, members, following in fills:
        candidates = set(range(count))
        considered = False
        for member in members:
            if member not in desired:
                continue
            considered = True
            if member in following:
                wanted = desired[member]["layout"]
            else:
                wanted = chart_at(applied.get(member) or empty(), index)
            candidates &= {one for one in range(count) if chart_at(desired[member], one) == wanted}
        if not considered:
            continue
        if not candidates:
            stranded.append((uid, index, sorted(members)))
            continue
        target = index if index in candidates else min(candidates)
        if target != index:
            moves[uid] = target
    if stranded:
        raise LayoutError("; ".join(
            "layer {0} reads UV set {1} and no UV set of the new surface holds those "
            "coordinates for {2}".format(uid, index, ", ".join(members))
            for uid, index, members in stranded))
    return moves
