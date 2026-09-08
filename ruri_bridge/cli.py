# -*- coding: utf-8 -*-
"""Command line onto a live bridge session.

Everything here reads the same arena the two hosts do, from a third process, so
the protocol can be watched and verified without either application running --
which is the point of putting the state in mapped files rather than in a socket.

``verify-mesh`` is the real check: it re-reads the published GLB out of the arena
and asserts the things a consumer depends on (chunk sizes against the file, every
buffer view inside the binary chunk, every index inside its own vertex range, and
the declared bounds actually bounding the positions). A publish that passes it is
one Painter can open.
"""

from __future__ import annotations

import argparse
import array
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import arena as arena_module
from . import channel as channel_module
from . import glb as glb_module
from . import log as log_module
from . import painter_host
from . import record as record_module

LOG = log_module.logger("cli")

PAINTER_PLUGIN_DIRECTORY_NAME = "painter_plugin"
INSTALLED_NAME = "RuriBridge"
DEFAULT_EXPORT_PRESET = "Document channels + Normal + AO (No Alpha)"


def _repository_root():
    return Path(__file__).resolve().parent.parent


def _open(arguments):
    return arena_module.Arena.open_session(
        record_module.CHANNELS, session=arguments.session, root=arguments.root)


def command_launch(arguments):
    """Start Painter, so a script can set the whole link up on its own."""
    executable = arguments.painter or painter_host.discover_executable()
    if executable is None:
        print("Windows has no record of a Painter install; pass --painter")
        return 1
    if painter_host.is_running():
        print("Painter is already running: {0}".format(executable))
        return 0
    painter_host.launch(executable, arguments.session)
    print("started {0} on session {1}".format(executable, arguments.session))
    return 0


def command_sessions(arguments):
    names = arena_module.list_sessions(arguments.root)
    if not names:
        print("no sessions under {0}".format(arena_module.default_root()))
        return 0
    for name in names:
        print(name)
    return 0


def command_status(arguments):
    with _open(arguments) as arena:
        print("session   {0}".format(arena.session))
        print("directory {0}".format(arena.directory))
        print("epoch     {0}".format(arena.epoch))
        for state in arena.describe():
            if state.channel in record_module.STATE_CHANNELS:
                payload, generation = arena.read_state(state.channel)
                print("  {0:<17} revision={1} inline={2} bytes writer={3}".format(
                    state.channel, generation,
                    len(json.dumps(payload)) if payload else 0, state.writer_process_id))
                continue
            generations = arena.existing_generations(state.channel)
            print("  {0:<17} generation={1} acknowledged={2} dropped={3} "
                  "payload={4} writer={5} on disk={6}".format(
                      state.channel, state.generation, state.acknowledged_generation,
                      state.dropped_generations, state.payload_bytes,
                      state.writer_process_id, generations))
    return 0


def command_values(arguments):
    """Read the inline state slots, which carry no files at all."""
    with _open(arguments) as arena:
        for name in record_module.STATE_CHANNELS:
            payload, generation = arena.read_state(name)
            if payload is None:
                print("{0}: nothing written".format(name))
                continue
            print("{0}: revision {1}, from {2}".format(
                name, generation, payload.get("source")))
            for texture_set, values in sorted(payload.get("by_texture_set", {}).items()):
                print("  {0}".format(texture_set))
                for key, value in sorted(values.items()):
                    print("    {0:<28} {1}".format(key, value))
    return 0


def command_inspect(arguments):
    with _open(arguments) as arena:
        channels = [arguments.channel] if arguments.channel else list(
            record_module.QUEUED_CHANNELS)
        for name in channels:
            state = arena.read_slot(name)
            if state.generation == 0:
                print("{0}: nothing published".format(name))
                continue
            directory = arena.generation_directory(name, state.generation)
            payload = record_module.read(directory)
            print("{0}: generation {1}".format(name, state.generation))
            print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def command_remove(arguments):
    removed = arena_module.remove_session(arguments.session, arguments.root)
    print("removed" if removed else "nothing to remove")
    return 0


def _newest_mesh(arena):
    state = arena.read_slot(record_module.CHANNEL_TO_PAINTER)
    for number in reversed(arena.existing_generations(record_module.CHANNEL_TO_PAINTER)):
        if number > state.generation:
            continue
        directory = arena.generation_directory(record_module.CHANNEL_TO_PAINTER, number)
        payload = record_module.read(directory)
        if payload.get("kind") == record_module.KIND_MESH:
            return number, directory, payload
    return None, None, None


def command_verify_mesh(arguments):
    with _open(arguments) as arena:
        number, directory, payload = _newest_mesh(arena)
        if number is None:
            print("no mesh has been published on {0}".format(record_module.CHANNEL_TO_PAINTER))
            return 1
        path = directory / payload.get("scene_file", record_module.SCENE_FILE_NAME)
        failures = verify_glb(path)
        document, total, binary_length = glb_module.read_document(path)
        print("generation {0}".format(number))
        print("file       {0}".format(path))
        print("size       {0} bytes (binary chunk {1})".format(total, binary_length))
        print("meshes     {0}, materials {1}, accessors {2}".format(
            len(document["meshes"]), len(document["materials"]), len(document["accessors"])))
        for mesh in document["meshes"]:
            for primitive in mesh["primitives"]:
                position = document["accessors"][primitive["attributes"]["POSITION"]]
                indices = document["accessors"][primitive["indices"]]
                print("  {0} / {1}: {2} vertices, {3} triangles, {4}".format(
                    mesh["name"], document["materials"][primitive["material"]]["name"],
                    position["count"], indices["count"] // 3,
                    ", ".join(sorted(primitive["attributes"]))))
        if failures:
            for failure in failures:
                print("FAIL {0}".format(failure))
            return 1
        print("OK all accessors inside the binary chunk, all indices in range, "
              "all declared bounds hold")
    return 0


def verify_glb(path):
    """Re-read a published GLB and check what a consumer will rely on."""
    failures = []
    document, total, binary_length = glb_module.read_document(path)
    actual = os.path.getsize(path)
    if actual != total:
        failures.append("header says {0} bytes, file is {1}".format(total, actual))
    declared = document["buffers"][0]["byteLength"]
    if declared != binary_length:
        failures.append("buffer declares {0} bytes, binary chunk holds {1}".format(
            declared, binary_length))
    for index, view in enumerate(document["bufferViews"]):
        end = view.get("byteOffset", 0) + view["byteLength"]
        if end > binary_length:
            failures.append("bufferView {0} ends at {1}, past the {2} byte chunk".format(
                index, end, binary_length))

    for mesh in document["meshes"]:
        for primitive in mesh["primitives"]:
            position_index = primitive["attributes"]["POSITION"]
            position = document["accessors"][position_index]
            values = array.array("f")
            with glb_module.mapped_accessor(path, position_index) as view:
                values.frombytes(bytes(view))
            if len(values) != position["count"] * 3:
                failures.append("{0}: POSITION holds {1} floats for {2} vertices".format(
                    mesh["name"], len(values), position["count"]))
                continue
            for axis in range(3):
                column = values[axis::3]
                if not column:
                    continue
                if min(column) < position["min"][axis] - 1e-6:
                    failures.append("{0}: POSITION axis {1} goes below its declared min".format(
                        mesh["name"], axis))
                if max(column) > position["max"][axis] + 1e-6:
                    failures.append("{0}: POSITION axis {1} goes above its declared max".format(
                        mesh["name"], axis))

            indices = array.array("I")
            with glb_module.mapped_accessor(path, primitive["indices"]) as view:
                indices.frombytes(bytes(view))
            if len(indices) % 3:
                failures.append("{0}: {1} indices is not a whole number of triangles".format(
                    mesh["name"], len(indices)))
            if indices and max(indices) >= position["count"]:
                failures.append("{0}: index {1} is outside its {2} vertices".format(
                    mesh["name"], max(indices), position["count"]))
    return failures


def command_textures(arguments):
    with _open(arguments) as arena:
        state = arena.read_slot(record_module.CHANNEL_TO_BLENDER)
        if state.generation == 0:
            print("Painter has published nothing")
            return 1
        directory = arena.generation_directory(record_module.CHANNEL_TO_BLENDER, state.generation)
        payload = record_module.read(directory)
        if payload.get("kind") != record_module.KIND_TEXTURES:
            print("newest generation is {0}, not textures".format(payload.get("kind")))
            return 1
        maps_directory = directory / payload["directory"]
        for texture_set in payload["texture_sets"]:
            print("{0} {1}".format(texture_set["name"], texture_set["resolution"]))
            for entry in texture_set["maps"]:
                for name in entry["files"]:
                    full = maps_directory / name
                    size = full.stat().st_size if full.exists() else -1
                    print("  {0:<24} {1:<5} {2:<4} {3:<16} {4} bytes".format(
                        entry["channel"], entry["file_format"], entry["bit_depth"],
                        entry["color_space"], size))
        if arguments.into:
            target = Path(arguments.into)
            target.mkdir(parents=True, exist_ok=True)
            for source in maps_directory.rglob("*"):
                if source.is_file():
                    shutil.copy2(source, target / source.name)
            print("copied to {0}".format(target))
    return 0


_DRIVER = """
import sys
import time
import addon_utils
import bpy

addon_utils.enable({addon!r}, default_set=False, persistent=False)
module = sys.modules[{addon!r}]
module.connect({session!r}, {root!r})
try:
{body}
finally:
    module.disconnect()
"""

MARKER = "RURI_BRIDGE_RESULT"


def _run_blender(arguments, body):
    """Run one bridge action inside Blender, which is the writer of its channel.

    The command line never writes to ``to_painter`` itself: that channel has one
    writer by design, and a driver that quietly became a second one would race
    the add-on for generation numbers.
    """
    driver = _DRIVER.format(
        addon=INSTALLED_NAME, session=arguments.session,
        root=str(arguments.root) if arguments.root else None, body=body)
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    handle.write(driver)
    handle.close()
    command = [arguments.blender, "--background"]
    if arguments.blend:
        command.append(arguments.blend)
    command.extend(["--python", handle.name])
    started = time.time()
    completed = subprocess.run(command, capture_output=True, text=True)
    os.unlink(handle.name)
    for line in completed.stdout.splitlines():
        if line.startswith(MARKER):
            print(line)
    if completed.returncode != 0 or MARKER not in completed.stdout:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        print("Blender did not finish the action (exit {0})".format(completed.returncode))
        return 1
    print("done in {0:.2f}s".format(time.time() - started))
    return 0


def command_publish_mesh(arguments):
    body = (
        "    generation = module.publish_mesh(bpy.context, {scope!r}, {intent!r}, True)\n"
        "    print({marker!r}, 'published', generation.number)"
    ).format(scope=arguments.scope, intent=arguments.intent, marker=MARKER)
    if arguments.then_request_export:
        body += (
            "\n    generation = module.request_export({preset!r})\n"
            "    print({marker!r}, 'requested', generation.number)"
        ).format(preset=arguments.preset, marker=MARKER)
    return _run_blender(arguments, body)


def command_request_export(arguments):
    body = (
        "    generation = module.request_export({preset!r})\n"
        "    print({marker!r}, 'requested', generation.number)"
    ).format(preset=arguments.preset, marker=MARKER)
    return _run_blender(arguments, body)


def command_push_shader_values(arguments):
    body = (
        "    generation = module.push_shader_parameters(bpy.context, {scope!r})\n"
        "    print({marker!r}, 'shader values', generation.number)"
    ).format(scope=arguments.scope, marker=MARKER)
    return _run_blender(arguments, body)


def command_shaders(arguments):
    """What Painter last said about its shaders, from outside both hosts."""
    with _open(arguments) as arena:
        subscriber = channel_module.Subscriber(arena, record_module.CHANNEL_TO_BLENDER)
        generation = subscriber.latest(record_module.KIND_SHADER_STATE)
        if generation is None:
            print("Painter has not published a shader state")
            return 1
        payload = generation.record
        print("generation {0}".format(generation.number))
        for entry in payload["instances"]:
            print("instance {0}  {1}  ({2})".format(entry["id"], entry["label"], entry["shader"]))
            for texture_set, body in sorted(
                    payload["assignment"].get("texturesets", {}).items()):
                if body.get("shader") == entry["label"]:
                    print("  texture set {0}".format(texture_set))
            for name, item in sorted(payload["parameters"].get(str(entry["id"]), {}).items()):
                if arguments.name and arguments.name not in name:
                    continue
                print("  {0:<32} {1:<8} {2}".format(
                    name, item["description"]["dataType"], item.get("value")))
    return 0


def command_pull_textures(arguments):
    if arguments.wait:
        body = (
            "    deadline = time.time() + {timeout}\n"
            "    received = []\n"
            "    while time.time() < deadline and not received:\n"
            "        for number, kind, report in module.pump(bind={bind}):\n"
            "            print({marker!r}, kind, number, report)\n"
            "            if kind == 'textures':\n"
            "                received.append(number)\n"
            "        time.sleep(0.5)\n"
            "    if not received:\n"
            "        raise SystemExit('no textures arrived within {timeout}s')\n"
        ).format(timeout=arguments.timeout, bind=arguments.bind, marker=MARKER)
    else:
        body = (
            "    generation, report = module.ingest_latest_textures(bind={bind})\n"
            "    print({marker!r}, 'textures', generation.number, report)\n"
        ).format(bind=arguments.bind, marker=MARKER)
    if arguments.save:
        body += (
            "    bpy.ops.wm.save_mainfile()\n"
            "    print({marker!r}, 'saved', bpy.data.filepath)"
        ).format(marker=MARKER)
    return _run_blender(arguments, body)


def _is_reparse_point(path):
    """A junction or a symlink, as opposed to a directory that really is one.

    This distinction decides whether a removal deletes a link or deletes the
    checkout the link points at, so it is made before anything is removed rather
    than left to a helper's own idea of what to follow.
    """
    try:
        attributes = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_install(target):
    if _is_reparse_point(target):
        if target.is_dir():
            os.rmdir(target)
        else:
            os.unlink(target)
        return
    if not target.exists():
        return
    if target.is_dir():
        if not (target / "__init__.py").exists():
            raise RuntimeError("{0} exists and is not a RuriBridge install".format(target))
        shutil.rmtree(target)
    else:
        target.unlink()


def _link_or_copy(source, target, use_copy):
    target = Path(target)
    _remove_install(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if use_copy:
        shutil.copytree(source, target)
        return "copied"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(target), str(source)],
        capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError("mklink failed: {0}".format(completed.stderr.strip()))
    return "junctioned"


PAINTER_PLUGIN_SETTINGS_KEY = (
    r"HKCU:\Software\Adobe\Adobe Substance 3D Painter\python_plugins\{0}")


def enable_painter_plugin(name=INSTALLED_NAME):
    """Tick the plugin's own launch-at-start box without opening the menu.

    A plugin under ``plugins/`` is discovered but left off until someone ticks
    it in Painter's Python menu; the tick is a QSettings value, which on Windows
    is this registry key. Painter reads it at start, so this has to happen while
    Painter is closed or it is read before it is written.
    """
    key = PAINTER_PLUGIN_SETTINGS_KEY.format(name)
    script = (
        "New-Item -Path '{0}' -Force | Out-Null; "
        "New-ItemProperty -Path '{0}' -Name 'launch_at_start' -Value 'on' "
        "-PropertyType String -Force | Out-Null; "
        "(Get-ItemProperty -Path '{0}').launch_at_start".format(key))
    completed = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                               capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError("could not set {0}: {1}".format(key, completed.stderr.strip()))
    return completed.stdout.strip()


def command_install(arguments):
    """Point Painter at this checkout. Blender already has it -- it lives there.

    The checkout *is* the Blender add-on, so there is nothing to install on that
    side; only Painter needs a junction into the plugin folder inside it.
    """
    root = _repository_root()
    print("blender add-on   {0}".format(root))
    target = Path(arguments.painter_plugins) / INSTALLED_NAME
    action = _link_or_copy(root / PAINTER_PLUGIN_DIRECTORY_NAME, target, arguments.copy)
    print("painter plugin   {0} {1}".format(action, target))
    if arguments.enable_painter_plugin:
        print("painter launch_at_start = {0} (read at Painter's next start)".format(
            enable_painter_plugin()))
    if arguments.copy:
        print("a copied plugin folder cannot find the shared core above it; "
              "junction unless the whole checkout was copied")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ruri_bridge", description="Inspect and drive a Ruri DCC bridge session")
    parser.add_argument("--session", default=arena_module.DEFAULT_SESSION)
    parser.add_argument("--root", default=None,
                        help="session root; defaults to %%TEMP%%/RuriDccBridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sessions").set_defaults(handler=command_sessions)
    subparsers.add_parser("status").set_defaults(handler=command_status)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--channel", default=None,
                         choices=list(record_module.QUEUED_CHANNELS))
    inspect.set_defaults(handler=command_inspect)

    subparsers.add_parser("remove").set_defaults(handler=command_remove)
    subparsers.add_parser("values").set_defaults(handler=command_values)

    launch = subparsers.add_parser("launch")
    launch.add_argument("--painter", default=None,
                        help="Painter executable; found in the registry when omitted")
    launch.set_defaults(handler=command_launch)
    subparsers.add_parser("verify-mesh").set_defaults(handler=command_verify_mesh)

    textures = subparsers.add_parser("textures")
    textures.add_argument("--into", default=None, help="also copy the maps out to this folder")
    textures.set_defaults(handler=command_textures)

    def add_blender_arguments(parser_to_extend):
        parser_to_extend.add_argument("--blender", required=True, help="path to blender.exe")
        parser_to_extend.add_argument("--blend", default=None, help="blend file to act on")

    publish = subparsers.add_parser("publish-mesh")
    add_blender_arguments(publish)
    publish.add_argument("--scope", default="VISIBLE", choices=["SELECTED", "VISIBLE"])
    publish.add_argument("--intent", default=record_module.INTENT_AUTO,
                         choices=[record_module.INTENT_AUTO,
                                  record_module.INTENT_CREATE_PROJECT,
                                  record_module.INTENT_RELOAD_MESH])
    publish.add_argument("--then-request-export", action="store_true",
                         help="publish an export request right behind the mesh, from the "
                              "same Blender run, so both reach Painter in one batch")
    publish.add_argument("--preset", default=DEFAULT_EXPORT_PRESET)
    publish.set_defaults(handler=command_publish_mesh)

    request = subparsers.add_parser("request-export")
    add_blender_arguments(request)
    request.add_argument("--preset", default=DEFAULT_EXPORT_PRESET)
    request.set_defaults(handler=command_request_export)

    push_values = subparsers.add_parser("push-shader-values")
    add_blender_arguments(push_values)
    push_values.add_argument("--scope", default="VISIBLE", choices=["SELECTED", "VISIBLE"])
    push_values.set_defaults(handler=command_push_shader_values)

    shaders = subparsers.add_parser("shaders")
    shaders.add_argument("--name", default=None, help="only parameters containing this text")
    shaders.set_defaults(handler=command_shaders)

    pull = subparsers.add_parser("pull-textures")
    add_blender_arguments(pull)
    pull.add_argument("--wait", action="store_true",
                      help="wait for a new publish instead of taking the latest one")
    pull.add_argument("--timeout", type=float, default=180.0)
    pull.add_argument("--bind", action="store_true",
                      help="fill Image Texture nodes labelled with an incoming channel")
    pull.add_argument("--save", action="store_true", help="save the blend after ingesting")
    pull.set_defaults(handler=command_pull_textures)

    install = subparsers.add_parser("install")
    install.add_argument("--painter-plugins", required=True,
                         help="Painter's user python/plugins folder")
    install.add_argument("--copy", action="store_true",
                         help="copy instead of creating a directory junction")
    install.add_argument("--enable-painter-plugin", action="store_true",
                         help="tick the plugin's launch-at-start box; Painter must be closed")
    install.set_defaults(handler=command_install)
    return parser


def main(argv=None):
    log_module.install_stream_sink()
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except (arena_module.ArenaError, record_module.RecordError, glb_module.GlbError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
