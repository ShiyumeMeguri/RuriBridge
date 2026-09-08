# -*- coding: utf-8 -*-
"""RuriBridge — the Blender end of the Blender/Substance shared-memory bridge.

Blender owns the ``to_painter`` channel and reads ``to_blender``. Everything it
sends is written straight into the arena's mapped pages; everything it receives
is a file that already lives in those pages by the time this side is told about
it. The polling pump is a Blender timer reading a few integers out of the mapped
control block, which is what a shared-memory bridge costs when idle.

The checkout lives here, in Blender's add-on folder, and Painter reaches the same
files through a directory junction. So the shared core is found by looking in
this package first and then above it -- the add-on sits on top of the core, the
Painter plugin sits one level below it, and one search covers both.
"""

import os
import sys


def _install_core_path():
    here = os.path.dirname(os.path.realpath(__file__))
    candidate = here
    while True:
        if os.path.isfile(os.path.join(candidate, "ruri_bridge", "__init__.py")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            raise ImportError(
                "RuriBridge cannot find the ruri_bridge core at or above {0}; the "
                "add-on must stay inside its checkout".format(here))
        candidate = parent


REPOSITORY_ROOT = _install_core_path()

bl_info = {
    "name": "RuriBridge",
    "author": "ShiyumeMeguri",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > N-panel > RuriBridge",
    "description": "Zero-copy shared-memory bridge to Adobe Substance 3D Painter: "
                   "meshes are written straight into pages Painter maps, and Painter's "
                   "channels come back through the same arena.",
    "category": "Import-Export",
}

import importlib

import bpy

from ruri_bridge import arena as arena_module
from ruri_bridge import channel as channel_module
from ruri_bridge import log as log_module
from ruri_bridge import painter_host
from ruri_bridge import record as record_module
from ruri_bridge import sync as sync_module

from . import mesh_publish, texture_ingest

for _module in (arena_module, channel_module, record_module, sync_module,
                mesh_publish, texture_ingest):
    importlib.reload(_module)

LOG = log_module.logger("blender")

DEFAULT_POLL_SECONDS = 0.25
SHADER_QUIET_SECONDS = 0.35
MESH_QUIET_SECONDS = 1.2


class _Connection:
    """The one live attachment this Blender process holds."""

    def __init__(self):
        self.arena = None
        self.publisher = None
        self.subscriber = None
        self.state_writer = None
        self.state_reader = None
        self.last_state = {}
        self.has_published = False

    @property
    def is_open(self):
        return self.arena is not None

    def open(self, session, root=None):
        self.close()
        self.arena = arena_module.Arena.open_session(
            record_module.CHANNELS, session=session, root=root)
        self.publisher = channel_module.Publisher(self.arena, record_module.CHANNEL_TO_PAINTER)
        self.subscriber = channel_module.Subscriber(self.arena, record_module.CHANNEL_TO_BLENDER)
        self.subscriber.skip_to_latest()
        self.state_writer = channel_module.StateWriter(
            self.arena, record_module.CHANNEL_STATE_TO_PAINTER)
        self.state_reader = channel_module.StateReader(
            self.arena, record_module.CHANNEL_STATE_TO_BLENDER)
        self.state_reader.skip_to_latest()
        LOG.info("attached to session %s at %s", session, self.arena.directory)
        return self.arena

    def close(self):
        if self.arena is not None:
            self.arena.close()
        self.arena = None
        self.publisher = None
        self.subscriber = None
        self.state_writer = None
        self.state_reader = None


CONNECTION = _Connection()
SHADER_GATE = sync_module.ChangeGate("blender.shader", SHADER_QUIET_SECONDS)
MESH_GATE = sync_module.ChangeGate("blender.mesh", MESH_QUIET_SECONDS)
_mesh_serial = 0


def connect(session=arena_module.DEFAULT_SESSION, root=None):
    """Attach without any UI, for headless drivers."""
    return CONNECTION.open(session, root)


def disconnect():
    CONNECTION.close()


def objects_in_scope(context, scope):
    """The objects a publish covers, resolved fresh every time.

    Read through the view layer rather than through ``context.selected_objects``
    so the same call works from a timer, which is what lets live sync answer the
    same question the button does -- including for an object created after the
    last publish.
    """
    objects = [entry for entry in context.view_layer.objects if entry.type == "MESH"]
    if scope == "SELECTED":
        return [entry for entry in objects if entry.select_get() and entry.visible_get()]
    return [entry for entry in objects if entry.visible_get()]


def publish_mesh(context, scope="SELECTED", intent=record_module.INTENT_AUTO,
                 include_colors=True):
    """Gather the scoped objects and publish them as one mesh generation."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    chosen = objects_in_scope(context, scope)
    if not chosen:
        raise RuntimeError("no mesh object in scope {0}".format(scope))
    depsgraph = context.evaluated_depsgraph_get()
    generation = mesh_publish.publish(
        CONNECTION.arena, CONNECTION.publisher, chosen, depsgraph, intent,
        context.scene.unit_settings.scale_length, include_colors,
        binding=scene_binding(context))
    CONNECTION.has_published = True
    MESH_GATE.prime({"serial": _mesh_serial})
    SHADER_GATE.prime(material_values(context.scene.ruri_bridge))
    return generation


def scene_binding(context):
    """The durable answer to "which project does this scene paint into".

    The scene identity is minted the same way a material's is, so it survives
    every rename on either side, and it is what Painter stores inside the saved
    project. The project path travels beside it as the place to look first; the
    identity is what decides whether the project found there is the right one.
    """
    scene = context.scene
    stored = scene.ruri_bridge.painter_project
    identity, is_new = mesh_publish.mint_identity(scene)
    return record_module.binding(
        identity, is_new, bpy.data.filepath,
        bpy.path.abspath(stored) if stored else "")


def request_export(preset_name, resolution_log2=None):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    payload = record_module.export_request("blender", preset_name, resolution_log2)
    return CONNECTION.publisher.publish_record(payload)


def push_shader_parameters(context, scope="SELECTED"):
    """Offer each material's data row to whatever shader Painter runs on it.

    Blender does not filter by name here. It cannot know which uniforms the
    shader on the other side exposes, and a table of names kept on this side
    would be a second truth source for something Painter can be asked directly,
    so the whole row goes and Painter reports what it could not use.
    """
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    rows = mesh_publish.collect_material_rows(objects_in_scope(context, scope))
    values = {row["identity"]: row["properties"] for row in rows if row.get("properties")}
    if not values:
        raise RuntimeError(
            "no material in scope {0} carries any custom property to offer".format(scope))
    SHADER_GATE.prime(values)
    return CONNECTION.state_writer.write(record_module.shader_values("blender", values))


def watched_objects(settings):
    """The objects live sync follows: whatever the scope says, right now."""
    return objects_in_scope(bpy.context, settings.scope)


def material_values(settings):
    """The data rows live sync watches and offers, keyed by identity."""
    rows = mesh_publish.collect_material_rows(watched_objects(settings))
    return {row["identity"]: row["properties"] for row in rows if row.get("properties")}


def live_sync(settings):
    """Publish what changed here, once it has settled. Never what just arrived."""
    if not CONNECTION.is_open or not settings.live_sync or not CONNECTION.has_published:
        return None
    if settings.live_shader_values:
        values = material_values(settings)
        if SHADER_GATE.should_publish(values):
            CONNECTION.state_writer.write(record_module.shader_values("blender", values))
            return "sent {0} shader value(s)".format(
                sum(len(entry) for entry in values.values()))
    if settings.live_mesh and bpy.context.mode == "OBJECT":
        if MESH_GATE.should_publish({"serial": _mesh_serial}):
            generation = publish_mesh(bpy.context, settings.scope, settings.intent,
                                      settings.include_colors)
            return "sent mesh, generation {0}".format(generation.number)
    return None


@bpy.app.handlers.persistent
def _on_save_pre(_path):
    """Saving is when persistence is asked for, so it is when it gets paid for."""
    settings = getattr(bpy.context.scene, "ruri_bridge", None)
    if settings is None or not settings.keep_textures:
        return
    try:
        texture_ingest.keep_textures_in_file()
    except Exception as error:
        LOG.error("could not take the bridge textures into the file: %s", error)


def _on_depsgraph_update(scene, depsgraph):
    """Count geometry edits only.

    Shading updates are excluded on purpose: mirroring Painter's shader values
    onto a material is itself a depsgraph update, and counting it here would make
    every value that arrives from Painter trigger a mesh republish back at it.
    """
    global _mesh_serial
    for update in depsgraph.updates:
        if update.is_updated_geometry:
            _mesh_serial += 1
            return


def ingest_latest_textures(bind=True):
    """Take whatever Painter last exported, even if it predates this session."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    generation = CONNECTION.subscriber.latest(record_module.KIND_TEXTURES)
    if generation is None:
        raise RuntimeError("Painter has not published any textures on this session")
    report = texture_ingest.ingest(generation, bind=bind)
    CONNECTION.subscriber.acknowledge(generation)
    return generation, report


def take_shader_values(bind=True):
    """Mirror Painter's live values back, straight out of the control block."""
    if not CONNECTION.is_open or not bind:
        return None
    payload = CONNECTION.state_reader.take()
    if payload is None:
        return None
    return apply_values(payload.get("by_texture_set", {}))


def apply_values(values_by_texture_set):
    """Write incoming values onto the material rows that already name them.

    Only names the Blender material already carries are written. The material's
    row is what this side considers the material to be about; the other side's
    shader exposes far more, and copying all of it in would move Painter's
    vocabulary into the .blend rather than keep two declared things equal.
    """
    written = {}
    for identity, values in values_by_texture_set.items():
        material = texture_ingest.resolve_material(identity, identity)
        if material is None:
            continue
        for name, value in values.items():
            if name not in material.keys():
                continue
            current = material[name]
            if hasattr(current, "to_list"):
                current = current.to_list()
            if current == value:
                continue
            material[name] = value
            written.setdefault(material.name, []).append(name)
    if written:
        LOG.info("mirrored incoming values onto %s", written)
    settings = _settings_or_none()
    if settings is not None:
        SHADER_GATE.suppress(material_values(settings))
    return written


def settings_scope():
    settings = _settings_or_none()
    return settings.scope if settings is not None else "VISIBLE"


def pump(bind=True):
    """Consume everything Painter has published since the last pump.

    A generation that raises is acknowledged all the same: leaving it unread
    would mean retrying it at every timer tick forever, which turns one bad
    payload into a channel that never moves again.
    """
    if not CONNECTION.is_open:
        return []
    handled = []
    for generation in CONNECTION.subscriber.pending():
        try:
            if generation.kind == record_module.KIND_TEXTURES:
                handled.append((generation.number, generation.kind,
                                texture_ingest.ingest(generation, bind=bind)))
            elif generation.kind == record_module.KIND_SHADER_STATE:
                CONNECTION.last_state[generation.kind] = generation.record
                handled.append((generation.number, generation.kind,
                                len(generation.record.get("instances", []))))
            elif generation.kind == record_module.KIND_PROJECT_STATE:
                CONNECTION.last_state[generation.kind] = generation.record
                remember_painter_executable(generation.record.get("host_executable"))
                remember_painter_project(generation.record.get("project_path"))
                handled.append((generation.number, generation.kind, generation.record))
            elif generation.kind == record_module.KIND_MESH_REQUEST:
                published = publish_mesh(bpy.context, settings_scope(),
                                         record_module.INTENT_AUTO, True)
                handled.append((generation.number, generation.kind, published.number))
            else:
                LOG.warning("ignoring generation %d of unknown kind %r",
                            generation.number, generation.kind)
        except Exception as error:
            LOG.error("generation %d (%s) failed and is being skipped: %s",
                      generation.number, generation.kind, error)
            handled.append((generation.number, "failed", str(error)))
        CONNECTION.subscriber.acknowledge(generation)
    written = take_shader_values(bind)
    if written:
        handled.append((0, record_module.KIND_SHADER_VALUES, written))
    return handled


class RuriBridgeSettings(bpy.types.PropertyGroup):
    session: bpy.props.StringProperty(
        name="Session",
        description="Arena session name; both hosts must use the same one",
        default=arena_module.DEFAULT_SESSION)
    scope: bpy.props.EnumProperty(
        name="Scope",
        description="Which objects a publish sends, re-read on every live update "
                    "so an object added later is included",
        items=[("VISIBLE", "Whole Scene", "Every visible mesh object in the view layer"),
               ("SELECTED", "Selected", "Selected mesh objects only")],
        default="VISIBLE")
    intent: bpy.props.EnumProperty(
        name="Intent",
        description="What Painter should do with the mesh",
        items=[(record_module.INTENT_AUTO, "Auto",
                "Create a project if none is open, otherwise reload the mesh"),
               (record_module.INTENT_CREATE_PROJECT, "Create Project",
                "Always start a new Painter project"),
               (record_module.INTENT_RELOAD_MESH, "Reload Mesh",
                "Reload into the open project, keeping the paint")],
        default=record_module.INTENT_AUTO)
    include_colors: bpy.props.BoolProperty(
        name="Vertex Colors",
        description="Send the active color attribute alongside positions and normals",
        default=True)
    live_sync: bpy.props.BoolProperty(
        name="Live Sync",
        description="Publish changes as soon as they settle, instead of on demand",
        default=True)
    live_shader_values: bpy.props.BoolProperty(
        name="Shader Values",
        description="Follow the published materials' custom properties both ways",
        default=True)
    live_mesh: bpy.props.BoolProperty(
        name="Mesh",
        description="Re-send the published objects after a geometry edit settles. "
                    "Each send is a whole-mesh reload on Painter's side, so it fires "
                    "on leaving Edit Mode rather than per vertex",
        default=True)
    keep_textures: bpy.props.BoolProperty(
        name="Keep Textures In File",
        description="On save, pack the textures Painter sent into the .blend. They "
                    "are read straight out of the arena while both applications are "
                    "live, and the arena keeps only its newest generations, so "
                    "without this a saved file opens with them missing",
        default=True)
    bind_on_receive: bpy.props.BoolProperty(
        name="Bind On Receive",
        description="Fill Image Texture nodes whose label matches an incoming channel",
        default=True)
    painter_project: bpy.props.StringProperty(
        name="Painter Project",
        description="The .spp this scene paints into. Painter fills it in the moment "
                    "the project is saved, and a later send reopens that project "
                    "instead of starting a new one",
        subtype="FILE_PATH", default="")
    export_preset: bpy.props.StringProperty(
        name="Export Preset",
        description="Painter export preset an export request asks for",
        default="Document channels + Normal + AO (No Alpha)")
    poll_seconds: bpy.props.FloatProperty(
        name="Poll",
        description="How often the mapped control block is read",
        default=DEFAULT_POLL_SECONDS, min=0.05, max=5.0)
    status: bpy.props.StringProperty(name="Status", default="detached")


def _timer():
    settings = _settings_or_none()
    if settings is None or not CONNECTION.is_open:
        return None
    try:
        handled = pump(bind=settings.bind_on_receive)
    except Exception as error:
        LOG.error("pump failed: %s", error)
        settings.status = "pump failed: {0}".format(error)
        return settings.poll_seconds
    CONNECTION.arena.touch(record_module.CHANNEL_TO_PAINTER)
    if handled:
        homeless = sorted({entry["texture_set"] for _number, kind, report in handled
                           if kind == record_module.KIND_TEXTURES
                           for entry in report if entry.get("homeless")})
        if homeless:
            settings.status = ("{0} has no material of that name here, so its paint is "
                               "not shown; send the mesh again".format(", ".join(homeless)))
        else:
            settings.status = "received " + ", ".join(
                "{0}#{1}".format(kind, number) for number, kind, _ in handled)
        _tag_redraw()
    try:
        sent = live_sync(settings)
    except Exception as error:
        LOG.error("live sync failed: %s", error)
        settings.status = "live sync failed: {0}".format(error)
        return settings.poll_seconds
    if sent:
        settings.status = "live: " + sent
        _tag_redraw()
    return settings.poll_seconds


def _settings_or_none():
    scene = getattr(bpy.context, "scene", None)
    return getattr(scene, "ruri_bridge", None) if scene is not None else None


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _start_timer():
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=DEFAULT_POLL_SECONDS, persistent=True)


def _stop_timer():
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)


class RuriBridgePreferences(bpy.types.AddonPreferences):
    """The one genuinely machine-specific fact: where Painter is installed.

    It lives in the add-on preferences rather than in the scene, because it is a
    property of this computer and not of the file being worked on. It is filled
    in without anyone typing it -- from the registry on request, and from Painter
    itself the first time the two ever connect.
    """

    bl_idname = __name__

    painter_executable: bpy.props.StringProperty(
        name="Painter",
        description="Adobe Substance 3D Painter executable, used to start it on demand",
        subtype="FILE_PATH", default="")
    auto_launch: bpy.props.BoolProperty(
        name="Start Painter When Sending",
        description="If Painter is not attached when a mesh is sent, start it; the mesh "
                    "waits in the arena and Painter takes it as it opens",
        default=True)

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "painter_executable")
        row.operator(RURIBRIDGE_OT_locate_painter.bl_idname, text="", icon="VIEWZOOM")
        layout.prop(self, "auto_launch")


def preferences():
    entry = bpy.context.preferences.addons.get(__name__)
    return entry.preferences if entry else None


def painter_executable():
    """The configured path, or whatever Windows recorded about the install."""
    stored = preferences()
    if stored is not None and stored.painter_executable:
        return bpy.path.abspath(stored.painter_executable)
    return painter_host.discover_executable()


def remember_painter_executable(path):
    stored = preferences()
    if stored is not None and path and not stored.painter_executable:
        stored.painter_executable = path
        LOG.info("learned where Painter lives: %s", path)


def remember_painter_project(path):
    """Bind the scene to the project Painter just told us it saved.

    Painter reports a path only once the project has one, which is the moment
    somebody saves it. Keeping it on the scene rather than in the session is what
    makes it outlive both applications: it goes into the .blend, so tomorrow's
    send knows to reopen that project instead of starting a fresh one and leaving
    the painted work sitting on disk, correct and unused.
    """
    settings = getattr(bpy.context.scene, "ruri_bridge", None)
    if settings is None or not path or settings.painter_project == path:
        return
    settings.painter_project = path
    LOG.info("this scene now paints into %s", path)


def painter_is_attached():
    """Whether Painter's half of the bridge is alive, not whether it is running.

    The heartbeat answers the question that matters. A Painter with its plugin
    switched off is running and will never respond, and starting a second copy
    because a process check said "no" would be worse than saying so.
    """
    if not CONNECTION.is_open:
        return False
    return CONNECTION.arena.read_slot(record_module.CHANNEL_TO_BLENDER).writer_is_live


class RURIBRIDGE_OT_locate_painter(bpy.types.Operator):
    bl_idname = "ruri_bridge.locate_painter"
    bl_label = "Find Painter"
    bl_description = "Read Painter's install path out of the Windows registry"

    def execute(self, context):
        found = painter_host.discover_executable()
        if found is None:
            self.report({"ERROR"},
                        "Windows has no record of a Painter install; set the path by hand")
            return {"CANCELLED"}
        preferences().painter_executable = found
        self.report({"INFO"}, found)
        return {"FINISHED"}


class RURIBRIDGE_OT_launch_painter(bpy.types.Operator):
    bl_idname = "ruri_bridge.launch_painter"
    bl_label = "Start Painter"
    bl_description = "Start Substance 3D Painter and let it attach to this session"

    def execute(self, context):
        settings = context.scene.ruri_bridge
        if painter_is_attached():
            self.report({"INFO"}, "Painter is already attached")
            return {"FINISHED"}
        if painter_host.is_running():
            settings.status = ("Painter is running but its RuriBridge plugin is off; "
                               "switch it on in Painter's Python menu")
            self.report({"WARNING"}, settings.status)
            return {"CANCELLED"}
        try:
            executable = painter_host.launch(painter_executable(), settings.session)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        remember_painter_executable(executable)
        settings.status = "starting Painter; it attaches on its own"
        return {"FINISHED"}


class RURIBRIDGE_OT_reconnect(bpy.types.Operator):
    bl_idname = "ruri_bridge.reconnect"
    bl_label = "Reattach"
    bl_description = "Attach to the named session again"

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            attached = CONNECTION.open(settings.session)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "attached: {0}".format(attached.directory)
        _start_timer()
        return {"FINISHED"}


class RURIBRIDGE_OT_publish_mesh(bpy.types.Operator):
    bl_idname = "ruri_bridge.publish_mesh"
    bl_label = "Send To Painter"
    bl_description = ("Write the scoped objects into the shared arena, starting Painter if "
                      "it is not attached. This is also what starts live sync")

    def execute(self, context):
        settings = context.scene.ruri_bridge
        if not CONNECTION.is_open:
            bpy.ops.ruri_bridge.reconnect()
        starting = False
        stored = preferences()
        if painter_is_attached():
            LOG.info("Painter is attached, so there is nothing to start")
        elif stored is None:
            self.report({"WARNING"},
                        "the add-on has no preferences entry, so Painter cannot be started")
        elif not stored.auto_launch:
            LOG.info("Painter is not attached and Start Painter When Sending is off; "
                     "the mesh waits in the arena until Painter opens")
        elif painter_host.is_running():
            self.report({"WARNING"},
                        "Painter is running but its RuriBridge plugin is off")
        else:
            try:
                remember_painter_executable(
                    painter_host.launch(painter_executable(), settings.session))
                starting = True
            except Exception as error:
                self.report({"WARNING"}, str(error))
        try:
            generation = publish_mesh(context, settings.scope, settings.intent,
                                      settings.include_colors)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        fresh = sum(1 for row in generation.record.get("materials", [])
                    if row.get("identity_is_new"))
        settings.status = "sent mesh {0}{1}".format(
            generation.number,
            "; Painter is starting and takes it as it opens" if starting else "")
        self.report({"INFO"}, settings.status)
        if fresh:
            self.report({"WARNING"},
                        "{0} material(s) had no identity until now; save this file or "
                        "the next session sends different ones and Painter builds new "
                        "Texture Sets beside the painted ones".format(fresh))
        return {"FINISHED"}


class RURIBRIDGE_OT_request_export(bpy.types.Operator):
    bl_idname = "ruri_bridge.request_export"
    bl_label = "Ask For Textures"
    bl_description = "Ask Painter to render its channels into the arena now"

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation = request_export(settings.export_preset)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "asked for textures, generation {0}".format(generation.number)
        return {"FINISHED"}


class RURIBRIDGE_OT_push_shader_parameters(bpy.types.Operator):
    bl_idname = "ruri_bridge.push_shader_parameters"
    bl_label = "Send Shader Values"
    bl_description = ("Offer each scoped material's custom properties to the shader Painter "
                      "runs on the matching Texture Set")

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            push_shader_parameters(context, settings.scope)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "sent shader values"
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class RURIBRIDGE_OT_pull_textures(bpy.types.Operator):
    bl_idname = "ruri_bridge.pull_textures"
    bl_label = "Pull Latest Textures"
    bl_description = ("Ingest the newest textures Painter published, even if it published "
                      "them before this session attached")

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation, report = ingest_latest_textures(settings.bind_on_receive)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        arrived = sum(entry["bound_nodes"] for entry in report)
        connected = sum(entry.get("connected_nodes", 0) for entry in report)
        settings.status = ("generation {0}: {1} set(s), {2} channel(s) placed, "
                           "{3} wired".format(generation.number, len(report),
                                              arrived, connected))
        if arrived and not connected:
            settings.status += " -- label a texture node after a channel, or declare "
            settings.status += "ruri_bridge_channels on the material, to wire them"
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class RURIBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "RuriBridge"
    bl_idname = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"

    def draw(self, context):
        settings = context.scene.ruri_bridge
        layout = self.layout

        state = layout.box()
        if not CONNECTION.is_open:
            state.label(text="Not attached to a session", icon="UNLINKED")
            state.operator(RURIBRIDGE_OT_reconnect.bl_idname, icon="LINKED")
        elif painter_is_attached():
            state.label(text="Painter is attached", icon="LINKED")
        else:
            state.label(text="Painter is not attached", icon="UNLINKED")
            row = state.row(align=True)
            row.operator(RURIBRIDGE_OT_launch_painter.bl_idname, icon="PLAY")
            row.operator(RURIBRIDGE_OT_locate_painter.bl_idname, text="", icon="VIEWZOOM")

        column = layout.column(align=True)
        column.enabled = CONNECTION.is_open
        column.prop(settings, "scope")
        column.prop(settings, "intent")
        column.prop(settings, "include_colors")
        column.prop(settings, "painter_project", text="Project")
        column.separator()
        column.operator(RURIBRIDGE_OT_publish_mesh.bl_idname, icon="EXPORT")

        live = layout.box()
        live.prop(settings, "live_sync")
        row = live.row(align=True)
        row.enabled = settings.live_sync
        row.prop(settings, "live_shader_values", toggle=True)
        row.prop(settings, "live_mesh", toggle=True)
        if CONNECTION.is_open and not CONNECTION.has_published:
            live.label(text="Send the scene once to start live sync", icon="INFO")

        manual = layout.column(align=True)
        manual.enabled = CONNECTION.is_open
        manual.prop(settings, "export_preset", text="Preset")
        manual.prop(settings, "bind_on_receive")
        manual.prop(settings, "keep_textures")
        manual.operator(RURIBRIDGE_OT_request_export.bl_idname, icon="IMPORT")
        manual.operator(RURIBRIDGE_OT_pull_textures.bl_idname, icon="FILE_REFRESH")
        manual.operator(RURIBRIDGE_OT_push_shader_parameters.bl_idname, icon="NODE_MATERIAL")

        if settings.status:
            layout.box().label(text=settings.status, icon="INFO")


class RURIBRIDGE_PT_diagnostics(bpy.types.Panel):
    bl_label = "Channels"
    bl_idname = "RURIBRIDGE_PT_diagnostics"
    bl_parent_id = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.ruri_bridge
        layout = self.layout
        row = layout.row(align=True)
        row.prop(settings, "session", text="")
        row.operator(RURIBRIDGE_OT_reconnect.bl_idname, text="", icon="FILE_REFRESH")
        layout.prop(settings, "poll_seconds")
        if not CONNECTION.is_open:
            return
        layout.label(text=str(CONNECTION.arena.directory))
        for state in CONNECTION.arena.describe():
            layout.label(text="{0}: gen {1} ack {2} drop {3}".format(
                state.channel, state.generation, state.acknowledged_generation,
                state.dropped_generations))


_CLASSES = (RuriBridgeSettings, RURIBRIDGE_OT_locate_painter, RuriBridgePreferences,
            RURIBRIDGE_OT_launch_painter, RURIBRIDGE_OT_reconnect,
            RURIBRIDGE_OT_publish_mesh, RURIBRIDGE_OT_request_export,
            RURIBRIDGE_OT_pull_textures, RURIBRIDGE_OT_push_shader_parameters,
            RURIBRIDGE_PT_panel, RURIBRIDGE_PT_diagnostics)


def register():
    log_module.install_stream_sink()
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)
    for entry in _CLASSES:
        bpy.utils.register_class(entry)
    bpy.types.Scene.ruri_bridge = bpy.props.PointerProperty(type=RuriBridgeSettings)
    try:
        CONNECTION.open(arena_module.DEFAULT_SESSION)
        _start_timer()
    except Exception as error:
        LOG.error("could not attach on start: %s", error)


def unregister():
    _stop_timer()
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
    CONNECTION.close()
    del bpy.types.Scene.ruri_bridge
    for entry in reversed(_CLASSES):
        bpy.utils.unregister_class(entry)
