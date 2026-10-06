# -*- coding: utf-8 -*-
"""Swapping the surface under the open project, and keeping every layer.

Painter's project API accepts geometry only as a file path -- ``project.create``
and ``project.reload_mesh`` both take one -- which is why the arena hands Painter
a real path to the FBX Blender wrote.

**Every layer stays.** A reload matches the project's Texture Sets to the
incoming materials by name, keeps every one that matches with its whole stack,
and drops the rest. So before anything moves, every Texture Set that has layers
must have a material in the payload that paints into it; one that does not stops
the swap and is named, and the answer is to bind it -- never to let the reload
take it.

**What a layer depends on, besides its UVs, is the frame.** Painter places every
3D projection -- tri-planar, planar, spherical, warp -- relative to the box the
project's surface first came in, and re-projects strokes from where they were on
the old surface to where that is on the new one. Reloading with strokes
preserved keeps that box; reloading without does not, and every projection and
every mask painted by polygon then lands somewhere else, on every Texture Set,
even when the new surface differs by a single triangle far away. Painter will
only preserve strokes across a surface in the same units and scale, so the
surface has to arrive in the frame the project already has. That frame is stored
on the project the moment the bridge starts it, published with this side's
presence, and every surface Blender sends is written into it.

A project the bridge did not start has no frame anybody knows, and a surface
sent into it would move everything painted in 3D. It is refused, by name, until
its frame is measured and written.

What preserving cannot carry is a polygon selection on faces that changed: Painter
records it against the triangles of the Texture Set, so a face that moved to
another Texture Set, or a quad re-triangulated, takes its part of the selection
with it. That is Painter's own limit and the same with or without the bridge.

Everything here is asynchronous and refuses while Painter is busy, so each step is
queued behind ``execute_when_not_busy`` and reports through a callback.
"""

from __future__ import annotations

import substance_painter.layerstack
import substance_painter.project
import substance_painter.textureset

from ...Kernel import layout as layout_module
from ...Kernel import record as record_module
from ...Kernel.log import logger

from . import layout_state, project_facts

LOG = logger("painter.mesh")

FRAME_KEY = "frame"


class MeshIngestError(RuntimeError):
    """A mesh generation that cannot be applied to this project."""


#: Texture Sets somebody said may go on the next swap, by name. The only way a
#: Texture Set with layers is ever dropped: by name, by hand, for one swap.
_allowed_drops = set()
#: The frame a project being created from a payload will live in, and the record of
#: the surface it came with, whose chart tables and fingerprints are written onto it
#: once it is open.
_frame_of_new_project = [None]
_surface_of_new_project = [None]


def allow_dropping(name, allowed):
    if allowed:
        _allowed_drops.add(name)
    else:
        _allowed_drops.discard(name)


def drop_allowed(name):
    return name in _allowed_drops


def project_frame():
    """The frame the open project's surface lives in; None if nobody knows it."""
    if not substance_painter.project.is_open():
        return None
    return project_facts.read(FRAME_KEY)


def write_frame(frame_of_project):
    """State the open project's frame. Done once, when the frame becomes known."""
    project_facts.write(FRAME_KEY, frame_of_project)


def settle_new_project():
    """Write the frame onto a project the bridge just created. True if it did."""
    pending = _frame_of_new_project[0]
    if pending is None or not substance_painter.project.is_open():
        return False
    _frame_of_new_project[0] = None
    write_frame(pending)
    surface = _surface_of_new_project[0] or {}
    layout_state.adopt(surface.get("layouts") or {}, surface.get("fingerprints") or {})
    _surface_of_new_project[0] = None
    LOG.info("the new project lives in frame %s", pending)
    return True


def layer_count(texture_set):
    """Every layer in a Texture Set's stacks, folders and their contents included."""
    total = 0
    pending = []
    for stack in texture_set.all_stacks():
        pending.extend(substance_painter.layerstack.get_root_layer_nodes(stack))
    while pending:
        node = pending.pop()
        total += 1
        children = getattr(node, "sub_layers", None)
        if callable(children):
            pending.extend(children())
    return total


def incoming_texture_sets(record):
    """The Texture Set names a mesh generation paints into."""
    return {name for entry in record.get("scene", []) for name in entry.get("texture_sets", {})}


def would_lose(record):
    """Texture Sets with layers that nothing in this payload paints into."""
    incoming = incoming_texture_sets(record)
    lost = []
    for texture_set in substance_painter.textureset.all_texture_sets():
        if texture_set.name in incoming or texture_set.name in _allowed_drops:
            continue
        layers = layer_count(texture_set)
        if layers:
            lost.append((texture_set.name, layers))
    return sorted(lost)


def apply(generation, texture_resolution, on_finished=None):
    """Queue this generation's surface into Painter. Returns what it will do.

    ``on_finished`` hears the end of a swap, with what it changed of the layouts
    (``layout_state.Applied``) when it went in. A new project says it is ready the
    way every project does, with ``ProjectEditionEntered`` -- creating returns long
    before the project can be asked anything.
    """
    record = generation.record
    scene_path = generation.path(record["scene_file"])
    if not scene_path.exists():
        raise MeshIngestError("generation {0} declares {1} but it is not there".format(
            generation.number, scene_path))
    frame_of_surface = record["frame"]

    if not substance_painter.project.is_open():
        def create():
            settings = substance_painter.project.Settings(
                default_texture_resolution=texture_resolution,
                import_cameras=False)
            _frame_of_new_project[0] = frame_of_surface
            _surface_of_new_project[0] = record
            substance_painter.project.create(mesh_file_path=str(scene_path),
                                             settings=settings)
            LOG.info("mesh generation %d is becoming a new project", generation.number)

        substance_painter.project.execute_when_not_busy(create)
        return "create"

    frame_of_project = project_frame()
    if frame_of_project is None:
        raise MeshIngestError(
            "this project was not started by the bridge and its frame is unknown; a "
            "surface sent into it would move every projection and every mask painted "
            "in 3D. The mesh was not swapped")
    if not record_module.same_frame(frame_of_project, frame_of_surface):
        raise MeshIngestError(
            "the surface was written for frame {0} and this project lives in {1}; "
            "send it again now that Blender can see this project. The mesh was not "
            "swapped".format(frame_of_surface, frame_of_project))

    lost = would_lose(record)
    if lost:
        raise MeshIngestError(
            "these Texture Sets have layers and nothing in this mesh paints into them: "
            "{0}. Bind each to the Blender material that paints it, then send again; "
            "the mesh was not swapped".format(
                ", ".join("{0} ({1} layers)".format(name, layers) for name, layers in lost)))

    dropping = sorted(name for name in _allowed_drops
                      if name not in incoming_texture_sets(record))
    if dropping:
        LOG.warning("dropping %s on this swap, as asked", ", ".join(dropping))

    try:
        chosen = layout_state.plan(record, str(generation.directory))
    except layout_module.LayoutError as error:
        raise MeshIngestError("{0}. The mesh was not swapped".format(error)) from error

    def finished(status):
        _allowed_drops.clear()
        applied = None
        if status == substance_painter.project.ReloadMeshStatus.SUCCESS:
            LOG.info("mesh generation %d swapped in", generation.number)
            applied = layout_state.after_surface(chosen)
            LOG.info("layouts: %s", applied.line)
        else:
            LOG.error("Painter refused mesh generation %d (%s); its log says why",
                      generation.number, status)
        if on_finished is not None:
            on_finished(str(status), applied)

    def reload():
        layout_state.before_surface(chosen)
        settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False, preserve_strokes=True)
        substance_painter.project.reload_mesh(str(scene_path), settings, finished)

    substance_painter.project.execute_when_not_busy(reload)
    return "swap"


def describe_scene(generation):
    """A one-line summary of what the generation claims to carry."""
    scene = generation.record.get("scene", [])
    polygons = sum(count for entry in scene for count in entry.get("texture_sets", {}).values())
    return "{0} object(s), {1} Texture Set(s), {2} polygons".format(
        len(scene), len(incoming_texture_sets(generation.record)), polygons)
