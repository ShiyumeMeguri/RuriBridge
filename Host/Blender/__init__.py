# -*- coding: utf-8 -*-
"""The Blender driver: everything this add-on does that only Blender can do.

The same verbs as Painter's end, from this side, and one table:

* **Update Mesh** sends the model -- the surface somebody paints on -- and Painter
  swaps it in under every layer it already has; nothing else changes.
* **Push / Pull Shader** -- every Texture Set, or the one the active material paints
  into: the shader of the material that paints it, with every parameter, into Painter;
  or Painter's parameters back into that material.
* **Push / Pull Textures** -- the material's own textures stood up in Painter, where it
  runs the same shader; or what is painted in Painter rendered into this document's
  textures folder and put into the materials that paint into it.
* **Pull Selected Layer** asks Painter for the one layer selected over there, on
  its own, as images in the same folder.
* **The table** says which material paints into which Texture Set, and is the one
  thing edited by hand -- from here or from Painter.
* **Retarget Layout** moves the active material's Texture Set to the UV layout another
  UV map of its meshes holds (``layout_retarget``): Painter keeps every layer, reading
  what was laid out in the old layout through the map that holds it now.

Nothing is sent on its own. Every crossing is somebody pressing a button, here or
in Painter, so a timer here only reads a few integers out of the mapped control
block and answers what was asked.

This is the only place in the package allowed to import ``bpy``.
"""

import importlib
import os

import bpy

from ...Kernel import arena as arena_module
from ...Kernel import host as host_port
from ...Kernel import layout as layout_module
from ...Kernel import log as log_module
from ...Kernel import painter_host
from ...Kernel import peers as peers_module
from ...Kernel import record as record_module
from ...Kernel import session as session_module
from ...Kernel import summon as summon_module
from ...Kernel import topic as topic_module

from . import (chart_resample, fbx_surface, glb_ingest, layout_retarget, layout_triangles,
               layouts, material_inputs, material_relayout, mesh_publish, pixels, shader_ingest,
               slot_compose, texture_ingest)

# Kernel.host is deliberately absent: it holds the bound driver, and reloading it
# would clear the binding while everything that already imported it kept the old
# module object -- "no application is bound", from the next call on.
for _module in (arena_module, record_module, topic_module, session_module, layout_module,
                glb_ingest, fbx_surface, layouts, mesh_publish, pixels, slot_compose,
                texture_ingest, material_inputs, shader_ingest, chart_resample, layout_triangles,
                material_relayout, layout_retarget):
    importlib.reload(_module)

LOG = log_module.logger("blender")

POLL_SECONDS = 0.25
#: Ticks between looks at whether this document's materials changed. The look is
#: a walk over the visible objects' slots, and the table it feeds is read by a
#: person: once a second is plenty.
PRESENCE_TICKS = 4

PEER = peers_module.BLENDER
PAINTER = peers_module.SUBSTANCE
#: The add-on's own module, two packages above this driver. Blender files an add-on's
#: preferences under it; a class keyed by this driver's package is never attached.
ADDON = __package__.rsplit(".", 2)[0]


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
        if topic is topic_module.PRESENCE:
            return presence_record()
        return None


HOST = host_port.bind(BlenderHost())


class _Connection:
    """The one live attachment this Blender process holds."""

    def __init__(self):
        self.session = None
        #: What each other application last said it has open, by name.
        self.peers = {}

    @property
    def is_open(self):
        return self.session is not None

    @property
    def arena(self):
        return self.session.arena if self.session is not None else None

    def open(self, session, root=None):
        """Attach. Nothing waiting is replayed: every crossing is asked for."""
        self.close()
        self.session = session_module.Session.open(
            HOST.name, HOST.capabilities, session=session, root=root)
        for one in topic_module.TOPICS:
            for endpoint in self.session.sources(one):
                endpoint.reader.skip_to_latest()
        self.peers = {peer: payload for peer, payload
                      in self.session.current_state(topic_module.PRESENCE)}
        LOG.info("attached to session %s at %s", session, self.session.arena.directory)
        return self.session.arena

    def close(self):
        if self.session is not None:
            self.session.close()
        self.session = None
        self.peers = {}


CONNECTION = _Connection()
_status = [""]
_view = {"materials": [], "bare": [], "fingerprint": None, "ticks": 0}


def connect(session=arena_module.DEFAULT_SESSION, root=None):
    """Attach without any UI, for headless drivers."""
    return CONNECTION.open(session, root)


def disconnect():
    CONNECTION.close()


def say(message):
    _status[0] = message
    LOG.info(message)
    _tag_redraw()


# -- what this document has -------------------------------------------------------

def preferences():
    """What is set for this add-on on this computer."""
    return bpy.context.preferences.addons[ADDON].preferences


def textures_directory():
    """The document's textures folder, or empty while the document has never been saved."""
    if not bpy.data.filepath:
        return ""
    return bpy.path.abspath("//" + preferences().textures_folder)


def _image_path(image):
    if image is None or not image.filepath:
        return ""
    return os.path.abspath(bpy.path.abspath(image.filepath, library=image.library))


def images_of(material):
    """Every image this material samples, by the name it samples it under.

    A generated material keeps them in its own record group; any other material
    keeps them in Image Texture nodes. Read here so Painter can offer them -- as
    a mask or a reference for the layer selected over there -- without guessing
    file names.
    """
    found = {}
    declaration = material.get(mesh_publish.SHADING_DECLARATION)
    group_name = None
    if declaration is not None:
        group_name = (dict(declaration.get("images") or {})).get("group")
    group = material.get(group_name) if group_name else None
    if group is not None:
        for slot, image in dict(group).items():
            path = _image_path(image) if isinstance(image, bpy.types.Image) else ""
            if path:
                found[slot] = path
    elif material.node_tree is not None:
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                path = _image_path(node.image)
                if path:
                    found[node.label or node.image.name] = path
    return found


def material_rows(objects):
    """The table's Blender half: every worn material, what it paints into, on what, and
    which generated shader it runs."""
    rows = []
    for name, (material, wearing) in mesh_publish.wearers(objects).items():
        shader, identity = mesh_publish.declared_shader(material)
        rows.append({
            "name": name,
            "texture_set": mesh_publish.texture_set_of(material),
            "objects": sorted(entry.name for entry in wearing),
            "images": images_of(material),
            "shader": shader,
            "identity": identity,
        })
    return rows


def presence_record():
    return record_module.presence(
        HOST.name, bpy.data.filepath, materials=_view["materials"],
        textures_directory=textures_directory())


def refresh_view(force=False):
    """Re-read this document's table; say so to the session when it moved."""
    objects = mesh_publish.scope(bpy.context.view_layer)
    rows = material_rows(objects)
    bare = mesh_publish.bare_objects(objects)
    fingerprint = (bpy.data.filepath, textures_directory(),
                   tuple((row["name"], row["texture_set"], row["identity"]) for row in rows),
                   tuple(bare))
    _view["materials"] = rows
    _view["bare"] = bare
    if not force and fingerprint == _view["fingerprint"]:
        return False
    _view["fingerprint"] = fingerprint
    if CONNECTION.is_open:
        CONNECTION.session.writer(topic_module.PRESENCE).write(presence_record())
    _tag_redraw()
    return True


def painter_state():
    """What Painter last said it has open, or an empty answer."""
    return CONNECTION.peers.get(PAINTER.name) or {}


def texture_sets_new_to_painter():
    """Texture Sets the next mesh would add to a project Painter already paints in.

    A new Texture Set starts with nothing on it. When it is a material just made,
    that is what anybody expects; when it is a material that used to paint into
    another set, its faces leave their paint behind there -- every layer is kept,
    and none of it shows on them any more. The two look the same from here, so the
    person sending decides.
    """
    painter = painter_state()
    existing = painter.get("texture_sets") or []
    if not painter.get("document") or not any(entry["layers"] for entry in existing):
        return []
    known = {entry["name"] for entry in existing}
    return sorted({row["texture_set"] for row in _view["materials"]
                   if row["texture_set"] and row["texture_set"] not in known})


def painter_is_attached():
    """Whether Painter's half of the bridge is alive -- its heartbeat, not its process.

    A Painter with its plugin switched off is running and will never respond, and
    starting a second copy because a process check said "no" would be worse than
    saying so.
    """
    return CONNECTION.is_open and CONNECTION.session.present(PAINTER.name)


# -- the verbs ------------------------------------------------------------------------

def project_frame(context):
    """The frame the surface has to be written in for the project Painter has open.

    A project keeps the frame it was started in; a project not open yet is started
    in centimetres. Painter having a project whose frame nobody knows is a refusal,
    not a guess: a surface in the wrong frame moves everything painted in 3D.
    """
    painter = painter_state()
    if painter.get("document"):
        known = painter.get("frame")
        if not known:
            raise RuntimeError(
                "Painter's open project was not started by the bridge, so the frame its "
                "surface lives in is unknown; nothing was sent")
        return known
    return record_module.frame(
        record_module.CENTIMETRES_PER_METRE * context.scene.unit_settings.scale_length)


def send_mesh(context):
    """Send the model. Painter keeps every layer it has: it only swaps the surface."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    objects = mesh_publish.scope(context.view_layer)
    if not objects:
        raise RuntimeError("no visible mesh object to send")
    generation = mesh_publish.publish(
        CONNECTION.session.publisher(topic_module.MESH), objects, project_frame(context))
    refresh_view(force=True)
    return generation


def selected_texture_set(context):
    """The Texture Set the active object's active material paints into."""
    material = context.object.active_material if context.object is not None else None
    if material is None:
        raise RuntimeError("the active object has no active material")
    texture_set = mesh_publish.texture_set_of(material)
    if not texture_set:
        raise RuntimeError("{0} paints into no Texture Set".format(material.name))
    return texture_set


def push_shader(context, texture_sets=None):
    """Push materials' shaders, with every parameter, into Painter -- every Texture Set's,
    or these: the shader, its identity, and the row of the material that speaks for each.

    Painter decides what it can take: the same shader by identity takes the whole row,
    any other shader only the parameters both name. Painter's Pull Shader asks for
    exactly this. Returns the rows pushed and the Texture Sets that were not, with why.
    """
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    objects = mesh_publish.scope(context.view_layer)
    speaking = mesh_publish.speakers(objects)
    declared = mesh_publish.shading_rows(objects)
    wanted = sorted(speaking) if texture_sets is None else sorted(texture_sets)
    skipped = {}
    for texture_set in wanted:
        if texture_set not in speaking:
            skipped[texture_set] = "nothing here paints into it"
        elif texture_set not in declared:
            skipped[texture_set] = "{0} is not on a generated shader".format(
                speaking[texture_set][0].name)
    for texture_set, why in sorted(skipped.items()):
        LOG.warning("%s: shader not pushed: %s", texture_set, why)
    rows = {texture_set: declared[texture_set] for texture_set in wanted
            if texture_set in declared}
    if not rows:
        raise RuntimeError("no shader to push: " + "; ".join(
            "{0}: {1}".format(texture_set, why) for texture_set, why in sorted(skipped.items())))
    CONNECTION.session.writer(topic_module.SHADING).write(record_module.shading(
        HOST.name,
        {texture_set: row["parameters"] for texture_set, row in rows.items()},
        vocabulary_by_texture_set={texture_set: row["shader"] for texture_set, row in rows.items()},
        shader_name_by_texture_set={texture_set: row["name"] for texture_set, row in rows.items()},
        identity_by_texture_set={texture_set: row["identity"] for texture_set, row in rows.items()}))
    return rows, skipped


def _textures_folder():
    directory = textures_directory()
    if not directory:
        raise RuntimeError("this .blend has never been saved, so it has no textures folder "
                           "beside it for Painter to write into")
    return directory


def _ask(what, **details):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    return CONNECTION.session.publisher(topic_module.REQUEST).publish_record(
        record_module.request(HOST.name, what, **details))


def pull_shader(texture_sets=None):
    """Pull Painter's shader parameters into the materials here: every Texture Set's, or
    these. Painter's Push Shader does exactly this."""
    return _ask(record_module.ASK_FOR_SHADING, texture_sets=texture_sets)


def push_textures(texture_sets=None):
    """Push the materials' own textures into Painter: every Texture Set, or these, stood
    up from them where Painter runs the same shader. Painter's Pull Textures does exactly
    this."""
    return _ask(record_module.ASK_TO_TAKE_TEXTURES, texture_sets=texture_sets)


def pull_textures(texture_sets=None):
    """Pull what is painted in Painter into the materials here: every Texture Set, or
    these, rendered into this document's textures folder. Painter's Push Textures does
    exactly this."""
    return _ask(record_module.ASK_FOR_TEXTURES, directory=_textures_folder(), layer=False,
                texture_sets=texture_sets)


def pull_selected_layer():
    """Ask Painter for the layer selected over there, on its own."""
    return _ask(record_module.ASK_FOR_TEXTURES, directory=_textures_folder(), layer=True,
                texture_sets=None)


def bind(texture_set, material_name):
    """Make a material paint into a Texture Set Painter has.

    Two cases, decided by whether some material here already paints into it. If
    one does, the Texture Set already has its proper name and this material joins
    it -- one material split in two across one UV layout. If none does, the name
    is Painter's alone and is not the convention here, so Painter is asked to
    rename the Texture Set after the material: a rename is the one edit there
    that keeps every layer.
    """
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError("there is no material called {0!r} here".format(material_name))
    owners = texture_ingest.materials_painting(texture_set)
    if owners:
        mesh_publish.paint_into(material, texture_set)
        refresh_view(force=True)
        return "{0} now paints into {1}, with {2}".format(
            material.name, texture_set, ", ".join(one.name for one in owners))
    mesh_publish.paint_into(material, material.name)
    _ask(record_module.ASK_TO_RENAME, renames={texture_set: material.name})
    refresh_view(force=True)
    return "asked Painter to call {0} {1}".format(texture_set, material.name)


def exclude(material_name, excluded):
    """Keep a material's faces out of Painter, or let them back in."""
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError("there is no material called {0!r} here".format(material_name))
    mesh_publish.paint_into(material, "" if excluded else material.name)
    refresh_view(force=True)


def retarget_layout(context, target_layer):
    """Move the Texture Set the active material paints to the layout its meshes hold in
    ``target_layer``: the render map takes that layout and ``target_layer`` the one it has
    now. Painter is asked first, and the change completes with its answer."""
    if not painter_is_attached():
        raise RuntimeError("Painter is not attached, and a layout change keeps its layers "
                           "only with it")
    material = context.object.active_material if context.object is not None else None
    if material is None:
        raise RuntimeError("the active object has no active material")
    return layout_retarget.begin(CONNECTION.session, material, target_layer)


def follow_rename(material_name):
    """A material renamed here: rename its Texture Set to match, and keep painting it."""
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError("there is no material called {0!r} here".format(material_name))
    old = mesh_publish.texture_set_of(material)
    mesh_publish.paint_into(material, material.name)
    _ask(record_module.ASK_TO_RENAME, renames={old: material.name})
    refresh_view(force=True)


# -- where a material nobody generated takes Painter's maps --------------------------------

#: What a node or a lane is pointed at to take nothing from Painter. Never a map's name:
#: those come from file names, which keep no lone separator.
NOTHING = "-"


def painter_maps(texture_set):
    """The maps Painter says a whole export of this Texture Set writes, and why it cannot
    say when it cannot."""
    if not painter_is_attached():
        return [], "Painter is not attached, so its maps are not known"
    for entry in painter_state().get("texture_sets") or []:
        if entry["name"] == texture_set:
            return list(entry.get("maps") or []), str(entry.get("maps_refused") or "")
    return [], "Painter has no Texture Set {0}".format(texture_set)


def texture_nodes(material):
    """The material's Image Texture nodes, the textures a table can point at."""
    if material.node_tree is None:
        return []
    return [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE"]


def _material(material_name):
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError("there is no material called {0!r} here".format(material_name))
    return material


def _table_to_edit(material):
    """The material's table; the first time it is edited, the one that says what landing
    by name already does, so taking one node in hand changes nothing about the others."""
    table = texture_ingest.mapping_of(material)
    if table:
        return table
    maps, _why = painter_maps(mesh_publish.texture_set_of(material))
    return {node_name: slot_compose.whole(map_name)
            for node_name, map_name in texture_ingest.landing_by_name(material, maps).items()}


def map_texture(material_name, node_name, lane, choice):
    """Point a texture node at a Painter map whole, or one of its lanes at one component
    of a map; ``NOTHING`` takes nothing there."""
    material = _material(material_name)
    table = _table_to_edit(material)
    if lane < 0:
        table[node_name] = ([slot_compose.keep_lane() for _ in slot_compose.LANES]
                            if choice == NOTHING else slot_compose.whole(choice))
    else:
        lanes = table.get(node_name) or [slot_compose.keep_lane() for _ in slot_compose.LANES]
        if choice == NOTHING:
            lanes[lane] = slot_compose.keep_lane()
        else:
            map_name, component = choice.rsplit(":", 1)
            lanes[lane] = {"map": map_name, "component": int(component), "invert": False}
        table[node_name] = lanes
    texture_ingest.set_mapping(material, table)


def invert_lane(material_name, node_name, lane):
    """Take one minus what a lane takes, or stop doing so."""
    material = _material(material_name)
    table = _table_to_edit(material)
    lanes = table.get(node_name)
    if not lanes or not lanes[lane]["map"]:
        raise RuntimeError("lane {0} of {1} takes nothing to invert".format(
            slot_compose.LANES[lane], node_name))
    lanes[lane]["invert"] = not lanes[lane]["invert"]
    texture_ingest.set_mapping(material, table)


def reset_mapping(material_name):
    """Drop the material's table: Painter's maps land in the nodes named after them again."""
    texture_ingest.set_mapping(_material(material_name), {})


# -- what arrives -------------------------------------------------------------------------

def _receive(topic, generation):
    """One arrival, dispatched on the topic it arrived on."""
    if topic is topic_module.TEXTURES:
        if generation.kind == record_module.LAYOUT_ANSWER:
            line = layout_retarget.complete(bpy.context, CONNECTION.session, generation,
                                            project_frame(bpy.context), textures_directory())
        else:
            line = texture_ingest.ingest(generation)
        refresh_view(force=True)
        return line
    if topic is topic_module.ANIMATION:
        path = generation.directory / generation.record["scene_file"]
        return glb_ingest.apply_performance(
            path, bpy.context.active_object,
            name="{0}_{1}".format(generation.record.get("source", "bridge"),
                                  generation.number))
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if not topic_module.can_answer(asked, HOST.capabilities):
            return None
        if asked == record_module.ASK_FOR_MESH:
            return send_mesh(bpy.context).number
        if asked == record_module.ASK_TO_BIND:
            return bind(generation.record["texture_set"], generation.record["material"])
        if asked == record_module.ASK_FOR_SHADING:
            rows, _skipped = push_shader(bpy.context, generation.record["texture_sets"])
            return "pushed the shader of {0} Texture Set(s) to Painter".format(len(rows))
        if asked == record_module.ASK_FOR_INPUTS:
            return material_inputs.bake(
                CONNECTION.session.publisher(topic_module.INPUTS), generation.record,
                mesh_publish.worn_materials(mesh_publish.scope(bpy.context.view_layer)))
    raise RuntimeError(
        "nothing here receives {0!r} yet, and the topic says this application "
        "hears it".format(topic.key))


def _describe(report):
    """One line about a textures arrival, saying what is waiting and why."""
    results = [entry for entry in report if not entry["layer"]]
    parts = []
    if results:
        placed = sum(sum(entry["placed"].values()) for entry in results)
        homeless = [entry["texture_set"] for entry in results if not entry["materials"]]
        stood_up = [entry for entry in results if entry["stood_up"] is not None]
        parts.append("{0} Texture Set(s) in, {1} channel(s) placed".format(
            len(results), placed))
        if stood_up:
            took = {material: slots for entry in stood_up
                    for material, slots in entry["stood_up"]["taken"].items()}
            parts.append("{0} material(s) took {1} texture(s) from Painter".format(
                len(took), sum(len(slots) for slots in took.values())))
        flat = [entry["texture_set"] for entry in stood_up if entry["stood_up"]["flat"]]
        if flat:
            parts.append("nothing painted on {0}, so its materials keep their textures".format(
                ", ".join(flat)))
        refused = ["{0}{1}: {2}".format(entry["texture_set"], " " + slot if slot else "", why)
                   for entry in stood_up for slot, why in sorted(entry["stood_up"]["refused"].items())]
        if refused:
            LOG.warning("not stood up: %s", "; ".join(refused))
            parts.append("{0} texture(s) not stood up (see the log)".format(len(refused)))
        if homeless:
            parts.append("nothing here paints into {0}".format(", ".join(homeless)))
    for entry in report:
        if entry["layer"]:
            parts.append("layer {0} of {1}: {2} image(s), kept out of the materials".format(
                entry["layer"], entry["texture_set"], len(entry["images"])))
    return "; ".join(parts)


def pump():
    """Consume what the others published since the last pump.

    A generation that raises is acknowledged all the same: leaving it unread
    would retry it at every tick forever, turning one bad payload into a channel
    that never moves again. The failure belongs in the log, not in the queue.
    """
    if not CONNECTION.is_open:
        return []
    handled = []
    for endpoint, generation in CONNECTION.session.incoming():
        try:
            result = _receive(endpoint.topic, generation)
            handled.append((endpoint.topic.key, generation.number, result))
            if isinstance(result, str):
                say(result)
            elif endpoint.topic is topic_module.TEXTURES:
                say(_describe(result))
        except Exception as error:
            LOG.error("%s generation %d from %s failed and is being skipped: %s",
                      endpoint.topic.key, generation.number, endpoint.peer, error)
            say("{0} from {1} failed: {2}".format(endpoint.topic.key, endpoint.peer, error))
            handled.append((endpoint.topic.key, generation.number, None))
        endpoint.reader.acknowledge(generation)
    for endpoint, payload in CONNECTION.session.changed_state():
        if endpoint.topic is topic_module.PRESENCE:
            CONNECTION.peers[endpoint.peer] = payload
            remember_painter_executable(payload.get("host_executable"))
            _tag_redraw()
        elif endpoint.topic is topic_module.SHADING:
            try:
                say(shader_ingest.take(payload, mesh_publish.scope(bpy.context.view_layer)))
            except Exception as error:
                LOG.error("the shader from %s could not be taken: %s", endpoint.peer, error)
                say("shader not taken: {0}".format(error))
    return handled


def _timer():
    if not CONNECTION.is_open:
        return None
    try:
        pump()
        _view["ticks"] += 1
        if _view["ticks"] >= PRESENCE_TICKS:
            _view["ticks"] = 0
            refresh_view()
        CONNECTION.session.touch()
    except Exception as error:
        LOG.error("pump failed: %s", error)
        say("pump failed: {0}".format(error))
    return POLL_SECONDS


@bpy.app.handlers.persistent
def _ruri_bridge_after_load(_path):
    """A different document is open: its table is a different table."""
    _view["fingerprint"] = None
    if CONNECTION.is_open:
        refresh_view(force=True)


def _tag_redraw():
    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _start_timer():
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=POLL_SECONDS, persistent=True)


def _stop_timer():
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)


# -- where the other applications live ---------------------------------------------------

def painter_executable():
    """The configured path, or whatever Windows recorded about the install."""
    stored = preferences()
    if stored.painter_executable:
        return bpy.path.abspath(stored.painter_executable)
    return painter_host.discover_executable()


def remember_painter_executable(path):
    stored = preferences()
    if path and not stored.painter_executable:
        stored.painter_executable = path
        LOG.info("learned where Painter lives: %s", path)


def _start_painter():
    """Start Painter if nobody is answering. Says why when it does not."""
    if painter_is_attached():
        return False
    if painter_host.is_running():
        raise RuntimeError("Painter is running but its RuriBridge plugin is off; switch "
                           "it on in Painter's Python menu")
    remember_painter_executable(painter_host.launch(
        painter_executable(), os.environ.get("RURI_BRIDGE_SESSION",
                                             arena_module.DEFAULT_SESSION)))
    return True


def _cascadeur_hint():
    stored = preferences()
    return bpy.path.abspath(stored.cascadeur_executable) if stored.cascadeur_executable else ""


class RuriBridgePreferences(bpy.types.AddonPreferences):
    """What is true of this computer, not of the file being worked on."""

    bl_idname = ADDON

    painter_executable: bpy.props.StringProperty(
        name="Painter",
        description="Substance 3D Painter, started when a mesh is sent and nobody is "
                    "answering. Filled from the Windows registry on its own",
        subtype="FILE_PATH", default="")
    textures_folder: bpy.props.StringProperty(
        name="Textures Folder",
        description="The folder beside the .blend where Painter writes the textures "
                    "and this document reads them",
        default="textures")
    cascadeur_executable: bpy.props.StringProperty(
        name="Cascadeur",
        description="Cascadeur, summoned to look at a published performance",
        subtype="FILE_PATH", default="")

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "painter_executable")
        row.operator(RURIBRIDGE_OT_locate_painter.bl_idname, text="", icon="VIEWZOOM")
        layout.prop(self, "textures_folder")
        row = layout.row(align=True)
        row.prop(self, "cascadeur_executable")
        row.operator(RURIBRIDGE_OT_locate_cascadeur.bl_idname, text="", icon="VIEWZOOM")


# -- operators ------------------------------------------------------------------------------

class RURIBRIDGE_OT_send_mesh(bpy.types.Operator):
    bl_idname = "ruri_bridge.send_mesh"
    bl_label = "Update Mesh"
    bl_description = ("Update only the mesh in Painter: every visible mesh is swapped in "
                      "under the layers Painter already has, and nothing else changes. A "
                      "Texture Set with layers that nothing here paints into stops the swap")

    def invoke(self, context, event):
        refresh_view()
        fresh = texture_sets_new_to_painter()
        if not fresh:
            return self.execute(context)
        return context.window_manager.invoke_confirm(
            self, event, title="Update Mesh",
            message=("Painter gets {0} new Texture Set(s) with nothing painted on them: {1}. "
                     "Faces that move into one leave their paint behind in the Texture Set "
                     "they came from".format(len(fresh), ", ".join(fresh))),
            confirm_text="Update")

    def execute(self, context):
        try:
            started = _start_painter()
            generation = send_mesh(context)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        say("sent the mesh{0}".format(
            "; Painter is starting and takes it as it opens" if started else ""))
        return {"FINISHED"}


def _scope(properties):
    return "the active material's Texture Set" if properties.selected else "every Texture Set"


def _scoped(operator, context, act):
    """Run one push or pull on every Texture Set, or the active material's, and say so."""
    try:
        texture_sets = [selected_texture_set(context)] if operator.selected else None
        line = act(context, texture_sets, texture_sets[0] if texture_sets else "every Texture Set")
    except Exception as error:
        operator.report({"ERROR"}, str(error))
        return {"CANCELLED"}
    say(line)
    return {"FINISHED"}


def _pushed_shader(context, texture_sets, named):
    rows, skipped = push_shader(context, texture_sets)
    line = "pushed the shader of {0} to Painter".format(
        named if texture_sets else "{0} Texture Set(s)".format(len(rows)))
    if skipped:
        line += "; skipped " + ", ".join("{0} ({1})".format(texture_set, why)
                                         for texture_set, why in sorted(skipped.items()))
    return line


def _pulled_shader(_context, texture_sets, named):
    pull_shader(texture_sets)
    return "pulling the shader of {0} from Painter".format(named)


def _pushed_textures(_context, texture_sets, named):
    push_textures(texture_sets)
    return "pushing the textures of {0} to Painter".format(named)


def _pulled_textures(_context, texture_sets, named):
    pull_textures(texture_sets)
    return "pulling the textures of {0} from Painter".format(named)


class RURIBRIDGE_OT_push_shader(bpy.types.Operator):
    bl_idname = "ruri_bridge.push_shader"
    bl_label = "Push Shader"
    selected: bpy.props.BoolProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return ("Put {0} in Painter on the shader of the material that paints it, with every "
                "parameter. The same shader by identity takes the whole row; another shader "
                "only the parameters both name".format(_scope(properties)))

    def execute(self, context):
        return _scoped(self, context, _pushed_shader)


class RURIBRIDGE_OT_pull_shader(bpy.types.Operator):
    bl_idname = "ruri_bridge.pull_shader"
    bl_label = "Pull Shader"
    selected: bpy.props.BoolProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return ("Write the shader parameters Painter holds for {0} into the material that "
                "paints it here".format(_scope(properties)))

    def execute(self, context):
        return _scoped(self, context, _pulled_shader)


class RURIBRIDGE_OT_push_textures(bpy.types.Operator):
    bl_idname = "ruri_bridge.push_textures"
    bl_label = "Push Textures"
    selected: bpy.props.BoolProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return ("Stand {0} up in Painter from the textures of the material that paints it, "
                "in a layer of its own under every other, where Painter runs the same "
                "shader".format(_scope(properties)))

    def execute(self, context):
        return _scoped(self, context, _pushed_textures)


class RURIBRIDGE_OT_pull_textures(bpy.types.Operator):
    bl_idname = "ruri_bridge.pull_textures"
    bl_label = "Pull Textures"
    selected: bpy.props.BoolProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return ("Pull what is painted in Painter on {0} into the materials painting it: "
                "exported into this document's textures folder and put into those "
                "materials".format(_scope(properties)))

    def execute(self, context):
        return _scoped(self, context, _pulled_textures)


class RURIBRIDGE_OT_pull_selected_layer(bpy.types.Operator):
    bl_idname = "ruri_bridge.pull_selected_layer"
    bl_label = "Pull Selected Layer"
    bl_description = ("Have Painter export only the layer selected there, on its own, "
                      "into this document's textures folder. It arrives as images and "
                      "is not put into any material")

    def execute(self, context):
        try:
            pull_selected_layer()
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        say("asked Painter for its selected layer")
        return {"FINISHED"}


_MATERIAL_ITEMS = []


def _material_items(_self, _context):
    """Every worn material, for the binding search. Kept alive on purpose: Blender
    reads enum strings after this returns, and a list it does not hold can be gone
    by then."""
    _MATERIAL_ITEMS[:] = [(row["name"], row["name"],
                           "{0} -- on {1}".format(row["texture_set"] or "not painted",
                                                 ", ".join(row["objects"][:3])))
                          for row in _view["materials"]]
    return _MATERIAL_ITEMS


class RURIBRIDGE_OT_bind(bpy.types.Operator):
    bl_idname = "ruri_bridge.bind"
    bl_label = "Painted By"
    bl_description = "Choose the material that paints into this Texture Set"
    bl_property = "material"

    texture_set: bpy.props.StringProperty()
    material: bpy.props.EnumProperty(items=_material_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            say(bind(self.texture_set, self.material))
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class RURIBRIDGE_OT_exclude(bpy.types.Operator):
    bl_idname = "ruri_bridge.exclude"
    bl_label = "Keep Out Of Painter"
    bl_description = "Keep this material's faces out of Painter, or let them back in"

    material: bpy.props.StringProperty()
    excluded: bpy.props.BoolProperty()

    @classmethod
    def description(cls, context, properties):
        if properties.excluded:
            return ("Keep {0}'s faces out of Painter: Update Mesh stops sending them. A "
                    "Texture Set that still has layers and nothing left painting into it "
                    "stops the swap until it is deleted in Painter or this is let back "
                    "in".format(properties.material))
        return "Let {0}'s faces back into Painter with the next Update Mesh".format(
            properties.material)

    def execute(self, context):
        try:
            exclude(self.material, self.excluded)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


_TARGET_ITEMS = []


def _target_items(_operator, context):
    """The UV maps every mesh wearing the active material's Texture Set has besides the
    one it renders with. Kept alive on purpose (see ``_material_items``)."""
    material = context.object.active_material if context.object is not None else None
    names = layout_retarget.target_layers(material) if material is not None else []
    _TARGET_ITEMS[:] = [(name, name, "Lay the Texture Set out as {0} holds it; {0} keeps the "
                                     "layout it has now".format(name)) for name in names]
    return _TARGET_ITEMS


class RURIBRIDGE_OT_retarget_layout(bpy.types.Operator):
    bl_idname = "ruri_bridge.retarget_layout"
    bl_label = "Retarget Layout"
    bl_description = ("Move the active material's Texture Set to the UV layout another UV map "
                      "of every mesh wearing it holds: the two maps swap on its faces, Painter "
                      "keeps every layer and lays its mesh maps out again, and the material's "
                      "own pictures are laid out again beside them")
    bl_property = "target"

    target: bpy.props.EnumProperty(items=_target_items)

    def execute(self, context):
        try:
            say(retarget_layout(context, self.target))
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class RURIBRIDGE_OT_follow_rename(bpy.types.Operator):
    bl_idname = "ruri_bridge.follow_rename"
    bl_label = "Rename Texture Set To Match"
    bl_description = ("This material was renamed here; rename its Texture Set in Painter "
                      "to match. Every layer stays")

    material: bpy.props.StringProperty()

    def execute(self, context):
        try:
            follow_rename(self.material)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


#: The texture nodes whose four lanes are open in Pull Mapping, by (material, node): how
#: the panel is folded, not a fact of the document, so it is written nowhere.
_open_lanes = set()
_MAP_ITEMS = []


def _map_items(operator, _context):
    """Painter's maps for the Texture Set the operator's material paints into: whole maps
    for a node, map components for a lane. Kept alive on purpose (see ``_material_items``)."""
    material = bpy.data.materials.get(operator.material)
    maps = painter_maps(mesh_publish.texture_set_of(material))[0] if material is not None else []
    if operator.lane < 0:
        items = [(NOTHING, "Not pulled", "This texture takes nothing from Painter")]
        items.extend((name, name, "All of {0}, unchanged".format(name)) for name in maps)
    else:
        items = [(NOTHING, "Keep its own", "This lane keeps what the texture has there")]
        items.extend(("{0}:{1}".format(name, index), "{0}.{1}".format(name, letter),
                      "Lane {0} of {1}".format(letter, name))
                     for name in maps for index, letter in enumerate(slot_compose.LANES))
    _MAP_ITEMS[:] = items
    return _MAP_ITEMS


class _OnTextureNode:
    material: bpy.props.StringProperty()
    node: bpy.props.StringProperty()
    lane: bpy.props.IntProperty(default=-1)


class RURIBRIDGE_OT_map_texture(_OnTextureNode, bpy.types.Operator):
    bl_idname = "ruri_bridge.map_texture"
    bl_label = "Take From Painter"
    bl_description = "Choose which of Painter's maps this texture takes, or this lane of it"
    bl_property = "choice"

    choice: bpy.props.EnumProperty(items=_map_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            map_texture(self.material, self.node, self.lane, self.choice)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _tag_redraw()
        return {"FINISHED"}


class RURIBRIDGE_OT_invert_lane(_OnTextureNode, bpy.types.Operator):
    bl_idname = "ruri_bridge.invert_lane"
    bl_label = "Invert Lane"
    bl_description = "Take one minus what this lane takes, as smoothness is of roughness"

    def execute(self, context):
        try:
            invert_lane(self.material, self.node, self.lane)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _tag_redraw()
        return {"FINISHED"}


class RURIBRIDGE_OT_open_lanes(_OnTextureNode, bpy.types.Operator):
    bl_idname = "ruri_bridge.open_lanes"
    bl_label = "Lanes"
    bl_description = "Show or hide this texture's four lanes"

    def execute(self, context):
        key = (self.material, self.node)
        if key in _open_lanes:
            _open_lanes.discard(key)
        else:
            _open_lanes.add(key)
        _tag_redraw()
        return {"FINISHED"}


class RURIBRIDGE_OT_reset_mapping(bpy.types.Operator):
    bl_idname = "ruri_bridge.reset_mapping"
    bl_label = "Land By Name"
    bl_description = ("Drop this material's table: Painter's maps land in the texture nodes "
                      "named after them again, and the missing ones are made")

    material: bpy.props.StringProperty()

    def execute(self, context):
        try:
            reset_mapping(self.material)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _tag_redraw()
        return {"FINISHED"}


class RURIBRIDGE_OT_start_painter(bpy.types.Operator):
    bl_idname = "ruri_bridge.start_painter"
    bl_label = "Start Painter"
    bl_description = "Start Substance 3D Painter; it attaches on its own"

    def execute(self, context):
        try:
            started = _start_painter()
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        say("starting Painter" if started else "Painter is already attached")
        return {"FINISHED"}


class RURIBRIDGE_OT_reattach(bpy.types.Operator):
    bl_idname = "ruri_bridge.reattach"
    bl_label = "Reattach"
    bl_description = "Attach to the bridge session again"

    def execute(self, context):
        try:
            CONNECTION.open(os.environ.get("RURI_BRIDGE_SESSION",
                                           arena_module.DEFAULT_SESSION))
            _start_timer()
            refresh_view(force=True)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class RURIBRIDGE_OT_locate_painter(bpy.types.Operator):
    bl_idname = "ruri_bridge.locate_painter"
    bl_label = "Find Painter"
    bl_description = "Read Painter's install path out of the Windows registry"

    def execute(self, context):
        found = painter_host.discover_executable()
        if found is None:
            self.report({"ERROR"}, "Windows has no record of a Painter install")
            return {"CANCELLED"}
        preferences().painter_executable = found
        self.report({"INFO"}, found)
        return {"FINISHED"}


class RURIBRIDGE_OT_locate_cascadeur(bpy.types.Operator):
    bl_idname = "ruri_bridge.locate_cascadeur"
    bl_label = "Find Cascadeur"
    bl_description = "Read Cascadeur's install path out of the Windows registry"

    def execute(self, context):
        found = summon_module.locate(peers_module.CASCADEUR)
        if not found:
            self.report({"WARNING"}, "Windows has no registration for cascadeur.exe")
            return {"CANCELLED"}
        preferences().cascadeur_executable = found
        self.report({"INFO"}, found)
        return {"FINISHED"}


def publish_animation(context):
    """Publish the performance on the selected rig, as a GLB carrying only it.

    Blender's own exporter writes it: a second writer for a format the
    application already writes is a second set of rounding. Only the active
    actions go -- a character file keeps every take it was given, and the thing
    being handed over is the one on the rig.
    """
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    publisher = CONNECTION.session.publisher(topic_module.ANIMATION)
    with publisher.staging() as staging:
        target = staging.path(record_module.SCENE_FILE_NAME)
        bpy.ops.export_scene.gltf(
            filepath=str(target), export_format="GLB", use_selection=True,
            export_animations=True, export_animation_mode="ACTIVE_ACTIONS",
            export_skins=True, export_yup=True, export_apply=False)
        if not target.exists():
            raise RuntimeError("the glTF exporter wrote nothing to {0}".format(target))
        return staging.publish(record_module.performance(
            HOST.name, record_module.SCENE_FILE_NAME, context.scene.name,
            frame_start=context.scene.frame_start, frame_end=context.scene.frame_end,
            fps=context.scene.render.fps))


def _summon_cascadeur():
    try:
        return " ".join(summon_module.summon(
            peers_module.CASCADEUR.name, hint=_cascadeur_hint()))
    except summon_module.SummonError as error:
        LOG.warning("could not summon Cascadeur: %s", error)
        return ""


class RURIBRIDGE_OT_send_animation(bpy.types.Operator):
    bl_idname = "ruri_bridge.send_animation"
    bl_label = "Send Rig And Animation"
    bl_description = "Publish the selected rig and what it is doing, then summon Cascadeur"

    def execute(self, context):
        try:
            generation = publish_animation(context)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        knocked = _summon_cascadeur()
        say("sent the rig and its animation, generation {0}{1}".format(
            generation.number, "; summoned Cascadeur" if knocked else ""))
        return {"FINISHED"}


class RURIBRIDGE_OT_fetch_animation(bpy.types.Operator):
    bl_idname = "ruri_bridge.fetch_animation"
    bl_label = "Fetch Animation"
    bl_description = ("Ask Cascadeur for what it has and summon it to answer; it lands "
                      "on the active armature")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def execute(self, context):
        try:
            generation = _ask(record_module.ASK_FOR_ANIMATION)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        knocked = _summon_cascadeur()
        say("asked Cascadeur, generation {0}{1}".format(
            generation.number, "; summoned it" if knocked else "; could not summon it"))
        return {"FINISHED"}


# -- the panel ----------------------------------------------------------------------------------

def _project_label(state):
    document = state.get("document") or ""
    return os.path.basename(document) if document else "no project open"


def _draw_status(layout):
    box = layout.box()
    if not CONNECTION.is_open:
        row = box.row()
        row.label(text="Not attached", icon="UNLINKED")
        row.operator(RURIBRIDGE_OT_reattach.bl_idname, text="", icon="FILE_REFRESH")
        return False
    if not painter_is_attached():
        row = box.row()
        row.label(text="Painter is not running", icon="UNLINKED")
        row.operator(RURIBRIDGE_OT_start_painter.bl_idname, text="", icon="PLAY")
        return True
    box.label(text="Painter: " + _project_label(painter_state()), icon="LINKED")
    return True


def _draw_table(layout):
    state = painter_state() if painter_is_attached() else {}
    sets = list(state.get("texture_sets") or [])
    rows = _view["materials"]
    by_set = {}
    for row in rows:
        if row["texture_set"]:
            by_set.setdefault(row["texture_set"], []).append(row["name"])
    if sets:
        box = layout.box()
        box.label(text="Texture Sets in Painter")
        for entry in sets:
            name = entry["name"]
            painters = by_set.get(name, [])
            line = box.row(align=True)
            orphan = entry.get("layers", 0) and not painters
            line.label(text=name, icon="ERROR" if orphan else "TEXTURE")
            label = ", ".join(painters) if painters else (
                "nothing paints it ({0} layers)".format(entry.get("layers", 0))
                if orphan else "nothing paints it")
            operator = line.operator(RURIBRIDGE_OT_bind.bl_idname, text=label,
                                     icon="DOWNARROW_HLT")
            operator.texture_set = name
            for painter in painters:
                operator = line.operator(RURIBRIDGE_OT_exclude.bl_idname, text="",
                                         icon="HIDE_OFF")
                operator.material, operator.excluded = painter, True
    known = {entry["name"] for entry in sets}
    fresh = [row for row in rows if row["texture_set"] and row["texture_set"] not in known]
    renamed = [row for row in rows if row["texture_set"] and row["texture_set"] != row["name"]
               and len(by_set.get(row["texture_set"], [])) == 1]
    kept_out = [row for row in rows if not row["texture_set"]]
    if fresh or renamed or kept_out:
        box = layout.box()
        box.label(text="Materials")
        for row in fresh:
            line = box.row(align=True)
            line.label(text=row["name"], icon="ADD")
            operator = line.operator(RURIBRIDGE_OT_exclude.bl_idname, text="",
                                     icon="HIDE_OFF")
            operator.material, operator.excluded = row["name"], True
        for row in renamed:
            line = box.row(align=True)
            line.label(text="{0} (Painter: {1})".format(row["name"], row["texture_set"]),
                       icon="SORTALPHA")
            operator = line.operator(RURIBRIDGE_OT_follow_rename.bl_idname, text="",
                                     icon="FILE_REFRESH")
            operator.material = row["name"]
        for row in kept_out:
            line = box.row(align=True)
            line.label(text=row["name"], icon="HIDE_ON")
            operator = line.operator(RURIBRIDGE_OT_exclude.bl_idname, text="",
                                     icon="HIDE_ON")
            operator.material, operator.excluded = row["name"], False
    if _view["bare"]:
        box = layout.box()
        box.label(text="No material, not sent")
        for name in _view["bare"]:
            box.label(text=name, icon="MESH_DATA")


class RURIBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "RuriBridge"
    bl_idname = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"

    def draw(self, context):
        layout = self.layout
        attached = _draw_status(layout)
        column = layout.column(align=True)
        column.enabled = attached
        column.scale_y = 1.3
        column.operator(RURIBRIDGE_OT_send_mesh.bl_idname, icon="EXPORT")
        for operator, icon in ((RURIBRIDGE_OT_push_shader, "MATERIAL"),
                               (RURIBRIDGE_OT_pull_shader, "NODE_MATERIAL"),
                               (RURIBRIDGE_OT_push_textures, "TEXTURE"),
                               (RURIBRIDGE_OT_pull_textures, "IMPORT")):
            verb, noun = operator.bl_label.split(" ", 1)
            row = column.row(align=True)
            row.operator(operator.bl_idname, text="{0} All {1}".format(verb, noun),
                         icon=icon).selected = False
            row.operator(operator.bl_idname,
                         text="{0} Selected {1}".format(verb, noun)).selected = True
        column.operator(RURIBRIDGE_OT_pull_selected_layer.bl_idname, icon="RENDERLAYERS")
        if attached:
            _draw_table(layout)
        if _status[0]:
            layout.label(text=_status[0], icon="INFO")


def _lane_text(lane):
    if not lane["map"]:
        return "keep"
    text = "{0}.{1}".format(lane["map"], slot_compose.LANES[lane["component"]])
    return "1 - " + text if lane["invert"] else text


def _node_text(lanes):
    whole_map = slot_compose.whole_map_of(lanes)
    if whole_map:
        return whole_map
    if not any(lane["map"] for lane in lanes):
        return "not pulled"
    return "  ".join("{0} {1}".format(letter, _lane_text(lane))
                     for letter, lane in zip(slot_compose.LANES, lanes) if lane["map"])


def _on_node(operator, material, node, lane=-1):
    operator.material, operator.node, operator.lane = material.name, node.name, lane


def _draw_mapping(layout, material, texture_set):
    """One material's table: each texture node, what it takes, and its lanes when open."""
    box = layout.box()
    header = box.row(align=True)
    header.label(text="{0} -> {1}".format(material.name, texture_set), icon="MATERIAL")
    try:
        table = texture_ingest.mapping_of(material)
    except RuntimeError as error:
        box.label(text=str(error), icon="ERROR")
        header.operator(RURIBRIDGE_OT_reset_mapping.bl_idname, text="",
                        icon="LOOP_BACK").material = material.name
        return
    if table:
        header.operator(RURIBRIDGE_OT_reset_mapping.bl_idname, text="",
                        icon="LOOP_BACK").material = material.name
    maps, why = painter_maps(texture_set)
    if why:
        box.label(text=why, icon="INFO")
    landing = {} if table else texture_ingest.landing_by_name(material, maps)
    nodes = texture_nodes(material)
    if not nodes:
        box.label(text="No Image Texture node: maps land by name, in nodes made for them",
                  icon="INFO")
    for node in nodes:
        if node.name in table:
            lanes = table[node.name]
        elif node.name in landing:
            lanes = slot_compose.whole(landing[node.name])
        else:
            lanes = [slot_compose.keep_lane() for _ in slot_compose.LANES]
        opened = (material.name, node.name) in _open_lanes
        line = box.row(align=True)
        _on_node(line.operator(RURIBRIDGE_OT_open_lanes.bl_idname, text="", emboss=False,
                               icon="DOWNARROW_HLT" if opened else "RIGHTARROW"), material, node)
        line.label(text=node.label or node.name, icon="IMAGE_DATA")
        text = _node_text(lanes)
        if node.name in landing:
            text = "by name: " + text
        _on_node(line.operator(RURIBRIDGE_OT_map_texture.bl_idname, text=text,
                               icon="DOWNARROW_HLT"), material, node)
        if not opened:
            continue
        for index, letter in enumerate(slot_compose.LANES):
            lane_line = box.row(align=True)
            lane_line.separator(factor=3.0)
            lane_line.label(text=letter)
            _on_node(lane_line.operator(RURIBRIDGE_OT_map_texture.bl_idname,
                                        text=_lane_text(lanes[index])), material, node, index)
            _on_node(lane_line.operator(RURIBRIDGE_OT_invert_lane.bl_idname, text="",
                                        icon="ARROW_LEFTRIGHT", depress=lanes[index]["invert"]),
                     material, node, index)


class RURIBRIDGE_PT_pull_mapping(bpy.types.Panel):
    bl_label = "Pull Mapping"
    bl_idname = "RURIBRIDGE_PT_pull_mapping"
    bl_parent_id = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        rows = [row for row in _view["materials"] if row["texture_set"]]
        generated = [row for row in rows if row["shader"]]
        if generated:
            layout.label(text="{0} generated material(s) take Painter's maps one to one".format(
                len(generated)), icon="CHECKMARK")
        for row in rows:
            material = bpy.data.materials.get(row["name"])
            if row["shader"] or material is None:
                continue
            _draw_mapping(layout, material, row["texture_set"])


def _chart_text(chart):
    return chart[:8] if chart else "as first sent"


class RURIBRIDGE_PT_layout(bpy.types.Panel):
    bl_label = "UV Layout"
    bl_idname = "RURIBRIDGE_PT_layout"
    bl_parent_id = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"

    def draw(self, context):
        layout = self.layout
        material = context.object.active_material if context.object is not None else None
        if material is None:
            layout.label(text="No active material", icon="INFO")
            return
        texture_set = mesh_publish.texture_set_of(material)
        if not texture_set:
            layout.label(text="{0} is kept out of Painter".format(material.name), icon="INFO")
            return
        box = layout.box()
        box.label(text="{0} -> {1}".format(material.name, texture_set), icon="MATERIAL")
        try:
            table = layouts.table_of(material)
        except Exception as error:
            box.label(text=str(error), icon="ERROR")
            return
        box.label(text="Render UV map: layout {0}".format(_chart_text(table["layout"])),
                  icon="UV")
        for index, entry in sorted(table["extra"].items(), key=lambda item: int(item[0])):
            box.label(text="UV set {0}: {1} holds layout {2}".format(
                index, entry["layer"], _chart_text(entry["chart"])), icon="UV_DATA")
        if texture_set in layout_retarget.waiting():
            layout.label(text="Waiting for Painter", icon="SORTTIME")
            return
        row = layout.row()
        row.enabled = CONNECTION.is_open and painter_is_attached()
        row.operator_menu_enum(RURIBRIDGE_OT_retarget_layout.bl_idname, "target",
                               text="Retarget Layout To", icon="UV_SYNC_SELECT")


class RURIBRIDGE_PT_cascadeur(bpy.types.Panel):
    bl_label = "Cascadeur"
    bl_idname = "RURIBRIDGE_PT_cascadeur"
    bl_parent_id = "RURIBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RuriBridge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        column = self.layout.column(align=True)
        column.enabled = CONNECTION.is_open
        column.operator(RURIBRIDGE_OT_send_animation.bl_idname, icon="ACTION")
        column.operator(RURIBRIDGE_OT_fetch_animation.bl_idname, icon="IMPORT")


_CLASSES = (RuriBridgePreferences,
            RURIBRIDGE_OT_send_mesh, RURIBRIDGE_OT_push_shader, RURIBRIDGE_OT_pull_shader,
            RURIBRIDGE_OT_push_textures, RURIBRIDGE_OT_pull_textures,
            RURIBRIDGE_OT_pull_selected_layer, RURIBRIDGE_OT_bind, RURIBRIDGE_OT_exclude,
            RURIBRIDGE_OT_follow_rename, RURIBRIDGE_OT_retarget_layout,
            RURIBRIDGE_OT_map_texture, RURIBRIDGE_OT_invert_lane,
            RURIBRIDGE_OT_open_lanes, RURIBRIDGE_OT_reset_mapping,
            RURIBRIDGE_OT_start_painter, RURIBRIDGE_OT_reattach,
            RURIBRIDGE_OT_locate_painter, RURIBRIDGE_OT_locate_cascadeur,
            RURIBRIDGE_OT_send_animation, RURIBRIDGE_OT_fetch_animation,
            RURIBRIDGE_PT_panel, RURIBRIDGE_PT_layout, RURIBRIDGE_PT_pull_mapping,
            RURIBRIDGE_PT_cascadeur)


def register():
    log_module.install_stream_sink()
    for entry in _CLASSES:
        bpy.utils.register_class(entry)
    if _ruri_bridge_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_ruri_bridge_after_load)
    try:
        CONNECTION.open(os.environ.get("RURI_BRIDGE_SESSION", arena_module.DEFAULT_SESSION),
                        os.environ.get("RURI_BRIDGE_ROOT") or None)
        _start_timer()
    except Exception as error:
        LOG.error("could not attach on start: %s", error)


def unregister():
    _stop_timer()
    if _ruri_bridge_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_ruri_bridge_after_load)
    CONNECTION.close()
    for entry in reversed(_CLASSES):
        bpy.utils.unregister_class(entry)
