# -*- coding: utf-8 -*-
"""RuriBridge -- the Substance 3D Painter end.

Painter is where textures are painted, so this end does three things and shows
one table:

* **Ask Blender For The Mesh** -- Blender sends the surface, and it is swapped in
  under every layer the project already has. A Texture Set with layers that
  nothing in the payload paints into stops the swap instead of being dropped.
* **Send Textures To Blender** -- every Texture Set is exported into the Blender
  document's own textures folder, and Blender puts the files into its materials.
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

from . import mesh_ingest, texture_publish

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


def send_textures(layer=False, directory=None):
    """Export into the folder Blender named, and say so on the session."""
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    target = directory or blender_state().get("textures_directory") or ""
    if not target:
        raise RuntimeError("Blender has not said where its textures live; save the "
                           ".blend and keep it attached")
    return texture_publish.publish(
        CONNECTION.session.publisher(topic_module.TEXTURES), target,
        texture_publish.DEFAULT_PRESET_NAME, layer=layer)


# -- pulling an image into the selected layer ----------------------------------------

_IMPORTED = {}


def _resource_for(path):
    """The project resource for an image file, imported once per project."""
    key = os.path.normcase(os.path.abspath(path))
    known = _IMPORTED.get(key)
    if known is not None:
        return known
    resource = substance_painter.resource.import_project_resource(
        path, substance_painter.resource.Usage.TEXTURE)
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


def _selected_layer():
    stack = substance_painter.textureset.get_active_stack()
    selected = substance_painter.layerstack.get_selected_nodes(stack)
    if len(selected) != 1:
        raise RuntimeError("select exactly one layer (or one effect in its mask)")
    return selected[0]


def pull_into_selected_layer(path, as_mask):
    """Put one Blender image into the selected layer: its mask, or its fill source.

    Nothing else in the stack is touched. As a mask, the layer gets a mask if it
    has none, and the image fills it; a fill effect already selected in a mask has
    its source replaced instead. As a reference, the selected fill layer's base
    colour comes from the image.
    """
    node = _selected_layer()
    resource = _resource_for(path)
    layerstack = substance_painter.layerstack
    if as_mask:
        if isinstance(node, layerstack.FillEffectNode) and node.is_in_mask_stack():
            node.set_source(None, resource)
            return "the selected mask fill now reads {0}".format(os.path.basename(path))
        if not isinstance(node, layerstack.LayerNode):
            raise RuntimeError("select a layer, or a fill effect inside its mask")
        if not node.has_mask():
            node.add_mask(layerstack.MaskBackground.Black)
        fill = layerstack.insert_fill(
            layerstack.InsertPosition.inside_node(node, layerstack.NodeStack.Mask))
        fill.set_source(None, resource)
        return "{0} is now masked by {1}".format(node.get_name(), os.path.basename(path))
    if not isinstance(node, layerstack.FillLayerNode):
        raise RuntimeError("select a fill layer to take the image as its reference")
    node.set_source(substance_painter.textureset.ChannelType.BaseColor, resource)
    return "{0} now shows {1}".format(node.get_name(), os.path.basename(path))


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
    """The dock: the table, three buttons and a status line."""

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

        self.mesh_button = QtWidgets.QPushButton("Ask Blender For The Mesh")
        self.textures_button = QtWidgets.QPushButton("Send Textures To Blender")
        layout.addWidget(self.mesh_button)
        layout.addWidget(self.textures_button)

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
        self.textures_button.clicked.connect(self._send_textures)
        self.mask_button.clicked.connect(lambda: self._pull(True))
        self.reference_button.clicked.connect(lambda: self._pull(False))
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

    def _send_textures(self):
        try:
            send_textures()
        except Exception as error:
            LOG.error("texture export failed: %s", error)
            self.set_status("export failed: {0}".format(error))
            return
        self.set_status("sent the textures to Blender")

    def _pull(self, as_mask):
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


def _mesh_finished(_status=None):
    _mesh_deadline[0] = None
    _presence_due[0] = True


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
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if not topic_module.can_answer(asked, HOST.capabilities):
            return False
        if asked == record_module.ASK_FOR_TEXTURES:
            send_textures(layer=bool(generation.record.get("layer")),
                          directory=generation.record.get("directory"))
            _panel.set_status("sent {0} to Blender".format(
                "the selected layer" if generation.record.get("layer") else "the textures"))
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
    mesh_ingest.settle_new_project()
    _mesh_finished()


def _on_project_changed(_event):
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
    (substance_painter.event.ProjectClosed, _on_project_changed),
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
