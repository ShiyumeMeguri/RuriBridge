# -*- coding: utf-8 -*-
"""Every resource the bridge puts into a project, taken back out once nothing uses it.

Painter keeps a project resource in the .spp for as long as the project holds it, used or
not, and no save lets one go. The bridge imports textures into the project for a Texture Set
stood up from its Blender material (``material_seed``) and for an image pulled into a layer;
bytes that changed come in as a new resource and the one they replace stays behind, as does
one whose layer somebody deleted. So every import goes through here and is registered in the
project's own metadata by identity, and after every save of the project's own file each
registered resource nothing in the project uses any more is deleted, its record with it. A
resource the bridge did not import is never touched; one it did always came from Blender and
can be sent again.

Not during the save: Painter holds the project locked from the moment a save is announced
until after it says the save is done, and is not busy meanwhile, so the sweep waits for the
bridge's tick that finds the project free. And deleting does not shrink the file by itself --
an ordinary save writes only what changed and keeps the room the deleted data took -- so a
sweep that took anything out is followed at once by one full save, which writes the file at
its smallest. A copy saved elsewhere is not the project's file and starts nothing.

Painter offers no public way to delete a resource; its own native module does, and the bridge
uses that and nothing else of it.
"""

from __future__ import annotations

import os

import _substance_painter.resource as native_resource
import substance_painter.project
import substance_painter.resource

from ...Kernel.log import logger

from .mesh_ingest import METADATA_CONTEXT

LOG = logger("painter.imports")

#: Every resource the bridge imported into the project and has not taken out, by name and
#: version.
IMPORTS_KEY = "imports"

_save = {"target": None, "due": None}


def _registered():
    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    return list(metadata.get(IMPORTS_KEY) or []) if IMPORTS_KEY in metadata.list() else []


def _write(entries):
    substance_painter.project.Metadata(METADATA_CONTEXT).set(IMPORTS_KEY, entries)


def take_in(path, usage, name=None):
    """Import one file into the open project as a resource the bridge answers for."""
    resource = substance_painter.resource.import_project_resource(path, usage, name=name)
    identifier = resource.identifier()
    entry = {"name": identifier.name, "version": identifier.version}
    entries = _registered()
    if entry not in entries:
        entries.append(entry)
        _write(entries)
    return resource


def sweep():
    """Delete every registered resource nothing in the project uses. Returns how many went."""
    entries = _registered()
    if not entries:
        return 0
    used = {identifier.url() for identifier in substance_painter.resource.list_project_resources()}
    kept = []
    gone = []
    for entry in entries:
        found = substance_painter.resource.Resource.retrieve(
            substance_painter.resource.ResourceID.from_project(entry["name"], entry["version"]))
        if not found:
            continue
        if found[0].identifier().url() in used:
            kept.append(entry)
            continue
        native_resource.delete_resource(found[0].handle)
        gone.append(entry["name"])
    if len(kept) != len(entries):
        _write(kept)
    if gone:
        LOG.info("took %d resource(s) the bridge imported and nothing uses any more out of "
                 "the project: %s", len(gone), ", ".join(sorted(gone)))
    return len(gone)


def _same_file(first, second):
    if not first or not second:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _settle(target):
    """After a save of the project's own file: sweep, and write it once more in full when the
    sweep took anything out."""
    if not substance_painter.project.is_open():
        return
    if not _same_file(target, substance_painter.project.file_path()):
        return
    if sweep():
        LOG.info("writing the project once more in full, at its smallest")
        substance_painter.project.save(substance_painter.project.ProjectSaveMode.Full)


def before_save(event):
    """Note which file this save writes."""
    _save["target"] = event.file_path


def after_save(_event):
    """The save is done; settle it on the first tick that finds the project free."""
    _save["due"] = _save["target"]
    _save["target"] = None


def settle_due(is_locked):
    """On the bridge's tick: settle the last save once it has let go of the project. While the
    project is still locked, it stays due for the next tick."""
    target = _save["due"]
    if target is None or substance_painter.project.is_busy():
        return
    try:
        _settle(target)
    except Exception as error:
        if is_locked(error):
            return
        _save["due"] = None
        LOG.error("could not take unused imports out of the project after the save: %s", error)
        return
    _save["due"] = None


def forget():
    """The project closed or another opened: no save of it is in flight or due."""
    _save["target"] = None
    _save["due"] = None
