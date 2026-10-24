# -*- coding: utf-8 -*-
"""The command line of ``Spp``: what a project holds, and changes to it, as JSON on standard output.

    python -m RuriBridge.Spp.cli materials PROJECT
    python -m RuriBridge.Spp.cli clone PROJECT SOURCE NAME [--out PATH]
    python -m RuriBridge.Spp.cli retarget PROJECT MATERIAL...|--all --anchor uv|surface --old SCENE [--source-set N] [--passes NAME...] [--out PATH]
    python -m RuriBridge.Spp.cli split PROJECT SOURCE NAME --new SCENE [--out PATH]
    python -m RuriBridge.Spp.cli carry PROJECT --old SCENE --events FILE [--uv-set N] [--out PATH]

``clone`` adds a material called NAME that is a copy of the material called SOURCE, with its stacks, layers,
brushes and strokes, to the project at PATH -- or to PROJECT itself when there is no ``--out``.

``retarget`` repairs the paint of the MATERIALs of PROJECT (``--all``: every one both surfaces have polygons of) for the surface PROJECT holds, which is not the one
the paint was made on: SCENE is that surface, a project file or a ``scene.assbin``. ``--anchor uv`` says the UV
layout stayed and the mesh changed, ``--anchor surface`` that the mesh stayed and the layout changed.
``--source-set`` is the UV set of PROJECT's mesh that holds the layout the paint was made in; fills laid through
the UV layout follow the surface to the new layout by way of it. ``--passes`` names the passes to run (``strokes``,
``hits``, ``fills``) when something else takes care of the rest; every pass of the anchor otherwise.

``carry`` repairs the paint of layers copied into a material for the faces carried into it from another one. FILE is JSON,
``[{"source": material the faces came from, "target": material the copies are in, "roots": [uid of each copied layer]}]``; SCENE
is the surface the paint was made on, a project file or a ``scene.assbin``, and the surface the faces lie on now is PROJECT's own.

``split`` adds a material called NAME that is a copy of SOURCE and takes out of it the strokes, polygon fills and layers that
cannot touch the polygons NAME has in SCENE -- the scene (a project file or a ``scene.assbin``) of the mesh the material was
split on, whose UV layout is PROJECT's own.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import materials, project


def _materials(arguments):
    with project.Project(arguments.project) as opened:
        return [info._asdict() for info in materials.describe(materials.Streams(opened))]


def _clone(arguments):
    with project.edited(arguments.project, arguments.out) as changing:
        streams = materials.Streams(changing)
        uid = materials.clone(streams, arguments.source, arguments.name)
        streams.save(changing)
    return {"cloned": arguments.source, "name": arguments.name, "uid": uid, "project": arguments.out or arguments.project}


def _scene_bytes(path):
    if path.lower().endswith(".spp"):
        with project.Project(path) as opened:
            return opened.read(project.SCENE)
    with open(path, "rb") as handle:
        return handle.read()


def _retarget(arguments):
    from .retarget import job
    if bool(arguments.materials) == arguments.all:
        raise SystemExit("name the materials to retarget, or give --all")
    old = _scene_bytes(arguments.old)
    with project.edited(arguments.project, arguments.out) as changing:
        report = job.run(changing, old, None if arguments.all else arguments.materials, arguments.anchor, arguments.uv_set, arguments.source_set,
                         arguments.passes)
    return {"anchor": arguments.anchor, "project": arguments.out or arguments.project, "report": report}


def _split(arguments):
    from .retarget import job
    new = _scene_bytes(arguments.new)
    with project.edited(arguments.project, arguments.out) as changing:
        report = job.split(changing, changing.read(project.SCENE), new, arguments.source, arguments.name, arguments.uv_set)
    return {"split": arguments.source, "name": arguments.name, "project": arguments.out or arguments.project, "report": report}


def _carry(arguments):
    from .retarget import job
    with open(arguments.events, encoding="utf-8") as handle:
        events = json.load(handle)
    old = _scene_bytes(arguments.old)
    with project.edited(arguments.project, arguments.out) as changing:
        report = job.carry(changing, old, events, arguments.uv_set)
    return {"events": events, "project": arguments.out or arguments.project, "report": report}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m RuriBridge.Spp.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("materials", help="the materials of a project")
    listing.add_argument("project")
    listing.set_defaults(run=_materials)
    cloning = commands.add_parser("clone", help="copy a material under a new name")
    cloning.add_argument("project")
    cloning.add_argument("source")
    cloning.add_argument("name")
    cloning.add_argument("--out")
    cloning.set_defaults(run=_clone)
    retargeting = commands.add_parser("retarget", help="repair paint for a surface that changed")
    retargeting.add_argument("project")
    retargeting.add_argument("materials", nargs="*")
    retargeting.add_argument("--all", action="store_true")
    retargeting.add_argument("--anchor", choices=("uv", "surface"), required=True)
    retargeting.add_argument("--old", required=True)
    retargeting.add_argument("--uv-set", type=int, default=0)
    retargeting.add_argument("--source-set", type=int)
    retargeting.add_argument("--passes", nargs="+")
    retargeting.add_argument("--out")
    retargeting.set_defaults(run=_retarget)
    splitting = commands.add_parser("split", help="copy a material under a new name and clean it for the polygons it has now")
    splitting.add_argument("project")
    splitting.add_argument("source")
    splitting.add_argument("name")
    splitting.add_argument("--new", required=True)
    splitting.add_argument("--uv-set", type=int, default=0)
    splitting.add_argument("--out")
    splitting.set_defaults(run=_split)
    carrying = commands.add_parser("carry", help="repair the paint of layers copied for faces carried into another material")
    carrying.add_argument("project")
    carrying.add_argument("--old", required=True)
    carrying.add_argument("--events", required=True)
    carrying.add_argument("--uv-set", type=int, default=0)
    carrying.add_argument("--out")
    carrying.set_defaults(run=_carry)
    arguments = parser.parse_args(argv)
    json.dump(arguments.run(arguments), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
