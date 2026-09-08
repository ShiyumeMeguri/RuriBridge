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
from ruri_bridge import record as record_module

from . import mesh_publish, texture_ingest

for _module in (arena_module, channel_module, record_module, mesh_publish, texture_ingest):
    importlib.reload(_module)

LOG = log_module.logger("blender")

DEFAULT_POLL_SECONDS = 0.25


class _Connection:
    """The one live attachment this Blender process holds."""

    def __init__(self):
        self.arena = None
        self.publisher = None
        self.subscriber = None
        self.last_state = {}

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
        LOG.info("attached to session %s at %s", session, self.arena.directory)
        return self.arena

    def close(self):
        if self.arena is not None:
            self.arena.close()
        self.arena = None
        self.publisher = None
        self.subscriber = None


CONNECTION = _Connection()


def connect(session=arena_module.DEFAULT_SESSION, root=None):
    """Attach without any UI, for headless drivers."""
    return CONNECTION.open(session, root)


def disconnect():
    CONNECTION.close()


def objects_in_scope(context, scope):
    if scope == "SELECTED":
        chosen = [entry for entry in context.selected_objects if entry.type == "MESH"]
    else:
        chosen = [entry for entry in context.view_layer.objects
                  if entry.type == "MESH" and entry.visible_get()]
    return chosen


def publish_mesh(context, scope="SELECTED", intent=record_module.INTENT_AUTO,
                 include_colors=True):
    """Gather the scoped objects and publish them as one mesh generation."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    chosen = objects_in_scope(context, scope)
    if not chosen:
        raise RuntimeError("no mesh object in scope {0}".format(scope))
    depsgraph = context.evaluated_depsgraph_get()
    return mesh_publish.publish(
        CONNECTION.arena, CONNECTION.publisher, chosen, depsgraph, intent,
        context.scene.unit_settings.scale_length, include_colors)


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
    values = {row["name"]: row["properties"] for row in rows if row.get("properties")}
    if not values:
        raise RuntimeError(
            "no material in scope {0} carries any custom property to offer".format(scope))
    return CONNECTION.publisher.publish_record(
        record_module.shader_apply("blender", values))


def ingest_latest_textures(bind=True):
    """Take whatever Painter last exported, even if it predates this session."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    generation = CONNECTION.subscriber.latest(record_module.KIND_TEXTURES)
    if generation is None:
        raise RuntimeError("Painter has not published any textures on this session")
    report = texture_ingest.ingest(generation, CONNECTION.arena.session, bind=bind)
    CONNECTION.subscriber.acknowledge(generation)
    return generation, report


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
                                texture_ingest.ingest(
                                    generation, CONNECTION.arena.session, bind=bind)))
            elif generation.kind in (record_module.KIND_PROJECT_STATE,
                                     record_module.KIND_SHADER_STATE):
                CONNECTION.last_state[generation.kind] = generation.record
                handled.append((generation.number, generation.kind, generation.record))
            else:
                LOG.warning("ignoring generation %d of unknown kind %r",
                            generation.number, generation.kind)
        except Exception as error:
            LOG.error("generation %d (%s) failed and is being skipped: %s",
                      generation.number, generation.kind, error)
            handled.append((generation.number, "failed", str(error)))
        CONNECTION.subscriber.acknowledge(generation)
    return handled


class RuriBridgeSettings(bpy.types.PropertyGroup):
    session: bpy.props.StringProperty(
        name="Session",
        description="Arena session name; both hosts must use the same one",
        default=arena_module.DEFAULT_SESSION)
    scope: bpy.props.EnumProperty(
        name="Scope",
        description="Which objects a publish sends",
        items=[("SELECTED", "Selected", "Selected mesh objects"),
               ("VISIBLE", "Visible", "Every visible mesh object in the view layer")],
        default="SELECTED")
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
    bind_on_receive: bpy.props.BoolProperty(
        name="Bind On Receive",
        description="Fill Image Texture nodes whose label matches an incoming channel",
        default=True)
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
    if handled:
        summary = ", ".join("{0}#{1}".format(kind, number) for number, kind, _ in handled)
        settings.status = "received " + summary
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


class RURIBRIDGE_OT_connect(bpy.types.Operator):
    bl_idname = "ruri_bridge.connect"
    bl_label = "Attach"
    bl_description = "Attach to the arena session, building it if nobody has yet"

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


class RURIBRIDGE_OT_disconnect(bpy.types.Operator):
    bl_idname = "ruri_bridge.disconnect"
    bl_label = "Detach"
    bl_description = "Release the mapping"

    def execute(self, context):
        _stop_timer()
        CONNECTION.close()
        context.scene.ruri_bridge.status = "detached"
        return {"FINISHED"}


class RURIBRIDGE_OT_publish_mesh(bpy.types.Operator):
    bl_idname = "ruri_bridge.publish_mesh"
    bl_label = "Send Mesh"
    bl_description = "Write the scoped objects into the arena for Painter"

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation = publish_mesh(context, settings.scope, settings.intent,
                                      settings.include_colors)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "sent mesh generation {0}".format(generation.number)
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class RURIBRIDGE_OT_request_export(bpy.types.Operator):
    bl_idname = "ruri_bridge.request_export"
    bl_label = "Request Textures"
    bl_description = "Ask Painter to render its channels into the arena now"

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation = request_export(settings.export_preset)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "requested export {0}".format(generation.number)
        return {"FINISHED"}


class RURIBRIDGE_OT_push_shader_parameters(bpy.types.Operator):
    bl_idname = "ruri_bridge.push_shader_parameters"
    bl_label = "Send Shader Values"
    bl_description = ("Offer each scoped material's custom properties to the shader "
                      "Painter runs on the matching Texture Set")

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation = push_shader_parameters(context, settings.scope)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        settings.status = "sent shader values, generation {0}".format(generation.number)
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class RURIBRIDGE_OT_pull_textures(bpy.types.Operator):
    bl_idname = "ruri_bridge.pull_textures"
    bl_label = "Pull Latest Textures"
    bl_description = ("Ingest the newest textures Painter published, even if it "
                      "published them before this session attached")

    def execute(self, context):
        settings = context.scene.ruri_bridge
        try:
            generation, report = ingest_latest_textures(settings.bind_on_receive)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        bound = sum(entry["bound_nodes"] for entry in report)
        settings.status = "pulled generation {0}: {1} texture set(s), {2} node(s) bound".format(
            generation.number, len(report), bound)
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
        row = layout.row(align=True)
        row.prop(settings, "session", text="")
        if CONNECTION.is_open:
            row.operator(RURIBRIDGE_OT_disconnect.bl_idname, text="", icon="UNLINKED")
        else:
            row.operator(RURIBRIDGE_OT_connect.bl_idname, text="", icon="LINKED")

        column = layout.column(align=True)
        column.enabled = CONNECTION.is_open
        column.prop(settings, "scope")
        column.prop(settings, "intent")
        column.prop(settings, "include_colors")
        column.operator(RURIBRIDGE_OT_publish_mesh.bl_idname, icon="EXPORT")
        column.operator(RURIBRIDGE_OT_push_shader_parameters.bl_idname, icon="NODE_MATERIAL")

        column = layout.column(align=True)
        column.enabled = CONNECTION.is_open
        column.prop(settings, "export_preset", text="Preset")
        column.prop(settings, "bind_on_receive")
        column.operator(RURIBRIDGE_OT_request_export.bl_idname, icon="IMPORT")
        column.operator(RURIBRIDGE_OT_pull_textures.bl_idname, icon="FILE_REFRESH")

        layout.prop(settings, "poll_seconds")
        box = layout.box()
        box.label(text=settings.status, icon="INFO")
        if CONNECTION.is_open:
            for state in CONNECTION.arena.describe():
                box.label(text="{0}: gen {1} ack {2} drop {3}".format(
                    state.channel, state.generation, state.acknowledged_generation,
                    state.dropped_generations))


_CLASSES = (RuriBridgeSettings, RURIBRIDGE_OT_connect, RURIBRIDGE_OT_disconnect,
            RURIBRIDGE_OT_publish_mesh, RURIBRIDGE_OT_request_export,
            RURIBRIDGE_OT_pull_textures, RURIBRIDGE_OT_push_shader_parameters,
            RURIBRIDGE_PT_panel)


def register():
    log_module.install_stream_sink()
    for entry in _CLASSES:
        bpy.utils.register_class(entry)
    bpy.types.Scene.ruri_bridge = bpy.props.PointerProperty(type=RuriBridgeSettings)


def unregister():
    _stop_timer()
    CONNECTION.close()
    del bpy.types.Scene.ruri_bridge
    for entry in reversed(_CLASSES):
        bpy.utils.unregister_class(entry)
