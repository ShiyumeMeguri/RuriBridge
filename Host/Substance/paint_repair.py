# -*- coding: utf-8 -*-
"""Repairing the paint of a project for a surface that is about to go in, or went in with copies of layers.

Painter replays every stroke and every polygon fill of the project against the surface it
holds, so a surface whose mesh changed under layouts that stayed, or whose layouts changed under a
mesh that stayed, is painted on differently unless the document says where each of them fell on
the new one. ``Spp.retarget`` says it: it rewrites the document in the project file for the surface
the project holds, given the one the paint was made on. Painter's interpreter has no numpy, so that
runs in a process of its own, with the interpreter the modelling application states in its presence,
on a project Painter has closed.

A repair of the paint for a surface (``begin``) is a sequence of stages, one tick at a time, and then the
swap goes on as it always did:

1. the project is saved and copied: that file is the surface the paint was made on;
2. the new surface is swapped in once, so the project file holds it, and saved;
3. the project is closed;
4. the process repairs a copy of the file;
5. the copy takes the project's place, and the project is opened;
6. the swap of the surface goes on against the repaired project.

A repair of the copies faces were given (``begin_carry``) comes after the swap, once the layers are copied:
the project as it stood before the swap is kept (``keep_project``), and the stages are the same but for the
swap of the surface -- the project is saved, closed, repaired against the kept copy, and opened; the file the
repaired one replaces is kept until the project is open.

Anything that fails after the project is changed or closed puts the saved copy back, when there is one, and
opens the project, so a repair that could not be done leaves the project as it was.
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

from . import project_imports

LOG = logger("painter.repair")

FOLDER_NAME = "painter_repair"
REPORT_NAME = "report.json"
COMPLAINT_NAME = "complaint.log"
EVENTS_NAME = "events.json"
#: The name the project as it stood before a swap is kept under.
KEPT_NAME = "kept.spp"
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

    __slots__ = ("texture_sets", "interpreter", "project", "directory", "old", "held", "surface", "edited", "arguments",
                 "resume", "fail", "stages", "index", "entered", "called", "ticks", "scouted", "swapped", "opened",
                 "process", "started", "streams", "report", "line", "said", "inside", "reason")

    def __init__(self, texture_sets, interpreter, project_path, directory, old, held, surface, edited, arguments, stages,
                 swapped, resume, fail):
        self.texture_sets = list(texture_sets)
        self.interpreter = interpreter
        self.project = project_path
        self.directory = directory
        self.old = old
        self.held = held
        self.surface = surface
        self.edited = edited
        self.arguments = arguments
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


def begin(generation, record, interpreter, resume, fail):
    """Take a repair in hand: ``record`` is what the surface asks to have repaired (see ``record.mesh``) and ``generation``
    the surface. ``resume`` is called with the path of the surface when the project is repaired and open, ``fail`` with a line when
    it could not be and the project is as it was. Returns one line about it. Nothing is touched when it raises."""
    if running():
        raise RepairError("a repair is still running")
    if record.get("anchor") not in kinds.BY_ANCHOR:
        raise RepairError("the paint can be repaired for a surface whose mesh changed under layouts that stayed ({0!r}) or whose layouts "
                          "changed under a mesh that stayed ({1!r}), not for {2!r}".format(kinds.UV, kinds.SURFACE, record.get("anchor")))
    check(interpreter)
    held = {texture_set.name for texture_set in substance_painter.textureset.all_texture_sets()}
    absent = [name for name in record["texture_sets"] if name not in held]
    if absent:
        raise RepairError("the project has no Texture Set called {0}".format(", ".join(absent)))
    directory = _folder()
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    project_path = substance_painter.project.file_path()
    old = directory / "before.spp"
    edited = directory / "repaired.spp"
    arguments = ["retarget", project_path, *record["texture_sets"], "--anchor", record["anchor"], "--old", str(old),
                 "--out", str(edited)]
    if record.get("passes"):
        arguments += ["--passes", *record["passes"]]
    repair = _Repair(record["texture_sets"], interpreter, project_path, directory, old, old, directory / "surface.fbx", edited,
                     arguments, _FORWARD, False, resume, fail)
    try:
        shutil.copyfile(generation.path(generation.record["scene_file"]), repair.surface)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _current[0] = repair
    LOG.info("repairing the paint of %s for the incoming surface", ", ".join(repair.texture_sets))
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
    arguments = ["carry", project_path, "--old", str(old), "--events", str(directory / EVENTS_NAME), "--out", str(edited)]
    repair = _Repair(sorted({event["target"] for event in events}), interpreter, project_path, directory, old,
                     directory / "held.spp", None, edited, arguments, _CARRYING, False, resume, fail)
    _current[0] = repair
    LOG.info("repairing the paint of the copies in %s", ", ".join(repair.texture_sets))
    return "repairing the paint of the copies in {0}".format(", ".join(repair.texture_sets))


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
    """The repair is over: the files it made are gone, but for the surface, which the swap goes on from."""
    _current[0] = None
    if repair.reason:
        shutil.rmtree(repair.directory, ignore_errors=True)
        LOG.warning("the paint was not repaired: %s", repair.reason)
        repair.fail("the paint could not be repaired, and the project is as it was: {0}".format(repair.reason))
        return
    for path in (repair.old, repair.held, repair.edited):
        path.unlink(missing_ok=True)
    for name in (REPORT_NAME, COMPLAINT_NAME, EVENTS_NAME):
        (repair.directory / name).unlink(missing_ok=True)
    LOG.info("the paint is repaired: %s", _summary(repair.report))
    repair.line = "repaired the paint: {0}".format(_summary(repair.report))
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


def _edit(repair):
    """The process, run on a copy of the project's file, until it ends."""
    if repair.process is None:
        arguments = [repair.interpreter, "-m", "RuriBridge.Spp.cli", *repair.arguments]
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
        if elapsed > EDIT_DEADLINE_SECONDS:
            repair.process.kill()
            raise RepairError("the repair took more than {0:.0f} hours and was stopped".format(EDIT_DEADLINE_SECONDS / 3600.0))
        repair.line = "repairing the paint, {0:.0f} s in".format(elapsed)
        return False
    _close_streams(repair)
    if code != 0:
        raise RepairError("the repair ended with code {0}: {1}".format(code, _complaint(repair)))
    repair.report = json.loads((repair.directory / REPORT_NAME).read_text(encoding="utf-8"))
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


_FORWARD = (_save_held, _scout, _save_current, _closed, _edit, _replaced, _opened)
_CARRYING = (_save_current, _closed, _edit, _replaced, _opened)
_BACKWARD = (_closed_when_open, _put_back, _opened)
_NAMES = {_save_held: "saving the project", _scout: "swapping the new surface in", _save_current: "saving the project with it",
          _closed: "closing the project", _edit: "repairing the paint", _replaced: "putting the repaired file in place",
          _opened: "opening the project", _closed_when_open: "closing the project", _put_back: "putting the saved project back"}
