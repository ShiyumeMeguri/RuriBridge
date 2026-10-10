# -*- coding: utf-8 -*-
"""RuriBridge -- the Substance 3D Painter end.

Painter is where textures are painted, so this end does these things and shows
one table:

* **Update Mesh** -- Blender sends the surface, and it is swapped in under every
  layer the project already has; nothing else changes. A Texture Set with layers
  that nothing in the payload paints into stops the swap instead of being dropped.
* **Pull / Push Shader** -- every Texture Set, or only the selected one, takes the
  shader of the Blender material that paints it with every parameter; or gives its
  parameters back to that material.
* **Pull / Push Textures** -- every Texture Set, or only the selected one, is stood up
  from the textures of the Blender material that paints it, where it runs the same
  shader; or is exported into the Blender document's own textures folder, and Blender
  puts the files into its materials. **Push Selected Layer** is Blender's Pull Selected
  Layer. Each is the same as its opposite pressed in Blender.
* **Pull Into Selected Layer** -- one of the images a Blender material samples
  becomes the mask, or the reference fill, of the layer selected here. Blender has
  no layers, so that is the only shape anything coming this way can take, and it
  touches nothing but the selected layer.
* **The table** says which Blender material paints into each Texture Set, and is
  edited from either side.

Nothing is sent on its own; a timer only reads a few integers out of the mapped
control block and answers what was asked.
"""

import ctypes
import os
import time

from PySide6 import QtCore, QtGui, QtWidgets

import substance_painter.event
import substance_painter.exception
import substance_painter.layerstack
import substance_painter.logging
import substance_painter.project
import substance_painter.resource
import substance_painter.textureset
import substance_painter.ui

from ...Kernel import arena as arena_module
from ...Kernel import host as host_port
from ...Kernel import log as log_module
from ...Kernel import peers as peers_module
from ...Kernel import record as record_module
from ...Kernel import session as session_module
from ...Kernel import topic as topic_module

from . import (guest_state, held_imports, layout_state, material_seed, mesh_ingest, project_imports,
               shader_state, texture_publish)

LOG = log_module.logger("painter")

SESSION_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_SESSION"
ROOT_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_ROOT"

#: Painter holds a lock on the project while it writes it out and answers nothing
#: while it does; an autosave takes it on an ordinary tick with no warning, and
#: the lock has no query of its own -- the only way to learn is the exception.
PROJECT_LOCKED_PHRASE = "locked"

#: How often the bridge looks at the mapped control block. An idle tick reads a
#: dozen integers out of pages this process already has mapped.
POLL_MILLISECONDS = 100
DEFAULT_TEXTURE_RESOLUTION = 2048
#: A mesh load that never says it finished must not hold the channel shut for the
#: rest of the session.
MESH_LOAD_DEADLINE_SECONDS = 600.0

PEER = peers_module.SUBSTANCE
BLENDER = peers_module.BLENDER

_SEVERITY = {
    "DEBUG": substance_painter.logging.DBG_INFO,
    "INFO": substance_painter.logging.INFO,
    "WARNING": substance_painter.logging.WARNING,
    "ERROR": substance_painter.logging.ERROR,
    "CRITICAL": substance_painter.logging.ERROR,
}


def _emit_to_painter(level_name, category, message):
    substance_painter.logging.log(
        _SEVERITY.get(level_name, substance_painter.logging.INFO),
        "RuriBridge/" + category, message)


def project_is_locked(error):
    return (isinstance(error, substance_painter.exception.ProjectError)
            and PROJECT_LOCKED_PHRASE in str(error).lower())


def package_root():
    """Where the package this leg came from actually is, junction resolved."""
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.dirname(os.path.dirname(here))


class SubstanceHost(host_port.Host):
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
        QtCore.QTimer.singleShot(int(seconds * 1000.0), function)

    def redraw(self):
        if _panel is not None:
            _panel.refresh()

    def receive(self, topic, generation):
        return _handle(topic, generation)

    def collect(self, topic):
        if topic is topic_module.PRESENCE:
            return texture_publish.current_project_state()
        return None


HOST = host_port.bind(SubstanceHost())


class _Connection:
    """The one live attachment this Painter process holds."""

    def __init__(self):
        self.session = None
        self.peers = {}

    @property
    def is_open(self):
        return self.session is not None

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


def executable_path():
    """This process's own image path, so Blender can start Painter later."""
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer))
    return buffer.value if length else ""


def blender_state():
    return CONNECTION.peers.get(BLENDER.name) or {}


def blender_is_attached():
    return CONNECTION.is_open and CONNECTION.session.present(BLENDER.name)


#: This project as last stated: read when something changed it, never per tick.
#: Counting layers walks every node of every stack, and a character's worth of
#: Texture Sets is a thousand calls into the application.
_own = [{}]


def publish_presence():
    _own[0] = texture_publish.current_project_state()
    _own[0]["host_executable"] = executable_path()
    if not CONNECTION.is_open:
        return None
    return CONNECTION.session.writer(topic_module.PRESENCE).write(_own[0])


def _ask(what, **details):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    return CONNECTION.session.publisher(topic_module.REQUEST).publish_record(
        record_module.request(HOST.name, what, **details))


def speakers(rows):
    """The Blender material that speaks for each Texture Set its generated materials paint:
    the one named like the set, else the first by name -- the rule the shading row follows."""
    found = {}
    for row in sorted(rows, key=lambda one: one["name"]):
        texture_set = row["texture_set"]
        if not texture_set or not row["shader"]:
            continue
        if texture_set not in found or row["name"] == texture_set:
            found[texture_set] = row
    return found


def shaders_by_texture_set(rows):
    """Which generated shader each Texture Set's Blender materials run, and its identity."""
    return {texture_set: {"name": row["shader"], "identity": row["identity"]}
            for texture_set, row in speakers(rows).items()}


def selected_texture_set():
    """The Texture Set selected here -- the one whose layers are showing."""
    return substance_painter.textureset.get_active_stack().material().name


def _texture_sets(names):
    """These Texture Sets by name, or every one this project has when none are named."""
    if names is None:
        return sorted(one.name for one in substance_painter.textureset.all_texture_sets())
    return list(names)


def _say_skipped(what, skipped):
    for texture_set, why in sorted(skipped.items()):
        LOG.warning("%s: %s skipped: %s", texture_set, what, why)


def _named(texture_sets):
    return texture_sets[0] if len(texture_sets) == 1 else "{0} Texture Set(s)".format(
        len(texture_sets))


def _with_skipped(line, skipped):
    if skipped:
        line += "; skipped " + ", ".join("{0} ({1})".format(texture_set, why)
                                         for texture_set, why in sorted(skipped.items()))
    return line


def pull_shader(texture_sets=None):
    """Pull the shader of the Blender material painting every Texture Set here, or these,
    with every parameter. Blender's Push Shader does exactly this. Returns the Texture
    Sets asked about, and the ones that cannot be, with why."""
    painted = speakers(blender_state().get("materials") or [])
    asked, skipped = [], {}
    for texture_set in _texture_sets(texture_sets):
        if texture_set in painted:
            asked.append(texture_set)
        else:
            skipped[texture_set] = "no Blender material on a generated shader paints into it"
    _say_skipped("shader", skipped)
    if not asked:
        raise RuntimeError("no shader to pull: " + "; ".join(
            "{0}: {1}".format(texture_set, why) for texture_set, why in sorted(skipped.items())))
    _ask(record_module.ASK_FOR_SHADING, texture_sets=asked)
    return asked, skipped


def push_shader(texture_sets=None):
    """Push the shader parameters every Texture Set here, or these, holds into the Blender
    materials painting them. Blender's Pull Shader asks for exactly this. Returns the
    Texture Sets pushed."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    layout = shader_state.Layout()
    values, shaders, identities = {}, {}, {}
    for texture_set in _texture_sets(texture_sets):
        identifier = layout.instance_by_texture_set.get(texture_set)
        if identifier is None:
            continue
        shader = layout.shader_by_instance.get(identifier, "")
        manifest = shader_state.shader_manifest(shader) if shader else None
        values[texture_set] = layout.holds(identifier)
        shaders[texture_set] = shader
        identities[texture_set] = str((manifest or {}).get("identity") or "")
    if not values:
        raise RuntimeError("none of those Texture Sets has a shader instance")
    CONNECTION.session.writer(topic_module.SHADING).write(record_module.shading(
        HOST.name, values, vocabulary_by_texture_set=shaders, shader_name_by_texture_set=shaders,
        identity_by_texture_set=identities))
    return sorted(values)


def _stand_up_requests(texture_sets, kinds):
    """What to ask Blender for to stand every Texture Set here, or these, up from its
    material: the inputs of these kinds, per Texture Set; and the ones that cannot be,
    with why."""
    rows = speakers(blender_state().get("materials") or [])
    requests, skipped = [], {}
    for texture_set in _texture_sets(texture_sets):
        row = rows.get(texture_set)
        if row is None:
            skipped[texture_set] = "no Blender material on a generated shader paints into it"
            continue
        manifest = shader_state.shader_manifest(row["shader"])
        if manifest is None:
            skipped[texture_set] = "{0} is not on this shelf".format(row["shader"])
            continue
        if str(manifest.get("identity") or "") != row["identity"]:
            skipped[texture_set] = "{0} on this shelf is another generation than {1}'s".format(
                row["shader"], row["name"])
            continue
        jobs = material_seed.jobs(manifest, row["images"], kinds)
        if not jobs:
            skipped[texture_set] = "{0} holds none of the textures {1} reads".format(
                row["name"], row["shader"])
            continue
        requests.append({"name": texture_set, "material": row["name"],
                         "shader": row["shader"], "jobs": jobs})
    return requests, skipped


def pull_textures(texture_sets=None):
    """Pull the Blender materials' own textures into every Texture Set here, or these: each
    stood up from its material where this shelf has that very shader (``material_seed``).
    Blender's Push Textures asks for exactly this. Returns the Texture Sets asked about,
    and the ones that cannot be, with why."""
    requests, skipped = _stand_up_requests(texture_sets, material_seed.FED_KINDS)
    _say_skipped("textures", skipped)
    if not requests:
        raise RuntimeError("no textures to pull: " + "; ".join(
            "{0}: {1}".format(texture_set, why) for texture_set, why in sorted(skipped.items())))
    _ask(record_module.ASK_FOR_INPUTS, texture_sets=requests)
    return [request["name"] for request in requests], skipped


def take_inputs(generation):
    """Put a delivery of cut textures in place. Returns one line about it."""
    reports = {}
    failed = {}
    for entry in generation.record["texture_sets"]:
        try:
            reports[entry["name"]] = material_seed.apply(entry, str(generation.directory))
        except Exception as error:
            LOG.error("%s could not be set up from Blender: %s", entry["name"], error)
            failed[entry["name"]] = str(error)
    channels = sum(len(report["channels"]) for report in reports.values())
    mesh_maps = sum(len(report["mesh_maps"]) for report in reports.values())
    parameters = sum(len(report["parameters"]) for report in reports.values())
    missing = sum(len(report["missing"]) for report in reports.values())
    line = ("set up {0} Texture Set(s) from Blender: {1} channel(s) in the Blender layer, "
            "{2} mesh map(s), {3} shader texture(s)".format(len(reports), channels, mesh_maps,
                                                            parameters))
    if missing:
        line += "; {0} input(s) not set (see the log)".format(missing)
    if failed:
        line += "; failed: " + ", ".join(sorted(failed))
    return line


def push_textures(layer=False, directory=None, texture_sets=None):
    """Push what is painted here into the Blender materials painting it: every Texture
    Set, or these by name, exported into the folder Blender named; or the layer selected
    here alone, which Blender keeps out of its materials. Blender's Pull Textures asks for
    exactly this."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    blender = blender_state()
    target = directory or blender.get("textures_directory") or ""
    if not target:
        raise RuntimeError("Blender has not said where its textures live; save the "
                           ".blend and keep it attached")
    return texture_publish.publish(
        CONNECTION.session.publisher(topic_module.TEXTURES), target,
        texture_publish.DEFAULT_PRESET_NAME, layer=layer,
        shaders=shaders_by_texture_set(blender.get("materials") or []),
        texture_sets=(None if texture_sets is None else
                      [substance_painter.textureset.TextureSet.from_name(name)
                       for name in texture_sets]))


# -- pulling an image into the selected layer ----------------------------------------

_IMPORTED = {}


def _resource_for(path):
    """The project resource for an image file as it is on disk now.

    Imported once per version of the file: an image Blender saved again since the
    last pull is a new picture, and handing back the resource of the old one would
    pull what was there before without a word. One a save took out because nothing
    used it any more is imported again.
    """
    status = os.stat(path)
    key = (os.path.normcase(os.path.abspath(path)), status.st_mtime_ns, status.st_size)
    known = _IMPORTED.get(key)
    if known is not None and substance_painter.resource.Resource.retrieve(known):
        return known
    resource = project_imports.take_in(path, substance_painter.resource.Usage.TEXTURE)
    _IMPORTED[key] = resource.identifier()
    return _IMPORTED[key]


def images_for_active_texture_set():
    """The images Blender's materials sample, for the Texture Set being painted."""
    if not substance_painter.project.is_open():
        return {}
    active = substance_painter.textureset.get_active_stack().material().name
    found = {}
    for row in blender_state().get("materials") or []:
        if row.get("texture_set") != active:
            continue
        for slot, path in sorted((row.get("images") or {}).items()):
            found["{0} / {1}".format(row["name"], slot)] = path
    return found


def _selection(stack):
    """The one node selected in this stack, or None when it is not exactly one."""
    selected = substance_painter.layerstack.get_selected_nodes(stack)
    return selected[0] if len(selected) == 1 else None


def _new_layer(stack, near, path):
    """A fresh fill layer for an image, above the selected layer (on top when none is),
    showing nothing but base colour -- so it covers no channel of the layers below
    with a default nobody chose -- and selected, so the next pull lands in it."""
    layerstack = substance_painter.layerstack
    position = (layerstack.InsertPosition.above_node(near)
                if isinstance(near, layerstack.LayerNode)
                else layerstack.InsertPosition.from_textureset_stack(stack))
    layer = layerstack.insert_fill(position)
    layer.set_name(os.path.splitext(os.path.basename(path))[0])
    layer.active_channels = {substance_painter.textureset.ChannelType.BaseColor}
    layerstack.set_selected_nodes([layer])
    return layer


def pull_into_selected_layer(path, as_mask):
    """Put one Blender image into the selected layer: its mask, or its fill source.

    Nothing else in the stack is touched. As a mask, the layer gets a mask if it
    has none, and the image fills it; a fill effect already selected in a mask has
    its source replaced instead. As a reference, the selected fill layer's base
    colour comes from the image. When what is selected cannot take the image that
    way -- nothing, several things, a paint layer for a reference -- a new fill
    layer takes it instead.
    """
    layerstack = substance_painter.layerstack
    stack = substance_painter.textureset.get_active_stack()
    node = _selection(stack)
    resource = _resource_for(path)
    image = os.path.basename(path)
    if as_mask:
        if isinstance(node, layerstack.FillEffectNode) and node.is_in_mask_stack():
            node.set_source(None, resource)
            return "the selected mask fill now reads {0}".format(image)
        made = not isinstance(node, layerstack.LayerNode)
        if made:
            node = _new_layer(stack, node, path)
        if not node.has_mask():
            node.add_mask(layerstack.MaskBackground.Black)
        fill = layerstack.insert_fill(
            layerstack.InsertPosition.inside_node(node, layerstack.NodeStack.Mask))
        fill.set_source(None, resource)
        return "{0}{1} is now masked by {2}".format(
            "new layer " if made else "", node.get_name(), image)
    if isinstance(node, layerstack.FillLayerNode):
        node.set_source(substance_painter.textureset.ChannelType.BaseColor, resource)
        return "{0} now shows {1}".format(node.get_name(), image)
    layer = _new_layer(stack, node, path)
    layer.set_source(substance_painter.textureset.ChannelType.BaseColor, resource)
    return "new layer {0} shows {1}".format(layer.get_name(), image)


# -- the panel -------------------------------------------------------------------------

def _panel_icon():
    """A tab-strip icon, which is also the only way back to a closed dock."""
    size = 64
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setBrush(QtGui.QColor(70, 130, 200))
    painter.setPen(QtCore.Qt.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 12, 12)
    font = painter.font()
    font.setPixelSize(34)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QtGui.QColor(255, 255, 255))
    painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, "RB")
    painter.end()
    return QtGui.QIcon(pixmap)


class RuriBridgePanel(QtWidgets.QWidget):
    """The dock: the table, three requests, the image pull and a status line."""

    def __init__(self):
        super().__init__()
        self.setObjectName("RuriBridgePanel")
        self.setWindowTitle("RuriBridge")
        self.setWindowIcon(_panel_icon())
        layout = QtWidgets.QVBoxLayout(self._themed_body())
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.link_label = QtWidgets.QLabel("")
        self.link_label.setWordWrap(True)
        layout.addWidget(self.link_label)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Texture Set", "Layers", "Painted by (Blender)", "Drop"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(self.table, 1)

        self.mesh_button = QtWidgets.QPushButton("Update Mesh")
        self.mesh_button.setToolTip(
            "Swap Blender's mesh in under the layers the project already has; nothing else "
            "changes")
        self.push_layer_button = QtWidgets.QPushButton("Push Selected Layer")
        self.push_layer_button.setToolTip(
            "Only the selected layer goes to Blender, as images kept out of the materials")
        layout.addWidget(self.mesh_button)
        actions = (
            ("Pull {0} Shader", "{0} takes the shader of the Blender material that paints it, "
                                "with every parameter", self._pull_shader),
            ("Push {0} Shader", "{0}'s shader parameters go into the Blender material that "
                                "paints it", self._push_shader),
            ("Pull {0} Textures", "{0} is stood up from the textures of the Blender material "
                                  "that paints it, in a layer of its own under every other",
             self._pull_textures),
            ("Push {0} Textures", "{0}'s paint goes into the Blender materials painting it",
             self._push_textures),
        )
        for label, tip, act in actions:
            pair = QtWidgets.QHBoxLayout()
            for scope, subject, selected_only in (("All", "Every Texture Set", False),
                                                  ("Selected", "The selected Texture Set", True)):
                button = QtWidgets.QPushButton(label.format(scope))
                button.setToolTip(tip.format(subject))
                button.clicked.connect(
                    lambda _checked=False, act=act, selected_only=selected_only: act(selected_only))
                pair.addWidget(button)
            layout.addLayout(pair)
        layout.addWidget(self.push_layer_button)

        pull = QtWidgets.QHBoxLayout()
        self.image_box = QtWidgets.QComboBox()
        self.image_box.setToolTip("An image a Blender material painting into this Texture "
                                  "Set samples")
        self.mask_button = QtWidgets.QPushButton("As Mask")
        self.reference_button = QtWidgets.QPushButton("As Reference")
        pull.addWidget(self.image_box, 1)
        pull.addWidget(self.mask_button)
        pull.addWidget(self.reference_button)
        layout.addWidget(QtWidgets.QLabel("Pull a Blender image into the selected layer"))
        layout.addLayout(pull)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.mesh_button.clicked.connect(self._ask_for_mesh)
        self.push_layer_button.clicked.connect(lambda: self._push_textures(False, layer=True))
        self.mask_button.clicked.connect(lambda: self._pull_image(True))
        self.reference_button.clicked.connect(lambda: self._pull_image(False))
        self._shown = None

    def _themed_body(self):
        """The widget the controls live in, chosen so Painter's own theme paints it.

        Painter themes through an application stylesheet rather than the palette.
        A bare QWidget matches nothing in it and the dock's accent shows through;
        a scroll area does match, so it paints Painter's panel grey and follows
        the theme without a colour written down here.
        """
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QtWidgets.QWidget()
        scroll.setWidget(body)
        return body

    def set_status(self, message):
        self.status_label.setText(message)
        LOG.info(message)

    def refresh(self):
        """Redraw the table, but only when what it shows changed."""
        blender = blender_state() if blender_is_attached() else {}
        own = _own[0]
        materials = list(blender.get("materials") or [])
        shown = (repr(own.get("texture_sets")), repr(
            [(row["name"], row["texture_set"]) for row in materials]),
            blender.get("document"))
        if CONNECTION.is_open:
            self.link_label.setText("Blender: {0}".format(
                os.path.basename(blender.get("document") or "") or "attached, file unsaved")
                if blender else "Blender is not attached")
        else:
            self.link_label.setText("Not attached to a bridge session")
        self._refresh_images()
        if shown == self._shown:
            return
        self._shown = shown
        painters = {}
        for row in materials:
            if row["texture_set"]:
                painters.setdefault(row["texture_set"], []).append(row["name"])
        names = [""] + sorted(row["name"] for row in materials)
        sets = list(own.get("texture_sets") or [])
        self.table.setRowCount(len(sets))
        for index, entry in enumerate(sets):
            painted = painters.get(entry["name"], [])
            name_item = QtWidgets.QTableWidgetItem(entry["name"])
            if entry["layers"] and not painted:
                name_item.setForeground(QtGui.QColor(230, 120, 90))
                name_item.setToolTip("Has layers and nothing in Blender paints into it; the "
                                     "next mesh would stop here until it is bound")
            self.table.setItem(index, 0, name_item)
            self.table.setItem(index, 1, QtWidgets.QTableWidgetItem(str(entry["layers"])))
            box = QtWidgets.QComboBox()
            box.blockSignals(True)
            box.addItems(names)
            if painted:
                box.setCurrentText(painted[0])
                box.setToolTip("Painted by " + ", ".join(painted))
            box.blockSignals(False)
            box.currentTextChanged.connect(
                lambda chosen, texture_set=entry["name"]: self._bind(texture_set, chosen))
            self.table.setCellWidget(index, 2, box)
            if entry["layers"] and not painted:
                drop = QtWidgets.QCheckBox()
                drop.setToolTip("Let the next mesh swap drop this Texture Set and its "
                                "{0} layers. Nothing is dropped unless this is "
                                "ticked".format(entry["layers"]))
                drop.setChecked(mesh_ingest.drop_allowed(entry["name"]))
                drop.toggled.connect(
                    lambda allowed, texture_set=entry["name"]:
                    mesh_ingest.allow_dropping(texture_set, allowed))
                self.table.setCellWidget(index, 3, drop)
            else:
                self.table.removeCellWidget(index, 3)
        self.table.resizeColumnsToContents()

    def _refresh_images(self):
        try:
            images = images_for_active_texture_set()
        except Exception:
            images = {}
        current = self.image_box.currentText()
        labels = sorted(images)
        if labels == [self.image_box.itemText(index) for index in range(self.image_box.count())]:
            return
        self.image_box.clear()
        for label in labels:
            self.image_box.addItem(label, images[label])
        if current in labels:
            self.image_box.setCurrentText(current)

    def _bind(self, texture_set, material):
        if not material:
            return
        try:
            _ask(record_module.ASK_TO_BIND, texture_set=texture_set, material=material)
        except Exception as error:
            self.set_status("could not bind: {0}".format(error))
            return
        self.set_status("asked Blender to paint {0} with {1}".format(texture_set, material))

    def _ask_for_mesh(self):
        try:
            _ask(record_module.ASK_FOR_MESH)
        except Exception as error:
            self.set_status("could not ask: {0}".format(error))
            return
        self.set_status("asked Blender for the mesh")

    @staticmethod
    def _scope(selected_only):
        return [selected_texture_set()] if selected_only else None

    def _pull_shader(self, selected_only):
        try:
            asked, skipped = pull_shader(self._scope(selected_only))
        except Exception as error:
            self.set_status("could not pull the shader: {0}".format(error))
            return
        self.set_status(_with_skipped(
            "pulling the shader of {0} from Blender".format(_named(asked)), skipped))

    def _push_shader(self, selected_only):
        try:
            pushed = push_shader(self._scope(selected_only))
        except Exception as error:
            self.set_status("could not push the shader: {0}".format(error))
            return
        self.set_status("pushed the shader of {0} to Blender".format(_named(pushed)))

    def _pull_textures(self, selected_only):
        try:
            asked, skipped = pull_textures(self._scope(selected_only))
        except Exception as error:
            self.set_status("could not pull the textures: {0}".format(error))
            return
        self.set_status(_with_skipped(
            "pulling the textures of {0} from Blender".format(_named(asked)), skipped))

    def _push_textures(self, selected_only, layer=False):
        try:
            texture_sets = None if layer else self._scope(selected_only)
            push_textures(layer=layer, texture_sets=texture_sets)
        except Exception as error:
            LOG.error("push failed: %s", error)
            self.set_status("could not push: {0}".format(error))
            return
        self.set_status("pushed {0} to Blender".format(
            "the selected layer" if layer else
            "the textures of " + (texture_sets[0] if texture_sets else "every Texture Set")))

    def _pull_image(self, as_mask):
        path = self.image_box.currentData()
        if not path:
            self.set_status("no Blender image to pull for this Texture Set")
            return
        try:
            self.set_status(pull_into_selected_layer(path, as_mask))
        except Exception as error:
            self.set_status("could not pull: {0}".format(error))


_panel = None
_dock = None
_timer = None
_log_handler = None
_menu_action = None
#: When a mesh that is still loading stops holding the channel shut, at the latest.
_mesh_deadline = [None]
#: Something changed what this project is at a moment it could not be asked about
#: it -- a save holds a lock -- so the next tick that can says it.
_presence_due = [False]


def apply_shading(record):
    """Put Blender's shading on the Texture Sets it names: the shader and every parameter,
    and nothing else. Returns one line about it."""
    if not substance_painter.project.is_open():
        return "a material arrived and no project is open"
    report = shader_state.apply_by_texture_set(
        record.get("by_texture_set") or {},
        vocabulary_by_texture_set=record.get("vocabulary_by_texture_set"),
        shader_name_by_texture_set=record.get("shader_name_by_texture_set"),
        identity_by_texture_set=record.get("identity_by_texture_set"))
    written = sum(len(names) for names in report["applied"].values())
    parts = ["{0} Texture Set(s) on their own shader, {1} on same-named parameters only; "
             "{2} value(s) written".format(len(report["same_shader"]),
                                           len(report["same_names_only"]), written)]
    if report["no_shader"]:
        parts.append("not on this shelf: " + ", ".join(sorted(report["no_shader"])))
    if report["unmapped"]:
        parts.append("no Texture Set here for " + ", ".join(report["unmapped"]))
    if report["mismatched"]:
        parts.append("{0} value(s) of the wrong type".format(
            sum(len(names) for names in report["mismatched"].values())))
    for texture_set, why in sorted(report["same_names_only"].items()):
        LOG.info("%s takes same-named parameters only: %s", texture_set, why)
    for texture_set, problems in sorted(report["mismatched"].items()):
        LOG.warning("%s: not written, %s", texture_set, "; ".join(problems))
    return "; ".join(parts)


def _mesh_finished(status=None, applied=None):
    _mesh_deadline[0] = None
    _presence_due[0] = True
    if applied is None and status is not None and status != str(substance_painter.project.ReloadMeshStatus.SUCCESS):
        _panel.set_status(status)
    if applied is not None and applied.relaid:
        _follow_layouts(applied)


def _follow_layouts(applied):
    """After a layout change, the shader textures of the bridge's own stand-up of a
    material are taken again from Blender, where the material's pictures were laid out
    again with the surface: a shader reads its textures in the Texture Set's layout, and
    Painter cannot hand over a texture's pixels to lay them out again itself. The rest of
    the stand-up stays as it is, read through the old layout like any other fill."""
    material_seed.follow_mesh_maps(applied.replaced)
    stood_up = material_seed.seeded(applied.relaid)
    if not stood_up:
        return
    requests, skipped = _stand_up_requests(stood_up, material_seed.SHADER_KINDS)
    if skipped:
        LOG.info("no shader textures to take again for %s", ", ".join(
            "{0} ({1})".format(texture_set, why) for texture_set, why in sorted(skipped.items())))
    if not requests:
        return
    _ask(record_module.ASK_FOR_INPUTS, texture_sets=requests)
    _panel.set_status("taking the shader textures of {0} again from Blender for the new "
                      "layout".format(_named([request["name"] for request in requests])))


def _handle(topic, generation):
    """Apply one arrival. Returns True when Painter is now busy with it."""
    if topic is topic_module.MESH:
        _mesh_deadline[0] = time.monotonic() + MESH_LOAD_DEADLINE_SECONDS
        try:
            what = mesh_ingest.apply(generation, DEFAULT_TEXTURE_RESOLUTION, _mesh_finished)
        except Exception:
            _mesh_deadline[0] = None
            raise
        _panel.set_status("{0}: {1}".format(
            "creating the project" if what == "create" else "swapping the mesh in",
            mesh_ingest.describe_scene(generation)))
        return True
    if topic is topic_module.INPUTS:
        if not substance_painter.project.is_open():
            _panel.set_status("textures arrived from Blender and no project is open")
            return False
        _panel.set_status(take_inputs(generation))
        _presence_due[0] = True
        return False
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if not topic_module.can_answer(asked, HOST.capabilities):
            return False
        record = generation.record
        if asked == record_module.ASK_FOR_TEXTURES:
            push_textures(layer=record["layer"], directory=record["directory"],
                          texture_sets=record["texture_sets"])
            _panel.set_status("pushed {0} to Blender".format(
                "the selected layer" if record["layer"] else
                "the textures of " + _named(record["texture_sets"]) if record["texture_sets"]
                else "the textures of every Texture Set"))
            return False
        if asked == record_module.ASK_FOR_SHADING:
            _panel.set_status("pushed the shader of {0} to Blender".format(
                _named(push_shader(record["texture_sets"]))))
            return False
        if asked == record_module.ASK_TO_TAKE_TEXTURES:
            pulled, skipped = pull_textures(record["texture_sets"])
            _panel.set_status(_with_skipped(
                "pulling the textures of {0} from Blender".format(_named(pulled)), skipped))
            return False
        if asked == record_module.ASK_FOR_LAYOUT:
            _panel.set_status(layout_state.answer(
                CONNECTION.session.publisher(topic_module.TEXTURES), record["texture_set"],
                generation.number))
            return False
        if asked == record_module.ASK_TO_CARRY:
            _panel.set_status(guest_state.answer(
                CONNECTION.session.publisher(topic_module.TEXTURES), record["moves"],
                set(record["emptied"]), generation.number))
            return False
        if asked == record_module.ASK_TO_RENAME:
            renamed = texture_publish.rename(generation.record.get("renames") or {})
            _presence_due[0] = True
            _panel.set_status("renamed " + (", ".join(
                "{0} -> {1}".format(old, new) for old, new in sorted(renamed.items()))
                or "nothing"))
            return False
    raise RuntimeError(
        "nothing here receives {0!r} yet, and the topic says this application "
        "hears it".format(topic.key))


def pump():
    """Consume what was published, stopping at anything that leaves Painter busy.

    A generation that raises is acknowledged all the same, so one bad request
    cannot keep the channel from ever moving again.
    """
    if not CONNECTION.is_open or _panel is None:
        return
    if _mesh_deadline[0] is not None:
        if time.monotonic() < _mesh_deadline[0]:
            return
        LOG.warning("a mesh load never said it finished within %.0fs; resuming the "
                    "channel", MESH_LOAD_DEADLINE_SECONDS)
        _mesh_deadline[0] = None
    CONNECTION.session.touch()
    if substance_painter.project.is_busy():
        return
    for endpoint, payload in CONNECTION.session.changed_state():
        if endpoint.topic is topic_module.PRESENCE:
            CONNECTION.peers[endpoint.peer] = payload
        elif endpoint.topic is topic_module.SHADING and endpoint.peer == BLENDER.name:
            try:
                _panel.set_status(apply_shading(payload))
            except Exception as error:
                LOG.error("the material from Blender could not be applied: %s", error)
                _panel.set_status("material not applied: {0}".format(error))
    for endpoint, generation in CONNECTION.session.incoming():
        try:
            became_busy = _handle(endpoint.topic, generation)
        except Exception as error:
            LOG.error("%s generation %d from %s failed and is being skipped: %s",
                      endpoint.topic.key, generation.number, endpoint.peer, error)
            _panel.set_status(str(error))
            became_busy = False
        endpoint.reader.acknowledge(generation)
        if became_busy:
            break


def _on_timer():
    try:
        project_imports.settle_due(project_is_locked)
        if _presence_due[0] and not substance_painter.project.is_busy():
            _presence_due[0] = False
            publish_presence()
        pump()
        if _panel is not None:
            _panel.refresh()
    except Exception as error:
        if project_is_locked(error):
            return
        LOG.error("pump failed: %s", error)
        if _panel is not None:
            _panel.set_status("pump failed: {0}".format(error))


def _on_project_ready(_event):
    _IMPORTED.clear()
    project_imports.forget()
    mesh_ingest.settle_new_project()
    _mesh_finished()


def _on_project_changed(_event):
    _presence_due[0] = True


def _on_project_closed(_event):
    held_imports.release()
    project_imports.forget()
    _presence_due[0] = True


def show_panel():
    """Bring the dock back, from a menu item that is always there."""
    if _dock is None:
        return
    _dock.setVisible(True)
    _dock.raise_()


def _rest_in_the_strip(_event=None):
    """Fold the panel into the right-hand strip once, after Painter restores its layout.

    Painter restores its saved layout after plugins start, so a dock closed any
    earlier is reopened a moment later. Folded exactly once; after that whatever it
    was left as comes back.
    """
    if _dock is None:
        return
    settings = QtCore.QSettings()
    key = "python_plugins/RuriBridge/dock_folded_once"
    if not settings.value(key):
        settings.setValue(key, True)
        _dock.setVisible(False)


_EVENTS = (
    (substance_painter.event.ProjectEditionEntered, _on_project_ready),
    (substance_painter.event.ProjectClosed, _on_project_closed),
    (substance_painter.event.ProjectAboutToSave, project_imports.before_save),
    (substance_painter.event.ProjectSaved, project_imports.after_save),
    (substance_painter.event.ProjectSaved, _on_project_changed),
    (substance_painter.event.LayerStacksModelDataChanged, _on_project_changed),
    (substance_painter.event.GraphicalUserInterfaceStarted, _rest_in_the_strip),
)


def start_plugin():
    global _panel, _dock, _timer, _log_handler, _menu_action
    _log_handler = log_module.install_callable_sink(_emit_to_painter)
    _panel = RuriBridgePanel()
    _dock = substance_painter.ui.add_dock_widget(
        _panel, substance_painter.ui.UIMode.Edition
        | substance_painter.ui.UIMode.Visualisation
        | substance_painter.ui.UIMode.Baking)
    _menu_action = QtGui.QAction("RuriBridge")
    _menu_action.triggered.connect(show_panel)
    substance_painter.ui.add_action(substance_painter.ui.ApplicationMenu.Window, _menu_action)
    for event, handler in _EVENTS:
        substance_painter.event.DISPATCHER.connect_strong(event, handler)
    if substance_painter.ui.get_main_window().isVisible():
        _rest_in_the_strip()
    _timer = QtCore.QTimer(_panel)
    _timer.timeout.connect(_on_timer)
    _timer.start(POLL_MILLISECONDS)
    try:
        CONNECTION.open(os.environ.get(SESSION_ENVIRONMENT_VARIABLE, arena_module.DEFAULT_SESSION),
                        os.environ.get(ROOT_ENVIRONMENT_VARIABLE) or None)
        publish_presence()
    except Exception as error:
        LOG.error("could not attach on start: %s", error)
        _panel.set_status("attach failed: {0}".format(error))
    LOG.info("plugin started from %s", package_root())


def close_plugin():
    global _panel, _dock, _timer, _log_handler, _menu_action
    if _timer is not None:
        _timer.stop()
        _timer = None
    for event, handler in _EVENTS:
        substance_painter.event.DISPATCHER.disconnect(event, handler)
    CONNECTION.close()
    if _menu_action is not None:
        substance_painter.ui.delete_ui_element(_menu_action)
        _menu_action = None
    if _dock is not None:
        substance_painter.ui.delete_ui_element(_dock)
        _dock = None
    _panel = None
    if _log_handler is not None:
        log_module.root().removeHandler(_log_handler)
        _log_handler = None
