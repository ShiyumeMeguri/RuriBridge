# -*- coding: utf-8 -*-
"""The command line of ``Spp``: what a project holds, and changes to it, as JSON on standard output.

    python -m RuriBridge.Spp.cli materials PROJECT
    python -m RuriBridge.Spp.cli clone PROJECT SOURCE NAME [--out PATH]

``clone`` adds a material called NAME that is a copy of the material called SOURCE, with its stacks, layers,
brushes and strokes, to the project at PATH -- or to PROJECT itself when there is no ``--out``.
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
    arguments = parser.parse_args(argv)
    json.dump(arguments.run(arguments), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
