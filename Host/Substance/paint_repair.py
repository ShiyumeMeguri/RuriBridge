# -*- coding: utf-8 -*-
"""Repairing the paint of a project for a surface that is about to go in, or went in with copies of layers.

Painter replays every stroke and every polygon fill of the project against the surface it
holds, so a surface whose mesh changed under layouts that stayed, or whose layouts changed under a
mesh that stayed, is painted on differently unless the document says where each of them fell on
the new one. ``Spp.retarget`` rewrites the document in the project file for the surface the project is to
hold. Painter's interpreter has no numpy, so that runs in a process of its own, with the interpreter the
modelling application states in its presence, on a project Painter has closed.

A mesh that changed under layouts that stayed is repaired with canvases (``Spp.retarget.canvas``): the material
is copied with all its paint, the copy is the Texture Set that replays it on the surface it was painted on, and the
material is left reading the copy. A Texture Set like that is no Texture Set of the modelling application's
(``canvas_state``), and every surface that goes into a project that has any carries their surfaces with it,
whatever it repairs: Painter drops the Texture Sets a surface does not paint into.

A repair of the paint for a surface (``begin``) is a sequence of stages, one tick at a time, and then the
swap goes on as it always did. By what the surface changed (``record["anchor"]``) and whether the project has canvases:

* the mesh changed (``uv``): the project is saved and copied (the surface the paint was made on); closed; the process gives
  its materials canvases and writes the file of the surface with the canvases' surfaces after it, in a copy of the project's
  file; the copy takes the project's place and the project is opened; the swap goes on with that file;
* the layouts changed (``surface``): the project is saved and copied; the surface (with the canvases', if there are any) is
  swapped in once, so the project file holds it, and saved; the project is closed; the process repairs a copy of the
  file; the copy takes the project's place and the project is opened; the swap goes on against it;
* nothing is repaired and the project has canvases: the project is saved and copied, the process writes the file of the
  surface with the canvases' surfaces after it, and the swap goes on with that.

A repair of the copies faces were given (``begin_carry``) comes after the swap, once the layers are copied:
the project as it stood before the swap is kept (``keep_project``), and the stages are the same but for the
swap of the surface -- the project is saved, closed, repaired against the kept copy, and opened; the file the
repaired one replaces is kept until the project is open.

Anything that fails after the project is changed or closed puts the saved copy back, when there is one, and
opens the project, so a repair that could not be done leaves the project as it was. So does a swap that Painter
refuses after the project was repaired for it: the saved copy is kept until the swap is known to have gone in
(``commit``, ``rollback``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import substance_painter.project
import substance_painter.textureset

from ...Kernel import arena as arena_module
from ...Kernel.log import logger
from ...Spp.retarget import kinds

from . import canvas_state, project_imports

LOG = logger("painter.repair")

FOLDER_NAME = "painter_repair"
REPORT_NAME = "report.json"
COMPLAINT_NAME = "complaint.log"
EVENTS_NAME = "events.json"
#: The name the project as it stood before a swap is kept under.
KEPT_NAME = "kept.spp"
#: The file of the surface a project with canvases is sent, canvases' surfaces included.
COMBINED_NAME = "combined.fbx"
#: How long what a Painter left in its folder waits before another sweeps it away.
STALE_SECONDS = 24 * 3600.0
#: Ticks a save or a close is given to announce itself before the project is asked whether it is done.
SETTLE_TICKS = 3
#: What one run of the repair may take before it is stopped.
EDIT_DEADLINE_SECONDS = 4 * 3600.0
#: What any other stage may take.
STAGE_DEADLINE_SECONDS = 1800.0
#: How many lines of the process's complaint a refusal carries.
COMPLAINT_LINES = 8


class RepairError(RuntimeError):
    """A repair that cannot be done, and why."""


class _Repair:
    """One repair under way: where its files are, which stage it is at and what that stage has done."""

    __slots__ = ("texture_sets", "interpreter", "project", "directory", "old", "held", "surface", "edited", "commands",
                 "resume", "fail", "stages", "index", "entered", "called", "ticks", "scouted", "swapped", "opened",
                 "process", "started", "streams", "report", "line", "said", "inside", "reason")

    def __init__(self, texture_sets, interpreter, project_path, directory, old, held, surface, edited, commands, stages,
                 swapped, resume, fail):
        self.texture_sets = list(texture_sets)
        self.interpreter = interpreter
        self.project = project_path
        self.directory = directory
        self.old = old
        self.held = held
        self.surface = surface
        self.edited = edited
        self.commands = commands
        self.resume = resume
        self.fail = fail
        self.stages = stages
        self.index = 0
        self.entered = time.monotonic()
        self.called = False
        self.ticks = 0
        self.scouted = None
        self.swapped = swapped
        self.opened = False
        self.process = None
        self.started = 0.0
        self.streams = ()
        self.report = None
        self.line = ""
        self.said = ""
        self.inside = False
        self.reason = ""


_current = [None]
#: The repair whose surface was handed on to be swapped in, until the swap is known to have gone in or not.
_kept = [None]


def running():
    """Whether a repair is under way."""
    return _current[0] is not None


def _folder():
    """This Painter's folder, with the ones other Painters left more than a day ago swept away."""
    base = arena_module.default_root().parent / FOLDER_NAME
    if base.is_dir():
        for entry in base.iterdir():
            if entry.is_dir() and time.time() - entry.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
    return base / str(os.getpid())


def check(interpreter):
    """Raise ``RepairError`` unless a repair can run: the project has a file, which it works on, and the modelling application
    has stated an interpreter that exists."""
    if not substance_painter.project.file_path():
        raise RepairError("the project has no file yet, and the repair works on the file: save it once first")
    if not interpreter or not os.path.isfile(interpreter):
        raise RepairError("the modelling application has not stated an interpreter the repair can run in")


def keep_project(name=KEPT_NAME):
    """A copy of the open project as it stands, kept in this Painter's folder under ``name``: the surface the paint was made on, for
    a repair that comes after the surface was swapped in. Returns its path."""
    directory = _folder()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    substance_painter.project.save_as_copy(str(path))
    return path


def needed(record):
    """Whether a surface that asks for ``record`` to be repaired, or for nothing (None), goes through ``begin``: it does when it asks for a
    repair, and when the project has canvases, whose surfaces go in with every surface."""
    return bool(record) or bool(canvas_state.names())


def begin(generation, record, interpreter, resume, fail):
    """Take a repair in hand: ``record`` is what the surface asks to have repaired (see ``record.mesh``), or None for a surface that asks
    nothing and goes into a project that has canvases; ``generation`` is the surface. ``resume`` is called with the path of the surface
    to swap in when the project is repaired and open, ``fail`` with a line when it could not be and the project is as it was. Returns one
    line about it. Nothing is touched when it raises."""
    if running():
        raise RepairError("a repair is still running")
    anchor = record.get("anchor") if record else None
    if record and anchor not in kinds.BY_ANCHOR:
        raise RepairError("the paint can be repaired for a surface whose mesh changed under layouts that stayed ({0!r}) or whose layouts "
                          "changed under a mesh that stayed ({1!r}), not for {2!r}".format(kinds.UV, kinds.SURFACE, anchor))
    check(interpreter)
    canvases = canvas_state.names()
    if not record and not canvases:
        raise RepairError("there is nothing to repair and no canvas to carry")
    texture_sets = list(record["texture_sets"]) if record else []
    held = {texture_set.name for texture_set in canvas_state.held()}
    absent = [name for name in texture_sets if name not in held]
    if absent:
        raise RepairError("the project has no Texture Set called {0}".format(", ".join(absent)))
    directory = _folder()
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    project_path = substance_painter.project.file_path()
    old = directory / "before.spp"
    edited = directory / "repaired.spp"
    surface = directory / "surface.fbx"
    combined = directory / COMBINED_NAME
    carrying = ["canvas", str(old), "--surface", str(surface), "--surface-out", str(combined)]
    if anchor == kinds.UV:
        stages = _LAYING
        commands = {_lay: ["canvas", project_path, *texture_sets, "--surface", str(surface), "--surface-out", str(combined), "--out", str(edited)]}
    elif anchor == kinds.SURFACE:
        stages = _FORWARD_WITH_CANVASES if canvases else _FORWARD
        commands = {_edit: ["retarget", project_path, *texture_sets, "--anchor", anchor, "--old", str(old), "--out", str(edited)]}
        if record.get("passes"):
            commands[_edit] += ["--passes", *record["passes"]]
        if canvases:
            commands[_attach] = carrying
    else:
        stages = _ATTACHING
        commands = {_attach: carrying}
    repair = _Repair(texture_sets or canvases, interpreter, project_path, directory, old, old, surface, edited, commands, stages, False,
                     resume, fail)
    try:
        shutil.copyfile(generation.path(generation.record["scene_file"]), repair.surface)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _current[0] = repair
    LOG.info("repairing the paint of %s for the incoming surface", ", ".join(repair.texture_sets))
    if not record:
        return "putting the surfaces of {0} beside the incoming surface".format(", ".join(canvases))
    return "repairing the paint of {0} before the surface goes in".format(", ".join(repair.texture_sets))


def begin_carry(events, old, interpreter, resume, fail):
    """Take in hand the repair of the copies of layers that faces carried into other Texture Sets were given: ``events`` are
    ``{"source", "target", "roots"}`` -- the Texture Set the faces came from, the one the copies are in and the uids of the copies'
    root layers -- and ``old`` is the project as it stood before the surface was swapped in (``keep_project``). ``resume`` and
    ``fail`` are as for ``begin``, ``resume`` called with None. Returns one line about it. Nothing is touched when it raises."""
    if running():
        raise RepairError("a repair is still running")
    check(interpreter)
    old = Path(old)
    directory = old.parent
    project_path = substance_painter.project.file_path()
    edited = directory / "repaired.spp"
    (directory / EVENTS_NAME).write_text(json.dumps(events), encoding="utf-8")
    commands = {_edit: ["carry", project_path, "--old", str(old), "--events", str(directory / EVENTS_NAME), "--out", str(edited)]}
    repair = _Repair(sorted({event["target"] for event in events}), interpreter, project_path, directory, old,
                     directory / "held.spp", None, edited, commands, _CARRYING, False, resume, fail)
    _current[0] = repair
    LOG.info("repairing the paint of the copies in %s", ", ".join(repair.texture_sets))
    return "repairing the paint of the copies in {0}".format(", ".join(repair.texture_sets))


def _discard(repair):
    """The files the repair made are gone, and the folder with them if nothing else is in it: the project kept for the copies of faces
    (``keep_project``) lives in the same one."""
    for path in (repair.old, repair.held, repair.edited, repair.surface, repair.directory / COMBINED_NAME):
        if path is not None:
            path.unlink(missing_ok=True)
    for name in (REPORT_NAME, COMPLAINT_NAME, EVENTS_NAME):
        (repair.directory / name).unlink(missing_ok=True)
    try:
        repair.directory.rmdir()
    except OSError:
        pass


def commit():
    """The surface the repair handed on went in: what the repair kept is let go."""
    repair, _kept[0] = _kept[0], None
    if repair is not None:
        _discard(repair)


def rollback(reason, fail):
    """The surface the repair handed on did not go in: the project is put back as it was before the repair, when the repair changed it,
    and ``fail`` is called with a line about it. Returns False, when there is nothing to put back, and the surface can be tried again."""
    repair, _kept[0] = _kept[0], None
    if repair is None:
        return False
    if not repair.swapped:
        _discard(repair)
        return False
    LOG.error("the surface did not go in after its repair (%s); the project is being put back", reason)
    repair.reason = reason
    repair.fail = fail
    repair.stages = _BACKWARD
    repair.index = 0
    repair.entered = time.monotonic()
    repair.called = False
    repair.ticks = 0
    repair.opened = False
    repair.report = None
    _current[0] = repair
    return True


def on_project_ready(_event=None):
    """A project reached its editing state: the one a repair opened is ready to be asked."""
    repair = _current[0]
    if repair is not None:
        repair.opened = True


def advance(is_locked):
    """Do what the stage the repair is at can do now. Returns one line about where it stands when that moved, else an
    empty one; ``running`` is False once it is over.

    ``is_locked`` says whether an error is Painter holding the project for a save: that one is raised again and the
    same step is tried on the next tick. A step can run the application's event loop, and so the next tick: a tick
    that finds a step under way leaves it be."""
    repair = _current[0]
    if repair.inside:
        return ""
    repair.inside = True
    try:
        _step(repair, is_locked)
    finally:
        repair.inside = False
    if repair.line == repair.said:
        return ""
    repair.said = repair.line
    return repair.line


def _step(repair, is_locked):
    stage = repair.stages[repair.index]
    try:
        if stage is not _edit and time.monotonic() - repair.entered > STAGE_DEADLINE_SECONDS:
            raise RepairError("{0} took more than {1:.0f} minutes".format(_NAMES[stage], STAGE_DEADLINE_SECONDS / 60.0))
        if stage(repair):
            repair.index += 1
            repair.entered = time.monotonic()
            repair.called = False
            repair.ticks = 0
            if repair.index == len(repair.stages):
                _finish(repair)
    except RepairError as error:
        _fall_back(repair, str(error))
    except Exception as error:
        if is_locked(error):
            raise
        LOG.exception("the repair stopped: %s", error)
        _fall_back(repair, str(error))


def _finish(repair):
    """The repair is over: the files it made are gone, but for the surface and the saved copy, which the swap goes on from and may
    ask for."""
    _current[0] = None
    if repair.reason:
        _discard(repair)
        LOG.warning("the paint was not repaired: %s", repair.reason)
        repair.fail("the paint could not be repaired, and the project is as it was: {0}".format(repair.reason))
        return
    made = dict((repair.report or {}).get("canvases") or {})
    if made:
        canvas_state.remember(made.values())
    LOG.info("the paint is repaired: %s", _summary(repair.report))
    repair.line = "repaired the paint: {0}".format(_summary(repair.report))
    if repair.stages is _CARRYING:
        _discard(repair)
    else:
        _kept[0] = repair
    repair.resume(None if repair.surface is None else str(repair.surface))


def _fall_back(repair, reason):
    """Something failed: what was done to the project is undone, and the repair ends with the reason."""
    LOG.error("the repair of %s failed: %s", ", ".join(repair.texture_sets), reason)
    if repair.stages is _BACKWARD:
        _current[0] = None
        repair.fail("the paint could not be repaired, and the project could not be put back as it was ({0}); the saved "
                    "copy of it is {1}".format(reason, repair.held))
        return
    repair.reason = reason
    if repair.process is not None and repair.process.poll() is None:
        repair.process.kill()
    _close_streams(repair)
    if not repair.swapped:
        _current[0] = None
        shutil.rmtree(repair.directory, ignore_errors=True)
        repair.fail("the paint could not be repaired, and the project is as it was: {0}".format(reason))
        return
    repair.stages = _BACKWARD
    repair.index = 0
    repair.entered = time.monotonic()
    repair.called = False
    repair.ticks = 0
    repair.opened = False


def _summary(report):
    """What a repair did, in a line: the numbers the process reported for the Texture Sets it repaired."""
    if not report:
        return "nothing reported"
    if "canvases" in report:
        return "{0} canvas(es) made, {1} Texture Set(s) already read one, {2} canvas(es) carried along".format(
            len(report["canvases"]), len(report["kept"]), len(report["attached"]))
    strokes = hits = split = left = 0
    for passes in dict(report.get("report") or {}).values():
        for name, counts in passes.items():
            module = name.split(".", 1)[0]
            if name == kinds.NOT_CARRIED:
                left += sum(int(count) for count in counts.values())
            elif module == "strokes":
                strokes += int(counts.get("strokes", 0))
                split += int(counts.get("duplicates given a path of their own", 0))
            elif module == "hits":
                hits += int(counts.get("clicks", 0))
    line = "{0} stroke(s), {1} of them symmetry duplicates given a path of their own, {2} polygon fill click(s)".format(
        strokes, split, hits)
    if left:
        line += ", {0} piece(s) of paint it does not carry".format(left)
    return line


def _settled():
    return (not substance_painter.project.is_busy() and not substance_painter.project.needs_saving()
            and project_imports.settled())


def _saved(repair, copy_to=None):
    """The project saved as it is, and settled; ``copy_to`` is where a copy of the file is kept once it is: the file is copied as
    it lies, which Painter does not lock, rather than written again by Painter, which froze once when asked right after a save."""
    if not repair.called:
        if substance_painter.project.is_busy():
            return False
        substance_painter.project.save()
        repair.called = True
        repair.line = "saving the project"
        return False
    repair.ticks += 1
    if repair.ticks < SETTLE_TICKS or not _settled():
        return False
    if copy_to is not None:
        shutil.copyfile(repair.project, copy_to)
    return True


def _save_held(repair):
    return _saved(repair, repair.held)


def _scout(repair):
    """The new surface swapped in once, so that the project file holds it."""
    if not repair.called:
        if substance_painter.project.is_busy():
            return False

        def finished(status):
            repair.scouted = status

        settings = substance_painter.project.MeshReloadingSettings(import_cameras=False, preserve_strokes=True)
        repair.swapped = True
        substance_painter.project.reload_mesh(str(repair.surface), settings, finished)
        repair.called = True
        repair.line = "swapping the new surface in, to read it from the project"
        return False
    if repair.scouted is None:
        return False
    if repair.scouted != substance_painter.project.ReloadMeshStatus.SUCCESS:
        raise RepairError("Painter refused the surface ({0})".format(repair.scouted))
    return True


def _save_current(repair):
    return _saved(repair)


def _closed(repair):
    """The project closed, and the file free."""
    if not repair.called:
        if substance_painter.project.is_busy():
            return False
        repair.swapped = True
        substance_painter.project.close()
        repair.called = True
        repair.line = "closing the project"
        return False
    return not substance_painter.project.is_open()


def _run(repair, stage, deadline):
    """The command of ``stage`` run in a process of its own until it ends: True once it did, its report in ``repair.report``."""
    if repair.process is None:
        arguments = [repair.interpreter, "-m", "RuriBridge.Spp.cli", *repair.commands[stage]]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(_package_root())] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []))
        environment["PYTHONUTF8"] = "1"
        repair.streams = (open(repair.directory / REPORT_NAME, "wb"), open(repair.directory / COMPLAINT_NAME, "wb"))
        repair.process = subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=repair.streams[0], stderr=repair.streams[1], cwd=str(repair.directory),
            env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        repair.started = time.monotonic()
        LOG.info("the repair runs as process %d: %s", repair.process.pid, " ".join(arguments))
        return False
    elapsed = time.monotonic() - repair.started
    code = repair.process.poll()
    if code is None:
        if elapsed > deadline:
            repair.process.kill()
            raise RepairError("the repair took more than {0:.0f} minutes and was stopped".format(deadline / 60.0))
        repair.line = "repairing the paint, {0:.0f} s in".format(elapsed)
        return False
    repair.process = None
    _close_streams(repair)
    if code != 0:
        raise RepairError("the repair ended with code {0}: {1}".format(code, _complaint(repair)))
    repair.report = json.loads((repair.directory / REPORT_NAME).read_text(encoding="utf-8"))
    return True


def _edit(repair):
    """The process that repairs the paint, run on a copy of the project's file, until it ends."""
    return _run(repair, _edit, EDIT_DEADLINE_SECONDS)


def _lay(repair):
    """The process that gives the materials canvases, run on a copy of the project's file, until it ends: the surface to swap in is the
    file it wrote."""
    if not _run(repair, _lay, STAGE_DEADLINE_SECONDS):
        return False
    repair.surface = repair.directory / COMBINED_NAME
    return True


def _attach(repair):
    """The process that writes the file of the surface with the canvases' surfaces after it, until it ends: that is the surface to swap in."""
    if not _run(repair, _attach, STAGE_DEADLINE_SECONDS):
        return False
    repair.surface = repair.directory / COMBINED_NAME
    return True


def _package_root():
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.dirname(os.path.dirname(here))


def _close_streams(repair):
    for stream in repair.streams:
        stream.close()
    repair.streams = ()


def _complaint(repair):
    path = repair.directory / COMPLAINT_NAME
    if not path.exists():
        return "no word"
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return " | ".join(lines[-COMPLAINT_LINES:]) or "no word"


def _replaced(repair):
    """The repaired file in the project's place: written beside it first, so the project is never half a file; the file it
    replaces is kept as the held copy unless one was made before."""
    beside = repair.project + ".repairing"
    shutil.copyfile(repair.edited, beside)
    if not repair.held.exists():
        shutil.move(repair.project, str(repair.held))
    os.replace(beside, repair.project)
    repair.edited.unlink(missing_ok=True)
    repair.line = "the repaired file is in the project's place"
    return True


def _opened(repair):
    """The project at its path opened, and readable."""
    if not repair.called:
        substance_painter.project.open(repair.project)
        repair.called = True
        repair.opened = False
        repair.line = "opening the repaired project"
        return False
    if not repair.opened:
        return False
    try:
        substance_painter.textureset.all_texture_sets()
    except Exception:
        return False
    return True


def _put_back(repair):
    """The saved copy in the project's place, when there is one: what the project was before it was changed."""
    if repair.held.exists():
        beside = repair.project + ".repairing"
        shutil.copyfile(repair.held, beside)
        os.replace(beside, repair.project)
    repair.line = "the project is back as it was"
    return True


def _closed_when_open(repair):
    if not substance_painter.project.is_open():
        return True
    return _closed(repair)


_LAYING = (_save_held, _closed, _lay, _replaced, _opened)
_ATTACHING = (_save_held, _attach)
_FORWARD = (_save_held, _scout, _save_current, _closed, _edit, _replaced, _opened)
_FORWARD_WITH_CANVASES = (_save_held, _attach, _scout, _save_current, _closed, _edit, _replaced, _opened)
_CARRYING = (_save_current, _closed, _edit, _replaced, _opened)
_BACKWARD = (_closed_when_open, _put_back, _opened)
_NAMES = {_save_held: "saving the project", _scout: "swapping the new surface in", _save_current: "saving the project with it",
          _closed: "closing the project", _edit: "repairing the paint", _lay: "giving the materials canvases",
          _attach: "putting the canvases' surfaces beside the surface", _replaced: "putting the repaired file in place",
          _opened: "opening the project", _closed_when_open: "closing the project", _put_back: "putting the saved project back"}
