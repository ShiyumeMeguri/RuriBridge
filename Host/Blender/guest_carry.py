# -*- coding: utf-8 -*-
"""Carrying the paint of faces that moved into another Texture Set, in the step that sends them.

Update Mesh reads, in the record ``face_ledger`` keeps, what the surface changes: Texture Sets
that take a new name, faces whose paint lives in another Texture Set's own stack than the one they
paint into now, and faces an event carried into one Texture Set that now paint into another.
Painter is asked first (``begin``): it renames, then says how the stacks are made and hands over the
mesh maps and pictures of the paint a copy cannot cast again. Its answer completes the surface
(``complete``):

* each pair of source and target becomes an event: a folder over there showing the source's stack
  on its faces (``guest_state``); which root layers come live, which are copied and which those
  faces never show is decided here, from where each covers them in the source;
* an event whose faces now paint into another Texture Set than the one holding its folder keeps its
  number and goes with them -- one material leaving a Texture Set it shared with another takes its
  faces' paint into its own: the target gets a folder of its own laying what the folder holds, as it
  lies, and the holder's root layers over it, and the holder's folder goes. The faces lie where they
  lay, so everything goes on reading what it read;
* the target's mesh maps take the moving faces' maps laid out where they lie now -- tangent normals
  carried from the frames the source's layout gave them into the ones the target's gives -- and
  none of the target's own texels changes; a Texture Set the surface makes for faces leaving
  another where they lie takes that one's maps whole, so what reads around them reads the same;
* the paint the copies stand in for is laid out where it was made, read through the coordinates it
  was made in; where it lays normals that bend as the islands turn, it is laid out where the faces
  lie now, the normals carried into the new frames;
* Painter reads tangent normals in the frames of set 0, so a picture of normals read through the
  coordinates it was made in points elsewhere where the faces' islands turn: a root layer of the
  source's own laying such a picture is copied rather than shown live, and the copy's picture is
  turned where each texel lies into the frames the faces have now. Normals no picture of which can
  turn -- computed, a tile's, or laid by a layer that stays live or is the target's own -- keep the
  directions the old islands gave them, and the line says by how much;
* the target's table gains the UV set where every face holds the coordinates its paint is laid out
  in; Painter records the events, with the folders;

and the surface goes with all of it (``surface_crossing.send``). A source left with no face goes
with that surface, its paint living on where its faces went.
"""

from __future__ import annotations

import os

import numpy

from ...Kernel import layout as layout_module
from ...Kernel import record as record_module
from ...Kernel import topic as topic_module
from ...Kernel.log import logger

from . import chart_resample, face_ledger, layout_retarget, layouts, mesh_publish, pixels, surface_crossing

LOG = logger("blender.carry")

#: The carries asked about, by the generation of the ask, for the length of one exchange.
_asked = {}
#: The least coverage that counts as a root layer covering a face: half a step of eight bits.
_COVERS = 0.5 / 255.0


class Asked:
    """A carry Blender asked Painter about: the Texture Sets renamed first, ``{old: new}``, the faces
    moving, ``{(source, target): count}``, the events moving with their faces, ``{event: (holder,
    target)}``, the sources and holders left with no face, and the Texture Sets the surface deletes
    in Painter."""

    __slots__ = ("renames", "moves", "rehomes", "emptied", "dropped")

    def __init__(self, renames, moves, rehomes, emptied, dropped):
        self.renames = renames
        self.moves = moves
        self.rehomes = rehomes
        self.emptied = emptied
        self.dropped = dropped


def survey(objects, painter, dropped=(), renames=None):
    """What the record says of the faces in scope (``face_ledger.survey``), against what Painter
    states it holds but for the Texture Sets the surface deletes there (``dropped``): faces whose
    paint lives in one start over. ``renames`` are renames asked for besides the ones the faces make.
    A project not open yet holds nothing: every face starts over in it."""
    names = [name for name in surface_crossing.project_names(painter) if name not in set(dropped)]
    if not painter.get("document"):
        return face_ledger.Survey()
    return face_ledger.survey(objects, surface_crossing.slot_texture_sets, surface_crossing.held_events(painter),
                              names, surface_crossing.render_coordinates, renames)


def waiting():
    """Whether a carry waits for Painter's answer."""
    return bool(_asked)


def describe(found):
    """What a surveyed surface changes in Painter, one line each, for a person to read first."""
    lines = ["{0} takes the name {1}, every layer kept".format(old, new) for old, new in sorted(found.renames.items())]
    lines += ["{0} face(s) take their paint from {1} into {2}".format(count, source, target)
              for (source, target), count in sorted(found.moves.items())]
    leaving = {}
    for holder, target in found.rehomes.values():
        leaving[(holder, target)] = leaving.get((holder, target), 0) + 1
    lines += ["{0} carried folder(s) of {1} go into {2} with their faces".format(count, holder, target)
              for (holder, target), count in sorted(leaving.items())]
    return lines


def begin(session, found, dropped=()):
    """Ask Painter to rename what the surface renames and how the paint of the moving faces is made;
    the surface that follows deletes ``dropped`` there. Returns one line about it."""
    targets = {}
    for source, target in sorted(found.moves):
        targets.setdefault(target, []).append(source)
    leaving = {holder for holder, _target in found.rehomes.values()}
    emptied = sorted(name for name in {source for source, _target in found.moves} | leaving
                     if not layout_retarget.wearing(mesh_publish.painted_by(name)))
    generation = session.publisher(topic_module.REQUEST).publish_record(record_module.request(
        "Blender", record_module.ASK_TO_CARRY, renames=dict(found.renames), moves=targets,
        rehomes={event: {"holder": holder, "target": target} for event, (holder, target) in found.rehomes.items()},
        emptied=emptied))
    _asked[generation.number] = Asked(dict(found.renames), dict(found.moves), dict(found.rehomes), emptied,
                                      tuple(sorted(dropped)))
    return "asked Painter to " + "; ".join(describe(found))


# -- what each layer comes as ------------------------------------------------------------------------------

def _movers(surface, source, target):
    """The triangles moving from ``source``'s own stack into ``target`` with this surface."""
    return (surface.event == 0) & (surface.source == face_ledger.digest(source)) & surface.of(target)


def _leaving(surface, event, target):
    """The triangles of an event moving with its folder into ``target``."""
    return (surface.event == int(event)) & surface.of(target)


def _covers(generation, coverage, painted):
    """Whether a root layer's pictures of where it covers its Texture Set cover any of these
    triangles, laid out where their paint is: its mask first, which shuts out most."""
    rasterized = {}

    def at(values):
        rows, columns = values.shape[:2]
        if (columns, rows) not in rasterized:
            rasterized[(columns, rows)] = chart_resample.rasterize(painted, columns, rows)[0]
        return values.reshape(-1, 4)[rasterized[(columns, rows)]]

    mask = None
    if coverage["mask"]:
        values, _wide = pixels.read(str(generation.path(coverage["mask"])))
        mask = at(values)[:, 0]
        if not len(mask) or float(mask.max()) <= _COVERS:
            return False
    for file_name in coverage["channels"].values():
        values, _wide = pixels.read(str(generation.path(file_name)))
        alpha = at(values)[:, 3]
        if mask is not None:
            alpha = alpha * mask
        if len(alpha) and float(alpha.max()) > _COVERS:
            return True
    return False


def _decide(roots, generation, painted, source, target, emptied, problems, bending):
    """How each root layer of ``source`` comes along with the faces laid out at ``painted``, top
    first: ``own`` for a layer of the target itself, which already lies on the faces in the target's
    stack -- a Texture Set showing its own layer twice computes it over and over, so it is never
    instanced into itself, and the folder goes under it instead; ``instance`` for what lives in a
    Texture Set that stays and shows no paint only the source shows; ``copy`` for the source's own
    layers holding paint -- or all its own when it goes -- that cover the faces, its own layers laying
    a picture of normals that bends where the faces' islands turn (``bending``), and its own layer of
    the Blender material; ``skip`` for the rest and every hidden root, which shows on no face. Every
    one goes on reading the layout it reads; a fill of it reading the layout is made to read where
    the faces' paint is laid out once laid in the target."""
    decided = []
    for root in roots:
        if root["home"] == target:
            carry = "own"
            if not root["visible"]:
                problems.append("{0} of {1} hides a layer of {2}, which shows on the faces once they are in {2}; "
                                "show it or delete it in Painter".format(root["name"], source, target))
        elif not root["visible"]:
            carry = "skip"
        elif root["seed"]:
            carry = "copy"
        elif root["home"] != source:
            carry = "instance"
            if root["home"] in emptied:
                problems.append("{0} of {1} shows a layer of {2}, which goes with this surface too".format(
                    root["name"], source, root["home"]))
        elif root["paint"] or source in emptied:
            carry = "copy" if _covers(generation, root["coverage"], painted) else "skip"
        elif any(not entry["untouched"] for entry in bending.get(int(root["uid"]), [])):
            carry = "copy"
        else:
            carry = "instance"
        if carry == "copy" and root["kind"] == "other":
            problems.append("{0} of {1} would have to be copied, and only folders and plain fills can be; put it "
                            "in a folder in Painter".format(root["name"], source))
        decided.append({"uid": root["uid"], "carry": carry, "reads": "painted"})
    return decided


def _decide_leaving(rehome, generation, laid, target, emptied, problems):
    """How each layer an event's folder takes along comes, top first: the holder's root layers over
    the folder as ``_decide`` takes a source's, each going on reading the layout -- the faces lie
    where they lay; and everything the folder holds, hidden or not, since the folder goes: an
    instance whose layer stays instanced again, the rest copied, each reading what it reads."""
    holder = rehome["holder"]
    over = [root for root in rehome["roots"] if not root["inside"]]
    decided = [dict(entry, reads="layout") for entry in _decide(over, generation, laid, holder, target, emptied,
                                                               problems, {})]
    lying = {int(uid) for uid in rehome["lying"]}
    for root in rehome["roots"]:
        if not root["inside"]:
            continue
        stays = int(root["shows"]) not in lying and (root["home"] != holder or holder not in emptied)
        carry = "instance" if int(root["shows"]) != int(root["uid"]) and stays else "copy"
        if carry == "copy" and root["kind"] == "other":
            problems.append("{0} in a folder of {1} would have to be copied, and only folders and plain fills can "
                            "be; put it in a folder in Painter".format(root["name"], holder))
        decided.append({"uid": root["uid"], "carry": carry, "reads": "painted"})
    return decided


# -- pictures ---------------------------------------------------------------------------------------------

class _Laid:
    """Triangles laid out where their paint is (``render``, the old layout) and where they lie now
    (``target``), the shape the retarget's pictures read."""

    __slots__ = ("render", "target")

    def __init__(self, render, target):
        self.render = render
        self.target = target


def _frozen(entries, decided, generation, laid, frames, green, beside, name):
    """Pictures of the paint the copies stand in for, ``{uid: {..., "root"}}``, and its names."""
    copied = {int(entry["uid"]) for entry in decided if entry["carry"] == "copy"}
    entries = [entry for entry in entries if int(entry["root"]) in copied]
    if not entries:
        return {}, []
    frozen, named = layout_retarget.frozen_pictures({"frozen": entries}, generation, laid, frames, green, name,
                                                    beside)
    roots = {str(entry["uid"]): int(entry["root"]) for entry in entries}
    return {uid: dict(entry, root=roots[uid]) for uid, entry in frozen.items()}, named


def _lanes(values):
    """A mesh map's lanes as few as it needs: one for an opaque grey, three for an opaque colour."""
    grey = bool((values[..., 0] == values[..., 1]).all() and (values[..., 0] == values[..., 2]).all())
    opaque = bool((values[..., 3] == 1.0).all())
    return values[..., :1] if grey and opaque else values[..., :3] if opaque else values


def _whole(record, generation, name):
    """A Texture Set's mesh maps as they are, as PNG files by usage."""
    made = {}
    for usage, entry in sorted(record["texture_sets"][name]["mesh_maps"].items()):
        values, _wide = pixels.read(str(generation.path(entry["file"])))
        lanes = _lanes(values.astype(numpy.float64))
        if lanes.min() < 0.0 or lanes.max() > 1.0:
            raise RuntimeError("the {0} mesh map of {1} holds values outside 0..1, which a PNG cannot keep".format(
                usage, name))
        made[usage] = ("{0}_{1}_whole.png".format(name, usage), pixels.png(lanes, True))
    return made


def _composite(record, generation, surface, target, units, green, problems):
    """The target's mesh maps with the faces moving in laid into them, as PNG files by usage: its own
    texels as they are, the moving faces' taken from the map of the Texture Set they come from at the
    same point of the surface -- tangent normals carried into the target's frames, labels from the
    nearest texel -- and past every island the nearest island's value. ``units`` are ``[(Texture Set,
    triangles, where its maps hold them: "painted" or "render")]``. Usages the target lacks stay out.

    A Texture Set this surface makes for faces that all leave one Texture Set where they lie in it
    takes that one's maps whole: the faces lie on the same texels, and what reads around them --
    a blur, a sharpen, a warp in UV space -- reads what it read there."""
    described = record["texture_sets"]
    if target not in described and {(name, before) for name, _chosen, before in units} == {(units[0][0], "render")}:
        return _whole(record, generation, units[0][0])
    own = surface.of(target).copy()
    for _name, chosen, _before in units:
        own &= ~chosen
    if target in described:
        usages = dict(described[target]["mesh_maps"])
    else:
        usages = {}
        for name, _chosen, _before in units:
            for usage, entry in described[name]["mesh_maps"].items():
                usages.setdefault(usage, entry)
    made = {}
    mine_at = {}
    seeds = {}
    for usage, entry in sorted(usages.items()):
        if target in described:
            base, _wide = pixels.read(str(generation.path(entry["file"])))
        else:
            width, height = max((tuple(described[name]["resolution"]) for name, _chosen, _before in units),
                                key=lambda size: size[0])
            base = numpy.zeros((height, width, 4), dtype=numpy.float32)
            base[..., 3] = 1.0
        rows, columns = base.shape[:2]
        out = base.astype(numpy.float64).reshape(-1, 4)
        if (columns, rows) not in mine_at:
            mine_at[(columns, rows)] = numpy.zeros(rows * columns, dtype=bool)
            texels, _owners, _weights = chart_resample.rasterize(surface.render[own], columns, rows)
            mine_at[(columns, rows)][texels] = True
        mine = mine_at[(columns, rows)]
        covered = mine.copy()
        clashes = 0
        taken = []
        for name, chosen, before in units:
            maps = described[name]["mesh_maps"]
            if usage not in maps:
                problems.append("{0} has no {1} mesh map, so the faces it brings into {2} take none".format(
                    name, usage, target))
                continue
            picture, _wide = pixels.read(str(generation.path(maps[usage]["file"])))
            kind = entry["kind"]
            texels, values = chart_resample.laid_at(
                picture, getattr(surface, before)[chosen], surface.render[chosen], kind,
                frames=surface.frames(chosen, before, "render") if kind == "tangent" else None,
                green=green() if kind == "tangent" else 1.0, size=(columns, rows))
            free = ~mine[texels]
            clashes += int((~free).sum())
            out[texels[free]] = values[free]
            covered[texels] = True
            taken.append(name)
        if clashes:
            problems.append("{0}: {1} texel(s) of its {2} map lie under both its own faces and faces moving in; "
                            "its own keep them".format(target, clashes, usage))
        key = (columns, rows, tuple(taken))
        if key not in seeds:
            seeds[key] = chart_resample.nearest(covered.reshape(rows, columns))
        out = chart_resample.pad(out.reshape(rows, columns, 4), covered.reshape(rows, columns), seeds[key])
        lanes = _lanes(out)
        if lanes.min() < 0.0 or lanes.max() > 1.0:
            raise RuntimeError("the {0} mesh map of {1} holds values outside 0..1, which a laid-out PNG cannot "
                               "keep".format(usage, target))
        made[usage] = ("{0}_{1}_carried.png".format(target, usage), pixels.png(lanes, True))
    return made


def _channels(record, target, names):
    """The channels the Texture Sets the faces come from have and the target lacks, with their format
    and label."""
    described = record["texture_sets"]
    held = {entry["channel"] for entry in (described.get(target) or {}).get("channels") or []}
    wanted = []
    for name in names:
        for entry in described[name]["channels"]:
            if entry["channel"] not in held:
                held.add(entry["channel"])
                wanted.append(entry)
    return wanted


def _green(record, generation, surface):
    """A function giving the project's green, read once when first needed."""
    known = []

    def green():
        if not known:
            probed = next(iter(record["convention"]), None)
            if probed is None:
                raise RuntimeError("Painter told nothing of which way its stored normals point their green")
            laid = (surface.of(probed) & (surface.source == 0)) | (surface.source == face_ledger.digest(probed))
            normal = (record["texture_sets"][probed]["mesh_maps"].get("Normal") or {}).get("file")
            known.append(layout_retarget.green_of(record["convention"][probed], normal, generation,
                                                  surface.painted[laid], probed))
        return known[0]

    return green


def _written(record, generation, green):
    """A function giving the green Painter takes a picture of normals it has never seen with, read once."""
    known = []

    def written():
        if not known:
            fresh = next(entry["fresh"] for entry in record["convention"].values() if "fresh" in entry)
            render, _wide = pixels.read(str(generation.path(fresh["render"])))
            known.append(green() * layout_retarget.green_taken(str(generation.path(fresh["picture"])), render))
        return known[0]

    return written


def _normal_picture(entry, generation):
    """A fill's picture of normals laid out in the chart it reads: its own file, or Painter's render
    of what it lays."""
    values, _wide = pixels.read(entry["file"] or str(generation.path(entry["render"])))
    return values


def _bending(record, generation, surface, movers, source):
    """The source's fills laying normals through its layout that bend where the moving faces' islands
    turn, by the root layer they lie under, ``{root: [entry]}``, and the frames the faces cross
    between."""
    frames = surface.frames(movers, "painted", "render")
    turning = frames.turning()
    found = {}
    if turning.any():
        for entry in record["normals"].get(source) or []:
            if layout_retarget.bent(_normal_picture(entry, generation), surface.painted[movers], turning):
                found.setdefault(int(entry["root"]), []).append(entry)
    return found, frames


def _turned(generation, decided, bending, frames, layout, green, written, beside, source, notes):
    """The pictures of normals of the copies' fills that bend where the faces' islands turn, turned
    where each texel lies into the frames the faces have now and kept in the source's layout, which the
    fills go on reading through the painted UV set, ``{uid: picture}``. Normals no picture of which can
    turn keep the directions the old islands gave them, each noted with the most, in degrees, they
    point off."""
    carried = {int(one["uid"]): one["carry"] for one in decided}
    turned = {}
    for root, entries in sorted(bending.items()):
        if carried.get(root) == "skip":
            continue
        for entry in entries:
            values = _normal_picture(entry, generation)
            taken = 1.0
            if entry["file"]:
                reading, _wide = pixels.read(str(generation.path(entry["reading"])))
                taken = layout_retarget.green_taken(entry["file"], reading)
            lanes = values[..., :3] if bool((values[..., 3] == 1.0).all()) else values
            if carried.get(root) == "copy" and not entry["untouched"]:
                laid = chart_resample.relaid(lanes, layout, layout, "tangent", frames, green() * taken, written())
                stem = (os.path.splitext(os.path.basename(entry["file"]))[0] if entry["file"]
                        else "{0}_{1}".format(source, entry["uid"]))
                turned[str(entry["uid"])] = beside.picture(laid, True, stem)
                continue
            notes.append("{0} of {1} lays normals that keep the directions the old islands gave them, off by up "
                         "to {2:.3g} degrees where the islands turn".format(
                             entry["name"], source,
                             chart_resample.turned_by(lanes, layout, frames, green() * taken, entry["turn"])))
    return turned


# -- completing the carry -----------------------------------------------------------------------------------

def _read_charts(tables, holder, decided, problems):
    """What stands in the way of the holder's root layers over a folder going on reading the layout in
    the target: a fill of the holder reading a chart other than its layout and its guests' UV set."""
    table = layout_module.charts(tables.get(holder))
    extra = sorted(index for index, chart in table["extra"].items() if chart != layout_module.PAINTED_CHART)
    if extra and any(entry["reads"] == "layout" and entry["carry"] != "skip" for entry in decided):
        problems.append("{0} reads layouts it had before a retarget through UV set(s) {1}, and the layers over its "
                        "carried folders go along only where it lies in its layout now; retarget {0} back "
                        "first".format(holder, ", ".join(extra)))


def complete(context, session, generation, frame_of_project, painter):
    """Carry the moving faces' paint with Painter's answer and send the surface. Returns one line."""
    record = generation.record
    asked = _asked.pop(int(record["request"]), None)
    if asked is None:
        return "Painter answered about carrying paint nothing here is waiting for"
    renamed = dict(record.get("renamed") or {})
    face_ledger.rename(renamed)
    if record.get("refused"):
        raise RuntimeError("Painter cannot carry the paint of the moving faces: {0}".format(record["refused"]))
    view = surface_crossing.renamed_view(painter, renamed)
    objects = mesh_publish.scope(context.view_layer)
    found = survey(objects, view, asked.dropped)
    if found.problems:
        raise RuntimeError("; ".join(found.problems))
    if found.renames or found.moves != asked.moves or found.rehomes != asked.rehomes:
        raise RuntimeError("faces moved between Texture Sets while Painter answered; Update Mesh again")
    tables = record["tables"]
    sources = sorted({source for source, _target in asked.moves})
    problems = []
    for reader in record["readers"]:
        for source in sorted(set(reader["members"]) & set(sources)):
            table = layout_module.charts(tables.get(source))
            if layout_module.chart_at(table, int(reader["index"])) != table["layout"]:
                problems.append("layer {0} of {1} reads a layout {1} had before a retarget, and its faces carry only "
                                "where they lie in its layout now; retarget {1} back first".format(reader["uid"], source))
    if problems:
        raise RuntimeError("; ".join(sorted(set(problems))))
    surface = surface_crossing.gather(objects, frames=True)
    green = _green(record, generation, surface)
    written = _written(record, generation, green)
    events, targets, notes, named = {}, {}, [], []
    beside = surface_crossing.Beside()
    counts = {"own": 0, "instance": 0, "copy": 0, "skip": 0}
    taken = set(surface_crossing.held_events(view))
    for target in sorted({target for _source, target in asked.moves} | {target for _holder, target
                                                                         in asked.rehomes.values()}):
        into = sorted(source for source, carried_into in asked.moves if carried_into == target)
        leaving = sorted(event for event, (_holder, carried_into) in asked.rehomes.items() if carried_into == target)
        entry = {"events": {}}
        units = []
        for source in into:
            event = face_ledger.new_event(taken | {str(one) for one in events.values()})
            events[(source, target)] = event
            movers = _movers(surface, source, target)
            units.append((source, movers, "painted"))
            bending, frames = _bending(record, generation, surface, movers, source)
            decided = _decide(record["roots"][source], generation, surface.painted[movers], source, target,
                              asked.emptied, problems, bending)
            frozen, made = _frozen(record["frozen"][source], decided, generation,
                                   _Laid(surface.painted[movers], surface.render[movers]), frames, green, beside, source)
            normals = _turned(generation, decided, bending, frames, surface.painted[movers], green, written, beside,
                              source, notes)
            named += made
            entry["events"][str(event)] = {"source": source, "holder": "", "roots": decided, "frozen": frozen,
                                           "normals": normals}
        for event in leaving:
            rehome = record["rehomes"][event]
            holder = rehome["holder"]
            movers = _leaving(surface, event, target)
            units.append((holder, movers, "render"))
            decided = _decide_leaving(rehome, generation, surface.render[movers], target, asked.emptied, problems)
            _read_charts(tables, holder, decided, problems)
            frames = surface.frames(movers, "render", "render")
            frozen, made = _frozen(rehome["frozen"], decided, generation,
                                   _Laid(surface.render[movers], surface.render[movers]), frames, green, beside, holder)
            named += made
            entry["events"][event] = {"source": surface_crossing.held_events(view)[event][1], "holder": holder,
                                      "roots": decided, "frozen": {uid: dict(one, moved=True)
                                                                   for uid, one in frozen.items()},
                                      "normals": {}}
        for one in entry["events"].values():
            for decision in one["roots"]:
                counts[decision["carry"]] += 1
        entry["channels"] = _channels(record, target, [name for name, _chosen, _before in units])
        entry["mesh_maps"] = _composite(record, generation, surface, target, units, green, notes)
        entry["resolution"] = (None if target in record["texture_sets"] else
                               list(max((tuple(record["texture_sets"][name]["resolution"]) for name, _chosen, _before
                                         in units), key=lambda size: size[0])))
        targets[target] = entry
    if problems:
        raise RuntimeError("; ".join(problems))
    resolutions = {}
    for target, entry in targets.items():
        materials = mesh_publish.painted_by(target)
        into = {source for source, carried_into in asked.moves if carried_into == target}
        reading = {}
        for event in entry["events"]:
            if event in record["rehomes"]:
                holder = record["rehomes"][event]["holder"]
                index = layout_module.painted_index(layout_module.charts(tables.get(holder)))
                reading.update({int(uid): index for uid in record["rehomes"][event]["inside"]})
        readers = [(reader["uid"], int(reader["index"]), set(reader["members"])) for reader in record["readers"]
                   if (set(reader["members"]) & into and target not in reader["following"])
                   or reading.get(int(reader["uid"]), -1) == int(reader["index"])]
        layouts.write(materials, layout_module.with_guests(
            layouts.table_of_texture_set(target, materials), face_ledger.PAINTED_UV_ATTRIBUTE, readers, tables,
            target))
        if entry["resolution"] is not None:
            resolutions[target] = tuple(entry["resolution"])
    carry = {"request": int(record["request"]), "emptied": list(asked.emptied), "targets": targets}
    sent = surface_crossing.send(session, context, frame_of_project, view, beside, carry=carry, events=events,
                                 resolutions=resolutions, dropped=asked.dropped, rehomes=asked.rehomes)
    notes = list(dict.fromkeys(notes))
    for note in notes:
        LOG.warning("%s", note)
    parts = ["{0} renamed {1}".format(old, new) for old, new in sorted(renamed.items())]
    if targets:
        parts.append("carried paint into {0}: {1} layer(s) live, {2} of the target's own laid over them, {3} copied, "
                     "{4} left out, {5} piece(s) of paint as pixels".format(
                         ", ".join(sorted(targets)), counts["instance"], counts["own"], counts["copy"], counts["skip"],
                         len(named)))
    parts.append("surface generation {0} sent".format(sent.number))
    if asked.emptied:
        parts.append("{0} keep no face and go".format(", ".join(asked.emptied)))
    return "; ".join(parts + notes)
