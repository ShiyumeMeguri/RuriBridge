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
where everything addressed through a chart has to read from now (``rebind``); a
retarget makes the same check before it changes anything (``retargeted``).
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


def _wanted(table, member, index, following):
    """The chart a fill reading UV set ``index`` needs to find in one Texture Set."""
    return table["layout"] if member in following else chart_at(table, index)


def retargeted(table, source_layer, target_layer, readers, tables, texture_set):
    """The table after a retarget swaps ``source_layer``'s coordinates with
    ``target_layer``'s on this Texture Set's faces, with every chart-addressed fill
    still finding the coordinates it reads.

    After the swap the source layer -- set 0 -- holds what the target layer held: a
    chart the table already names when the target was one of its extras, else a new
    one; the target holds the layout the Texture Set had; every other layer the table
    names keeps what it held. A UV set the table declares stays pointed at its layer and
    so holds whatever that layer holds now.

    ``readers`` are the fills showing this Texture Set, ``[(uid, index, members,
    following)]``, and ``tables`` the tables the texturing side applies (charts only),
    by Texture Set. One fill reads one UV set in every Texture Set showing it, so the
    others pin which ones it may read: where none of those holds what the fill reads
    here, the lowest one nothing else here needs is pointed at the layer that holds it
    now -- most constrained fills first. The result is checked with ``rebind``, the very
    check the texturing side makes when the surface arrives, so a change it would refuse
    is refused here, before anything moves; ``LayoutError`` names the fills.
    """
    table = normalized(table)
    holding = {source_layer: table["layout"]}
    for entry in table["extra"].values():
        holding[entry["layer"]] = entry["chart"]
    layout = holding[target_layer] if target_layer in holding else new_chart()
    holding[target_layer] = table["layout"]
    holding[source_layer] = layout
    pointers = {int(index): entry["layer"] for index, entry in table["extra"].items()}

    def desired_table():
        return {"layout": layout, "extra": {str(index): holding[layer] for index, layer in pointers.items()}}

    current = charts_only(table)
    others = {name: charts(value) for name, value in dict(tables).items() if name != texture_set}
    needs = []
    for uid, index, members, following in readers:
        wanted = layout if texture_set in following else chart_at(current, index)
        allowed = [one for one in range(MAX_UV_SETS)
                   if all(chart_at(others.get(member) or empty(), one)
                          == _wanted(others.get(member) or empty(), member, index, following)
                          for member in members if member != texture_set)]
        needs.append((uid, wanted, allowed))
    claimed = {}
    stranded = []
    for uid, wanted, allowed in sorted(needs, key=lambda need: (len(need[2]), need[0])):
        desired = desired_table()
        fitting = [one for one in allowed if chart_at(desired, one) == wanted
                   and claimed.get(one, wanted) == wanted]
        if fitting:
            claimed.setdefault(fitting[0], wanted)
            continue
        layer = next((name for name, chart in sorted(holding.items()) if chart == wanted), None)
        free = [one for one in allowed if one > 0 and one not in claimed]
        if layer is None or not free:
            stranded.append(uid)
            continue
        pointers[free[0]] = layer
        claimed[free[0]] = wanted
    if stranded:
        raise LayoutError("no UV set of the new surface can hold what layer(s) {0} read for every "
                          "Texture Set showing them".format(", ".join(str(uid) for uid in sorted(stranded))))
    result = {"layout": layout, "extra": {str(index): {"layer": layer, "chart": holding[layer]}
                                          for index, layer in sorted(pointers.items())}}
    applied = dict(others)
    applied[texture_set] = current
    desired = dict(others)
    desired[texture_set] = charts_only(result)
    rebind(readers, applied, desired, uv_set_count(desired.values()))
    return result


def rebind(fills, applied, desired, count):
    """Where every chart-addressed fill reads from once ``desired`` is in place.

    ``fills`` is ``[(uid, index, members, following)]``: the UV set a fill reads now,
    the Texture Sets that show it -- its own, and every one holding an instance of it
    -- and those of them it follows the layout of: a picture that is a Texture Set's
    own mesh map is that Texture Set's mesh map wherever it is shown there, and the
    mesh maps are laid out in the layout. ``applied`` and ``desired`` are tables by
    Texture Set (charts only); one missing from ``applied`` was never retargeted.
    Returns ``{uid: new index}`` for every fill that moves: to set 0 wherever set 0 holds
    what it reads -- read one to one, the way it was laid -- else it stays where it can,
    else to the lowest UV set that serves it. A fill that no index of the new surface can
    serve -- the chart it reads is gone for one of its Texture Sets, or its Texture Sets
    need it at different indices -- is a ``LayoutError`` naming them all, raised before
    anything moves.
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
        target = 0 if 0 in candidates else index if index in candidates else min(candidates)
        if target != index:
            moves[uid] = target
    if stranded:
        raise LayoutError("; ".join(
            "layer {0} reads UV set {1} and no UV set of the new surface holds those "
            "coordinates for {2}".format(uid, index, ", ".join(members))
            for uid, index, members in stranded))
    return moves
