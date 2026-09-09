# -*- coding: utf-8 -*-
"""The Blender driver: everything this add-on does that only Blender can do.

Publishing turns evaluated objects into the arena's own GLB layout, ingesting
turns a published payload into images and material nodes, and the pump is a
Blender timer reading a few integers out of the mapped control block -- which is
what a shared-memory bridge costs while nothing is happening.

This is the only place in the package allowed to import ``bpy``. Everything it
says -- the channels, the record shapes, when a change has settled -- is stated
once in :mod:`Kernel` and shared with every other application the bridge reaches.
"""

import importlib

import bpy

from ...Kernel import arena as arena_module
from ...Kernel import host as host_port
from ...Kernel import log as log_module
from ...Kernel import painter_host
from ...Kernel import peers as peers_module
from ...Kernel import record as record_module
from ...Kernel import session as session_module
from ...Kernel import summon as summon_module
from ...Kernel import sync as sync_module
from ...Kernel import topic as topic_module

from . import glb_ingest, mesh_publish, texture_ingest

# Kernel.host is deliberately absent: it holds the bound driver, and reloading it
# would clear the binding while everything that already imported it kept the old
# module object -- "no application is bound", from the next call on.
for _module in (arena_module, record_module, sync_module, topic_module,
                session_module, glb_ingest, mesh_publish, texture_ingest):
    importlib.reload(_module)

LOG = log_module.logger("blender")

DEFAULT_POLL_SECONDS = 0.25
SHADER_QUIET_SECONDS = 0.35
MESH_QUIET_SECONDS = 1.2


PEER = peers_module.BLENDER


class BlenderHost(host_port.Host):
    """This application, as the bridge uses it."""

    @property
    def name(self):
        return PEER.name

    @property
    def capabilities(self):
        return PEER.capabilities

    def log(self, level, message):
        getattr(LOG, level if level != host_port.WARNING else "warning")(message)

    def schedule(self, seconds, function):
        bpy.app.timers.register(function, first_interval=seconds)

    def redraw(self):
        _tag_redraw()

    def receive(self, topic, generation):
        return _receive(topic, generation)

    def collect(self, topic):
        if topic is topic_module.SHADING:
            settings = _settings_or_none()
            return material_values(settings) if settings else {}
        return None


HOST = host_port.bind(BlenderHost())


class _Connection:
    """The one live attachment this Blender process holds."""

    def __init__(self):
        self.session = None
        self.last_state = {}
        self.has_published = False

    @property
    def is_open(self):
        return self.session is not None

    @property
    def arena(self):
        return self.session.arena if self.session is not None else None

    def open(self, session, root=None):
        self.close()
        self.session = session_module.Session.open(
            HOST.name, HOST.capabilities, session=session, root=root)
        for one in topic_module.TOPICS:
            for endpoint in self.session.sources(one):
                if one.kind == topic_module.QUEUED:
                    endpoint.reader.catch_up(None)
                else:
                    endpoint.reader.skip_to_latest()
        LOG.info("attached to session %s at %s", session, self.session.arena.directory)
        return self.session.arena

    def publisher(self, topic):
        return self.session.publisher(topic)

    def writer(self, topic):
        return self.session.writer(topic)

    def close(self):
        if self.session is not None:
            self.session.close()
        self.session = None


CONNECTION = _Connection()
SHADER_GATE = sync_module.ChangeGate("blender.shader", SHADER_QUIET_SECONDS)
MESH_GATE = sync_module.ChangeGate("blender.mesh", MESH_QUIET_SECONDS)
_mesh_serial = 0
#: Object and mesh-data names Blender has reported as edited since the last
#: send. What lets a live send read one object instead of all of them.
_CHANGED_GEOMETRY = set()


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
                 include_colors=True, changed=None):
    """Gather the scoped objects and publish them as one mesh generation.

    ``changed`` names what Blender reported as edited; everything else is handed
    back from the previous gather. None means "read everything", which is what a
    manual send wants.
    """
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    chosen = objects_in_scope(context, scope)
    if not chosen:
        raise RuntimeError("no mesh object in scope {0}".format(scope))
    adopt_painter_identities(chosen)
    depsgraph = context.evaluated_depsgraph_get()
    generation = mesh_publish.publish(
        CONNECTION.arena, CONNECTION.publisher(topic_module.MESH), chosen, depsgraph, intent,
        context.scene.unit_settings.scale_length, include_colors,
        binding=scene_binding(context, chosen), changed=changed)
    _CHANGED_GEOMETRY.clear()
    CONNECTION.has_published = True
    MESH_GATE.prime({"serial": _mesh_serial})
    SHADER_GATE.prime(material_values(context.scene.ruri_bridge))
    return generation


def adopt_painter_identities(objects):
    """Take the names Painter already uses, before any new identity is minted.

    A project somebody made by hand, or a scene whose file was never saved, has
    no identity to match on -- and minting one would build a second set of
    Texture Sets beside the painted ones. Painter reports what it has, so a
    material whose name is a Texture Set over there simply takes that Texture
    Set's name as its identity. Nothing is typed and nothing is guessed: the two
    are bound by the one thing both sides already agree on, and from then on the
    identity carries the pairing so renaming either is free.

    The vertex count is the check. A project that remembers being built from a
    different number of vertices is a different model, and matching names across
    two models would be worse than starting clean, so that case mints as before.
    """
    state = CONNECTION.last_state.get(peers_module.SUBSTANCE.name) or {}
    if not state.get("is_open"):
        return {}
    mine = mesh_publish.vertex_count_of(objects)
    remembered = (state.get("binding") or {}).get("vertex_count")
    if remembered and mine and remembered != mine:
        LOG.info("the open project was built from %d vertices and this scene has %d, "
                 "so its Texture Set names are not read as identities",
                 remembered, mine)
        return {}
    known = {}
    for entry in state.get("texture_sets") or []:
        identity = entry.get("identity")
        if identity:
            known.setdefault(entry.get("name") or identity, identity)
            known.setdefault(identity, identity)
    adopted = {}
    for object_reference in objects:
        for slot in object_reference.material_slots:
            material = slot.material
            if material is None or material.get(mesh_publish.IDENTITY_PROPERTY):
                continue
            identity = known.get(material.name)
            if identity is None:
                continue
            material[mesh_publish.IDENTITY_PROPERTY] = identity
            adopted[material.name] = identity
    if adopted:
        LOG.info("%d material(s) took the name Painter already uses as their identity",
                 len(adopted))
    return adopted


def scene_binding(context, objects):
    """The durable answer to "which project does this scene paint into".

    The scene identity is minted the same way a material's is, so it survives
    every rename on either side, and it is what Painter stores inside the saved
    project. The project path travels beside it as the place to look first; the
    identity is what decides whether the project found there is the right one.
    """
    scene = context.scene
    stored = scene.ruri_bridge.painter_project
    identity, is_new = mesh_publish.mint_scene_identity(scene)
    return record_module.binding(
        identity, is_new, bpy.data.filepath,
        bpy.path.abspath(stored) if stored else "",
        mesh_publish.vertex_count_of(objects))


def request_export(preset_name, resolution_log2=None):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    payload = record_module.request(
        HOST.name, record_module.ASK_FOR_TEXTURES,
        preset_name=preset_name, resolution_log2=resolution_log2, texture_sets=None)
    return CONNECTION.publisher(topic_module.REQUEST).publish_record(payload)


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
    return CONNECTION.writer(topic_module.SHADING).write(
        record_module.shading(HOST.name, values))


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
            CONNECTION.writer(topic_module.SHADING).write(
                record_module.shading(HOST.name, values))
            return "sent {0} shader value(s)".format(
                sum(len(entry) for entry in values.values()))
    if settings.live_mesh and bpy.context.mode == "OBJECT":
        if MESH_GATE.should_publish({"serial": _mesh_serial}):
            generation = publish_mesh(bpy.context, settings.scope, settings.intent,
                                      settings.include_colors,
                                      changed=frozenset(_CHANGED_GEOMETRY))
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
    """Note WHICH geometry was edited, not just that some was.

    Shading updates are excluded on purpose: mirroring Painter's shader values
    onto a material is itself a depsgraph update, and counting it here would make
    every value that arrives from Painter trigger a mesh republish back at it.

    The name kept is whatever the update names -- the object for a transform or a
    modifier, the mesh data for an edit -- and the publisher treats an object as
    stale if either of its two names is in the set. Resolving datablocks to their
    owners here would put a scene-wide walk inside a handler that runs on every
    edit, to answer a question the publisher can answer for free.
    """
    global _mesh_serial
    touched = False
    for update in depsgraph.updates:
        if update.is_updated_geometry:
            name = getattr(update.id, "name", "")
            if name:
                _CHANGED_GEOMETRY.add(name)
            touched = True
    if touched:
        _mesh_serial += 1


def ingest_latest_textures(bind=True):
    """Take whatever Painter last exported, even if it predates this session."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    newest, source = None, None
    for endpoint in CONNECTION.session.sources(topic_module.TEXTURES):
        candidate = endpoint.reader.latest()
        if candidate is not None and (newest is None or candidate.number > newest.number):
            newest, source = candidate, endpoint
    if newest is None:
        raise RuntimeError("nobody has published any textures on this session")
    report = texture_ingest.ingest(newest, bind=bind)
    source.reader.acknowledge(newest)
    return newest, report


def take_shader_values(bind=True):
    """Mirror Painter's live values back, straight out of the control block."""
    if not CONNECTION.is_open or not bind:
        return None
    written = 0
    for endpoint, payload in CONNECTION.session.changed_state():
        if endpoint.topic is topic_module.SHADING:
            SHADER_GATE.suppress(payload.get("by_texture_set", {}))
            written += apply_values(payload.get("by_texture_set", {})) or 0
        elif endpoint.topic is topic_module.PRESENCE:
            CONNECTION.last_state[endpoint.peer] = payload
            remember_painter_executable(payload.get("host_executable"))
            remember_painter_project(payload.get("project_path"))
    return written or None


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
    for endpoint, generation in CONNECTION.session.incoming():
        try:
            handled.append((generation.number, endpoint.topic.key,
                            _receive(endpoint.topic, generation, bind)))
        except Exception as error:
            LOG.error("%s generation %d from %s failed and is being skipped: %s",
                      endpoint.topic.key, generation.number, endpoint.peer, error)
            handled.append((generation.number, "failed", str(error)))
        endpoint.reader.acknowledge(generation)
    written = take_shader_values(bind)
    if written:
        handled.append((0, topic_module.SHADING.key, written))
    return handled


def _receive(topic, generation, bind=True):
    """One arrival, dispatched on the topic it arrived on.

    No ladder over what a record calls itself: the channel already decided that,
    and re-deriving it from the payload would be a second answer to a question
    the transport had answered before the payload was read.
    """
    if topic is topic_module.TEXTURES:
        return texture_ingest.ingest(generation, bind=bind)
    if topic is topic_module.ANIMATION:
        path = generation.directory / generation.record.get(
            "scene_file", record_module.SCENE_FILE_NAME)
        return glb_ingest.apply_performance(
            path, bpy.context.active_object,
            name="{0}_{1}".format(generation.record.get("source", "bridge"),
                                  generation.number))
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if asked == record_module.ASK_FOR_MESH:
            return publish_mesh(bpy.context, settings_scope(),
                                record_module.INTENT_AUTO, True).number
        raise RuntimeError(
            "{0} asked for {1!r}, which this application does not answer".format(
                generation.record.get("source"), asked))
    raise RuntimeError(
        "nothing here receives {0!r} yet, and the topic says this application "
        "hears it".format(topic.key))


def publish_animation(context, scope="SELECTED"):
    """Publish the performance on what is selected, as a GLB carrying only it.

    Blender's own exporter writes it, for the same reason its importer reads the
    way back: a second writer for a format the application already writes is a
    second set of rounding.
    """
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    publisher = CONNECTION.publisher(topic_module.ANIMATION)
    with publisher.staging() as staging:
        target = staging.path(record_module.SCENE_FILE_NAME)
        bpy.ops.export_scene.gltf(
            filepath=str(target), export_format="GLB",
            use_selection=scope == "SELECTED", export_animations=True,
            export_animation_mode="ACTIONS", export_skins=True,
            export_yup=True, export_apply=False)
        if not target.exists():
            raise RuntimeError("the glTF exporter wrote nothing to {0}".format(target))
        payload = record_module.mesh(
            HOST.name, record_module.INTENT_AUTO,
            {"name": context.scene.name}, [],
            context.scene.unit_settings.scale_length, "Y")
        payload["kind"] = topic_module.ANIMATION.key
        payload["frame_start"] = context.scene.frame_start
        payload["frame_end"] = context.scene.frame_end
        payload["fps"] = context.scene.render.fps
        return staging.publish(payload)


def ask_for(peer_name, what, summon=True):
    """Ask another application for something, and knock if it is not watching."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    generation = CONNECTION.publisher(topic_module.REQUEST).publish_record(
        record_module.request(HOST.name, what))
    knocked = summon and _knock(peer_name)
    return generation, knocked


def _knock(peer_name):
    """Summon an application that does not watch. Silent for one that does."""
    peer = peers_module.by_name(peer_name)
    if peer.resident:
        return ""
    settings = _settings_or_none()
    hint = getattr(settings, "cascadeur_executable", "") if settings else ""
    return " ".join(summon_module.summon(peer_name, hint=hint))


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
    CONNECTION.session.touch()
    if handled:
        homeless = sorted({entry["texture_set"] for _number, kind, report in handled
                           if kind == topic_module.TEXTURES.key
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
    cascadeur_executable: bpy.props.StringProperty(
        name="Cascadeur",
        description="Cascadeur executable. It does not keep a plugin running, so it "
                    "has to be summoned to come and look at what was published",
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
    return CONNECTION.session.present(peers_module.SUBSTANCE.name)


def _cascadeur_hint():
    stored = preferences()
    return (bpy.path.abspath(stored.cascadeur_executable)
            if stored is not None and stored.cascadeur_executable else "")


def _summon_cascadeur():
    """Knock, and say what was run. Empty when the application was not found."""
    try:
        return " ".join(summon_module.summon(
            peers_module.CASCADEUR.name, hint=_cascadeur_hint()))
    except summon_module.SummonError as error:
        LOG.warning("could not summon Cascadeur: %s", error)
        return ""


class RURIBRIDGE_OT_locate_cascadeur(bpy.types.Operator):
    bl_idname = "ruri_bridge.locate_cascadeur"
    bl_label = "Find Cascadeur"
    bl_description = "Read Cascadeur's install path out of the Windows registry"

    def execute(self, context):
        found = summon_module.locate(peers_module.CASCADEUR)
        if not found:
            self.report({"WARNING"},
                        "Windows has no registration for cascadeur.exe; type the path")
            return {"CANCELLED"}
        preferences().cascadeur_executable = found
        self.report({"INFO"}, found)
        return {"FINISHED"}


class RURIBRIDGE_OT_send_animation(bpy.types.Operator):
    bl_idname = "ruri_bridge.send_animation"
    bl_label = "Send Rig and Animation to Cascadeur"
    bl_description = ("Publish the selected rig and what it is doing, then summon\n"
                      "Cascadeur. The skeleton travels with the performance: a\n"
                      "performance without the bones it is keyed to is not one")

    def execute(self, context):
        try:
            generation = publish_animation(context, settings_scope())
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        knocked = _summon_cascadeur()
        self.report({"INFO"}, "sent the rig and its animation, generation {0}{1}".format(
            generation.number, "; summoned Cascadeur" if knocked else ""))
        return {"FINISHED"}


class RURIBRIDGE_OT_fetch_animation(bpy.types.Operator):
    bl_idname = "ruri_bridge.fetch_animation"
    bl_label = "Fetch Animation from Cascadeur"
    bl_description = ("Ask Cascadeur for what it currently has and summon it to "
                      "answer. What comes back lands on the active armature")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def execute(self, context):
        try:
            generation, knocked = ask_for(peers_module.CASCADEUR.name,
                                          record_module.ASK_FOR_ANIMATION)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "asked Cascadeur, generation {0}{1}".format(
            generation.number,
            "; summoned it" if knocked else "; could not summon it"))
        return {"FINISHED"}


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
        else:
            for name, here in CONNECTION.session.attendance():
                if name == HOST.name:
                    continue
                peer = peers_module.by_name(name)
                if here:
                    state.label(text="{0} is attached".format(peer.label), icon="LINKED")
                elif peer.resident:
                    state.label(text="{0} is not attached".format(peer.label),
                                icon="UNLINKED")
                    row = state.row(align=True)
                    row.operator(RURIBRIDGE_OT_launch_painter.bl_idname, icon="PLAY")
                    row.operator(RURIBRIDGE_OT_locate_painter.bl_idname,
                                 text="", icon="VIEWZOOM")
                else:
                    # Not a problem to report: this application is not supposed to
                    # be sitting there watching. It is summoned when it is needed.
                    state.label(text="{0} is summoned when needed".format(peer.label),
                                icon="TIME")

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

        animation = layout.column(align=True)
        animation.enabled = CONNECTION.is_open
        animation.label(text="Animation", icon="ARMATURE_DATA")
        animation.operator(RURIBRIDGE_OT_send_animation.bl_idname, icon="ACTION")
        animation.operator(RURIBRIDGE_OT_fetch_animation.bl_idname, icon="IMPORT")
        if not _cascadeur_hint() and not summon_module.locate(peers_module.CASCADEUR):
            row = animation.row(align=True)
            row.operator(RURIBRIDGE_OT_locate_cascadeur.bl_idname, icon="VIEWZOOM")

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


_CLASSES = (RuriBridgeSettings, RURIBRIDGE_OT_locate_painter,
            RURIBRIDGE_OT_locate_cascadeur, RURIBRIDGE_OT_send_animation,
            RURIBRIDGE_OT_fetch_animation,
            RuriBridgePreferences,
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
