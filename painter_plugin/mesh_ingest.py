# -*- coding: utf-8 -*-
"""Taking a published mesh generation into the open Painter project.

Painter's project API accepts geometry only as a file path -- ``project.create``
and ``project.reload_mesh`` both take one, and there is no buffer-shaped entry
point anywhere in the shipped ``substance_painter`` package. That constraint is
exactly why the arena hands Painter a real path: the GLB at that path was
written by Blender into pages this process maps, so following the path is a page
lookup rather than a read from storage. Nothing is re-encoded on the way in.

Both entry points are asynchronous, and both refuse while Painter is busy, so
every ingest is queued behind ``execute_when_not_busy`` and reports through the
callback rather than pretending to be done when it returns.
"""

from __future__ import annotations

import substance_painter.project

from ruri_bridge import record as record_module
from ruri_bridge.log import logger

LOG = logger("painter.mesh")


class MeshIngestError(RuntimeError):
    """A mesh generation that cannot be applied to this project."""


def resolve_intent(declared):
    """Turn the declared intent into the one Painter can actually perform.

    Reloading into nothing means creating: an intent says what the sender wants
    to be looking at, and with no project open there is exactly one way to
    satisfy that. Refusing instead -- which is what this did -- loses the mesh
    entirely when a session is caught up from a reload that arrived after the
    create it was meant to follow.
    """
    if not substance_painter.project.is_open():
        return record_module.INTENT_CREATE_PROJECT
    if declared == record_module.INTENT_AUTO:
        return record_module.INTENT_RELOAD_MESH
    return declared


def apply(generation, texture_resolution, on_finished=None):
    """Queue this generation's GLB into the project. Returns the intent used."""
    scene_path = generation.path(generation.record.get(
        "scene_file", record_module.SCENE_FILE_NAME))
    if not scene_path.exists():
        raise MeshIngestError("generation {0} declares {1} but it is not there".format(
            generation.number, scene_path))
    intent = resolve_intent(generation.record.get("intent", record_module.INTENT_AUTO))

    def report(status):
        LOG.info("mesh generation %d applied as %s: %s", generation.number, intent, status)
        if on_finished is not None:
            on_finished(intent, status)

    def run():
        if intent == record_module.INTENT_CREATE_PROJECT:
            if substance_painter.project.is_open():
                substance_painter.project.close()
            settings = substance_painter.project.Settings(
                default_texture_resolution=texture_resolution,
                import_cameras=False,
                mesh_settings=substance_painter.project.GltfSettings())
            substance_painter.project.create(mesh_file_path=str(scene_path), settings=settings)
            report("created")
        else:
            settings = substance_painter.project.MeshReloadingSettings(
                import_cameras=False, preserve_strokes=True)
            substance_painter.project.reload_mesh(
                str(scene_path), settings, lambda status: report(status))

    substance_painter.project.execute_when_not_busy(run)
    return intent


def describe_scene(generation):
    """A one-line summary of what the generation claims to carry."""
    scene = generation.record.get("scene", [])
    objects = len(scene)
    primitives = sum(len(entry.get("primitives", [])) for entry in scene)
    triangles = sum(primitive.get("triangle_count", 0)
                    for entry in scene for primitive in entry.get("primitives", []))
    return "{0} object(s), {1} material split(s), {2} triangles".format(
        objects, primitives, triangles)
