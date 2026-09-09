# -*- coding: utf-8 -*-
"""Taking a published mesh generation into the Painter project it belongs to.

Painter's project API accepts geometry only as a file path -- ``project.create``
and ``project.reload_mesh`` both take one, and there is no buffer-shaped entry
point anywhere in the shipped ``substance_painter`` package. That constraint is
exactly why the arena hands Painter a real path: the GLB at that path was
written by Blender into pages this process maps, so following the path is a page
lookup rather than a read from storage. Nothing is re-encoded on the way in.

Which project a mesh belongs to is answered by data on both sides rather than by
names. Blender mints an identity for the scene and sends it; Painter writes it
into the project's own metadata, which the project file carries, so a project
saved today and opened next week still knows what it paints. Everything here is
asynchronous and refuses while Painter is busy, so each step is queued behind
``execute_when_not_busy`` and reports through a callback rather than pretending
to be done when it returns.
"""

from __future__ import annotations

import os

import substance_painter.project
import substance_painter.textureset

from ...Kernel import record as record_module
from ...Kernel.log import logger

LOG = logger("painter.mesh")

METADATA_CONTEXT = "RuriBridge"
BINDING_KEY = "binding"

_pending_binding = {}
_pending_reload = {}
_pending_save = {}
_asked_for_names = False


class MeshIngestError(RuntimeError):
    """A mesh generation that cannot be applied to this project."""


def stored_binding():
    """What the open project remembers about the scene it belongs to."""
    if not substance_painter.project.is_open():
        return {}
    metadata = substance_painter.project.Metadata(METADATA_CONTEXT)
    if BINDING_KEY not in metadata.list():
        return {}
    return metadata.get(BINDING_KEY) or {}


def remember_binding(binding):
    """Write the binding into the project, where saving carries it along.

    Writing metadata marks the project as changed, so writing what is already
    there would leave Painter asking to save a project nobody edited after every
    single send.
    """
    if not binding or not substance_painter.project.is_open():
        return
    if stored_binding() == binding:
        return
    substance_painter.project.Metadata(METADATA_CONTEXT).set(BINDING_KEY, binding)
    LOG.info("this project paints scene %s from %s", binding.get("scene_identity"),
             binding.get("document") or "an unsaved file")


def names_would_be_stranded(record):
    """Whether reloading this mesh would build Texture Sets beside the painted ones.

    True in one shape only: the sender says its identities are freshly minted --
    an unsaved file, or a project nobody has bridged before -- the project is
    plainly built from the same model, and not one incoming material answers to a
    Texture Set that is already here. Reloading then paints nothing and strands
    everything, so the better move is to let the sender take the names this
    project already uses and send again.

    The vertex count is the check when there is one to check against. A project
    made by hand has never recorded one, and there names are all either side has
    -- which is the case this exists to serve.
    """
    binding = record.get("binding") or {}
    if not binding.get("scene_identity_is_new"):
        return False
    remembered = stored_binding().get("vertex_count")
    theirs = binding.get("vertex_count")
    if remembered and theirs and remembered != theirs:
        return False
    existing = {texture_set.original_name
                for texture_set in substance_painter.textureset.all_texture_sets()}
    incoming = {row.get("identity") for row in record.get("materials", [])}
    return bool(existing) and not (existing & incoming)


def forget_asking():
    """A closed project has no names to offer, so the next one may ask again."""
    global _asked_for_names
    _asked_for_names = False


def belongs_to_another_scene(binding):
    """The two identities when they disagree, or None when they do not.

    An identity the sender has just minted proves nothing -- it means the file
    holding it has not been saved since the bridge first touched it -- so that
    case adopts rather than refuses. Everything else is two documents that have
    both been through here, and a disagreement then is a real one.
    """
    theirs = (binding or {}).get("scene_identity")
    mine = stored_binding().get("scene_identity")
    if not mine or not theirs or mine == theirs:
        return None
    if (binding or {}).get("scene_identity_is_new"):
        return None
    return mine, theirs


def plan(record):
    """Decide what to do with this generation before anything moves.

    In order: does the open project belong to the scene that sent this, did the
    sender ask for a new project, and is there a saved project this scene is
    bound to. Reopening that bound project is what lets a session survive being
    closed -- creating instead would leave the painted work on disk, intact and
    unused -- and refusing to close a dirty project is what stops the same
    mistake from being made destructively.
    """
    binding = record.get("binding") or {}
    declared = record.get("intent", record_module.INTENT_AUTO)
    if substance_painter.project.is_open():
        wrong = belongs_to_another_scene(binding)
        if wrong is not None:
            raise MeshIngestError(
                "the open project paints scene {0} and this mesh comes from {1}; "
                "close that project before sending this one".format(*wrong))
        if declared == record_module.INTENT_CREATE_PROJECT:
            if substance_painter.project.needs_saving():
                raise MeshIngestError(
                    "a new project would discard unsaved work in the open one; "
                    "save it first, or send with Reload Mesh")
            return record_module.INTENT_CREATE_PROJECT, None
        return record_module.INTENT_RELOAD_MESH, None
    if declared == record_module.INTENT_CREATE_PROJECT:
        return record_module.INTENT_CREATE_PROJECT, None
    bound = binding.get("project_path")
    if bound and os.path.isfile(bound):
        return record_module.INTENT_RELOAD_MESH, bound
    return record_module.INTENT_CREATE_PROJECT, None


def apply(generation, texture_resolution, ask_for_names=None, on_finished=None):
    """Queue this generation's GLB into the project. Returns the intent used."""
    global _pending_binding
    scene_path = generation.path(generation.record.get(
        "scene_file", record_module.SCENE_FILE_NAME))
    if not scene_path.exists():
        raise MeshIngestError("generation {0} declares {1} but it is not there".format(
            generation.number, scene_path))
    intent, project_to_open = plan(generation.record)
    _pending_binding = generation.record.get("binding") or {}

    def report(status):
        LOG.info("mesh generation %d applied as %s: %s", generation.number, intent, status)
        if on_finished is not None:
            on_finished(intent, status)

    def warn_about_stranded():
        incoming = {row.get("identity") for row in generation.record.get("materials", [])}
        if not incoming - {None}:
            return
        stranded = sorted(
            texture_set.original_name
            for texture_set in substance_painter.textureset.all_texture_sets()
            if texture_set.original_name not in incoming)
        if stranded:
            LOG.warning(
                "%d Texture Set(s) keep their paint but have no material in this send, "
                "so nothing will reach them: %s. That is what an unsaved sending file "
                "looks like from here", len(stranded), ", ".join(stranded[:4]))

    def reload_now(after=None):
        global _asked_for_names
        if ask_for_names is not None and not _asked_for_names                 and names_would_be_stranded(generation.record):
            _asked_for_names = True
            LOG.info("none of the %d incoming material(s) answers to a Texture Set here, "
                     "and the sender has no memory of earlier sessions; asking it to "
                     "take the names this project already uses and send again",
                     len(generation.record.get("materials", [])))
            ask_for_names()
            return False
        warn_about_stranded()
        def finished(status):
            report(status)
            if after is not None:
                after()
        settings = substance_painter.project.MeshReloadingSettings(
            import_cameras=False, preserve_strokes=True)
        substance_painter.project.reload_mesh(str(scene_path), settings, finished)
        return True

    def run():
        if intent == record_module.INTENT_CREATE_PROJECT:
            if substance_painter.project.is_open():
                substance_painter.project.close()
            settings = substance_painter.project.Settings(
                default_texture_resolution=texture_resolution,
                import_cameras=False,
                mesh_settings=substance_painter.project.GltfSettings())
            substance_painter.project.create(mesh_file_path=str(scene_path),
                                             settings=settings)
            report("created")
        elif project_to_open is not None:
            _pending_reload["run"] = reload_now
            LOG.info("reopening the project this scene is bound to: %s", project_to_open)
            substance_painter.project.open(project_to_open)
        else:
            reload_now()

    substance_painter.project.execute_when_not_busy(run)
    return intent


def resume_after_open(on_settled):
    """Finish whatever was waiting for the project to become editable.

    Three things need an open project: the binding metadata, the mesh that asked
    for a project to be reopened, and the first save of a project the scene named
    but that is not on disk yet. Returns True when a reload really is in flight,
    which tells the caller the project is not settled and to wait for
    ``on_settled``.

    Whether it is in flight is read from the reload itself rather than assumed,
    because ``execute_when_not_busy`` runs its callback on the spot when Painter
    is idle. A reload that decides to ask for the mesh again instead of loading it
    would otherwise be reported as in flight, and the caller would go on holding
    the channel shut for a load that is never coming.
    """
    global _pending_binding
    binding, _pending_binding = _pending_binding, {}
    remembered = stored_binding().get("scene_identity")
    if remembered:
        LOG.info("this project already remembered scene %s", remembered)
    wrong = belongs_to_another_scene(binding)
    if wrong is not None:
        _pending_reload.pop("run", None)
        LOG.error("this project paints scene %s, not the %s that was sent; "
                  "nothing was reloaded into it", *wrong)
        return False
    remember_binding(binding)
    _pending_save.update(binding=binding)
    resume = _pending_reload.pop("run", None)
    if resume is None:
        return False
    in_flight = {"reloading": True}

    def run_resume():
        in_flight["reloading"] = resume(on_settled)

    substance_painter.project.execute_when_not_busy(run_resume)
    return bool(in_flight["reloading"])


def save_where_the_scene_asked():
    """Write out a project the scene named but that was not there yet.

    Without this the binding only forms when somebody remembers to save by hand,
    and a session closed before that takes its paint with it. The path is the one
    the sending side asked for, so nothing is ever written somewhere the user did
    not name, and an existing file is left alone rather than overwritten.

    Called once the project has settled rather than the moment it opens, because
    a project saved before the Texture Sets are named after Blender's materials
    keeps the identities as its display names -- and a reader coming back to that
    file later has nothing legible to match on.
    """
    binding = _pending_save.pop("binding", None)
    wanted = (binding or {}).get("project_path")
    if not wanted or substance_painter.project.file_path() or os.path.exists(wanted):
        return

    def save():
        substance_painter.project.save_as(wanted)
        LOG.info("saved the new project where the scene asked: %s", wanted)

    substance_painter.project.execute_when_not_busy(save)


def describe_scene(generation):
    """A one-line summary of what the generation claims to carry."""
    scene = generation.record.get("scene", [])
    objects = len(scene)
    primitives = sum(len(entry.get("primitives", [])) for entry in scene)
    triangles = sum(primitive.get("triangle_count", 0)
                    for entry in scene for primitive in entry.get("primitives", []))
    return "{0} object(s), {1} material split(s), {2} triangles".format(
        objects, primitives, triangles)
