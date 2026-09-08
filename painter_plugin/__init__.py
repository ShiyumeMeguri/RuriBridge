# -*- coding: utf-8 -*-
"""RuriBridge — the Substance 3D Painter end of the shared-memory bridge.

Painter owns the ``to_blender`` channel and reads ``to_painter``. The pump is a
QTimer reading a few integers out of the mapped control block; when Blender
publishes a mesh, the GLB it names is already resident in pages this process can
map, so handing its path to ``project.create`` costs a page lookup rather than a
read from storage.

Painter's own Python API is the whole vocabulary here: project creation and mesh
reloading through ``substance_painter.project``, channel rendering through
``substance_painter.export``, texture set introspection through
``substance_painter.textureset``, and -- because the package ships no shaders
module at all -- viewport shader instances through ``substance_painter.js``,
which is a Python entry point that already hands back parsed JSON.
"""

import ctypes
import os
import sys
import time


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
                "plugin must stay inside its checkout".format(here))
        candidate = parent


REPOSITORY_ROOT = _install_core_path()

from PySide6 import QtCore, QtGui, QtWidgets

import substance_painter.event
import substance_painter.logging
import substance_painter.project
import substance_painter.textureset
import substance_painter.ui

from ruri_bridge import arena as arena_module
from ruri_bridge import channel as channel_module
from ruri_bridge import log as log_module
from ruri_bridge import record as record_module
from ruri_bridge import sync as sync_module

from . import mesh_ingest, shader_state, texture_publish

LOG = log_module.logger("painter")

SESSION_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_SESSION"
POLL_MILLISECONDS = 250
DEFAULT_TEXTURE_RESOLUTION = 2048
MESH_LOAD_DEADLINE_SECONDS = 600.0
SHADER_QUIET_SECONDS = 0.35
TEXTURE_QUIET_SECONDS = 0.9
SHADER_POLL_DUTY = 20.0

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


class _Connection:
    """The one live attachment this Painter process holds."""

    def __init__(self):
        self.arena = None
        self.publisher = None
        self.subscriber = None
        self.state_writer = None
        self.state_reader = None

    @property
    def is_open(self):
        return self.arena is not None

    def open(self, session):
        self.close()
        self.arena = arena_module.Arena.open_session(record_module.CHANNELS, session=session)
        self.publisher = channel_module.Publisher(self.arena, record_module.CHANNEL_TO_BLENDER)
        self.subscriber = channel_module.Subscriber(self.arena, record_module.CHANNEL_TO_PAINTER)
        self._adopt_pending_work()
        self.state_writer = channel_module.StateWriter(
            self.arena, record_module.CHANNEL_STATE_TO_BLENDER)
        self.state_reader = channel_module.StateReader(
            self.arena, record_module.CHANNEL_STATE_TO_PAINTER)
        self.state_reader.skip_to_latest()
        LOG.info("attached to session %s at %s", session, self.arena.directory)
        return self.arena

    def _adopt_pending_work(self):
        """Decide what a fresh attachment owes the other side.

        With a project already open, history is not ours to replay: the user is
        working, and loading a mesh from an hour ago would throw that away. With
        no project open there is nothing to protect and something to do -- the
        mesh someone published while starting this application is waiting, and
        taking it is the entire reason they published it before opening Painter.
        """
        try:
            busy = substance_painter.project.is_open()
        except Exception:
            busy = False
        if busy:
            self.subscriber.skip_to_latest()
        else:
            self.subscriber.catch_up(record_module.KIND_MESH)

    def close(self):
        if self.arena is not None:
            self.arena.close()
        self.arena = None
        self.publisher = None
        self.subscriber = None
        self.state_writer = None
        self.state_reader = None


CONNECTION = _Connection()


def executable_path():
    """This process's own image path, so Blender can start Painter later.

    Asked of Windows rather than of ``sys.executable``, which in an embedded
    interpreter is whatever the host chose to report.
    """
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer))
    return buffer.value if length else ""


def default_session():
    return os.environ.get(SESSION_ENVIRONMENT_VARIABLE, arena_module.DEFAULT_SESSION)


def publish_project_state():
    if not CONNECTION.is_open:
        return None
    state = texture_publish.current_project_state()
    state["host_executable"] = executable_path()
    return CONNECTION.publisher.publish_record(state)


def publish_shader_state():
    """Tell Blender which shaders this project runs and what they expose."""
    if not CONNECTION.is_open or not substance_painter.project.is_open():
        return None
    state = shader_state.read_state()
    return CONNECTION.publisher.publish_record(record_module.shader_state(
        "painter", state["instances"], state["parameters"], state["assignment"]))


def publish_textures(preset_name):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    return texture_publish.publish(CONNECTION.arena, CONNECTION.publisher, preset_name)


def _panel_icon():
    """A tab-strip icon, which is also the only way back to a closed dock.

    Painter turns a dock widget's windowIcon into the button that reopens it
    once someone closes it, and gives a dock without one no way back at all. It
    is drawn here rather than shipped as a file so the plugin stays a folder of
    source with nothing to lose.
    """
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
    """The dock: attach, watch the slots, push textures back by hand."""

    def __init__(self):
        super().__init__()
        self.setObjectName("RuriBridgePanel")
        self.setWindowTitle("RuriBridge")
        self.setWindowIcon(_panel_icon())

        layout = QtWidgets.QVBoxLayout(self._themed_body())
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        session_row = QtWidgets.QHBoxLayout()
        self.session_field = QtWidgets.QLineEdit(default_session())
        self.attach_button = QtWidgets.QPushButton("Attach")
        session_row.addWidget(QtWidgets.QLabel("Session"))
        session_row.addWidget(self.session_field, 1)
        session_row.addWidget(self.attach_button)
        layout.addLayout(session_row)

        form = QtWidgets.QFormLayout()
        self.resolution_box = QtWidgets.QComboBox()
        for value in (256, 512, 1024, 2048, 4096, 8192):
            self.resolution_box.addItem(str(value), value)
        self.resolution_box.setCurrentText(str(DEFAULT_TEXTURE_RESOLUTION))
        form.addRow("New project resolution", self.resolution_box)
        self.preset_box = QtWidgets.QComboBox()
        self.preset_box.setEditable(True)
        self.preset_box.addItem(texture_publish.DEFAULT_PRESET_NAME)
        form.addRow("Export preset", self.preset_box)
        layout.addLayout(form)

        self.live_box = QtWidgets.QCheckBox("Live sync to Blender")
        self.live_box.setToolTip(
            "Send painted channels and changed shader values as soon as they settle, "
            "instead of waiting to be asked")
        self.live_box.setChecked(True)
        layout.addWidget(self.live_box)

        legs = QtWidgets.QHBoxLayout()
        self.live_textures_box = QtWidgets.QCheckBox("Textures")
        self.live_textures_box.setChecked(True)
        self.live_values_box = QtWidgets.QCheckBox("Shader Values")
        self.live_values_box.setChecked(True)
        legs.addWidget(self.live_textures_box)
        legs.addWidget(self.live_values_box)
        layout.addLayout(legs)

        self.send_button = QtWidgets.QPushButton("Send Textures To Blender")
        layout.addWidget(self.send_button)
        self.send_values_button = QtWidgets.QPushButton("Send Shader Values To Blender")
        layout.addWidget(self.send_values_button)
        self.request_mesh_button = QtWidgets.QPushButton("Ask Blender For The Scene")
        layout.addWidget(self.request_mesh_button)

        self.link_label = QtWidgets.QLabel("")
        layout.addWidget(self.link_label)

        self.status_label = QtWidgets.QLabel("detached")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.slot_label = QtWidgets.QLabel("")
        self.slot_label.setWordWrap(True)
        layout.addWidget(self.slot_label)
        layout.addStretch(1)

        self.attach_button.clicked.connect(self._toggle_attachment)
        self.send_button.clicked.connect(self._send_textures)
        self.send_values_button.clicked.connect(self._send_values)
        self.request_mesh_button.clicked.connect(self._request_mesh)

    def _themed_body(self):
        """The widget the controls live in, chosen so Painter's own theme paints it.

        Painter themes through a 300-kilobyte application stylesheet rather than
        through the palette; its main window's palette is still Qt's default light
        one. That stylesheet dresses the widget classes Painter knows, which is
        why the buttons and fields looked right from the start while the panel
        behind them arrived bright blue: a bare QWidget matches nothing in it, so
        what showed through was the dock's own accent colour.

        A scroll area does match, so it paints Painter's panel grey and keeps
        following the theme wherever that goes, without a colour written down
        here. It also lets the panel scroll when the dock is short, which a dock
        sharing the right-hand column with the layer stack usually is.
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

    @property
    def texture_resolution(self):
        return int(self.resolution_box.currentData())

    def live_enabled(self):
        return self.live_box.isChecked()

    def live_textures_enabled(self):
        return self.live_box.isChecked() and self.live_textures_box.isChecked()

    def live_values_enabled(self):
        return self.live_box.isChecked() and self.live_values_box.isChecked()

    def _send_values(self):
        if not CONNECTION.is_open or not substance_painter.project.is_open():
            self.set_status("no project open")
            return
        try:
            values = _values_by_texture_set(shader_state.parameter_values())
            _shader_gate.prime(shader_state.parameter_values())
            CONNECTION.state_writer.write(record_module.shader_values("painter", values))
        except Exception as error:
            LOG.error("could not send shader values: %s", error)
            self.set_status("send failed: {0}".format(error))
            return
        self.set_status("sent {0} shader value(s)".format(
            sum(len(entry) for entry in values.values())))

    def _request_mesh(self):
        if not CONNECTION.is_open:
            self.set_status("not attached")
            return
        generation = CONNECTION.publisher.publish_record(
            record_module.mesh_request("painter"))
        self.set_status("asked Blender for the scene, generation {0}".format(
            generation.number))

    def set_status(self, message):
        self.status_label.setText(message)

    def refresh_slots(self):
        if not CONNECTION.is_open:
            self.slot_label.setText("")
            return
        blender = CONNECTION.arena.read_slot(record_module.CHANNEL_TO_PAINTER)
        self.link_label.setText(
            "Blender is attached" if blender.writer_is_live else "Blender is not attached")
        self.slot_label.setText("\n".join(
            "{0}: gen {1} ack {2} drop {3}".format(
                state.channel, state.generation, state.acknowledged_generation,
                state.dropped_generations)
            for state in CONNECTION.arena.describe()))

    def refresh_presets(self):
        if not substance_painter.project.is_open():
            return
        current = self.preset_box.currentText()
        names = texture_publish.available_preset_names()
        self.preset_box.clear()
        self.preset_box.addItems(names)
        if current in names:
            self.preset_box.setCurrentText(current)

    def _toggle_attachment(self):
        if CONNECTION.is_open:
            CONNECTION.close()
            self.attach_button.setText("Attach")
            self.set_status("detached")
            self.refresh_slots()
            return
        try:
            attached = CONNECTION.open(self.session_field.text().strip() or default_session())
        except Exception as error:
            self.set_status("attach failed: {0}".format(error))
            return
        self.attach_button.setText("Detach")
        self.set_status("attached: {0}".format(attached.directory))
        publish_project_state()
        self.refresh_slots()

    def _send_textures(self):
        try:
            generation = publish_textures(self.preset_box.currentText())
        except Exception as error:
            LOG.error("texture publish failed: %s", error)
            self.set_status("export failed: {0}".format(error))
            return
        self.set_status("sent texture generation {0}".format(generation.number))
        self.refresh_slots()


_panel = None
_dock = None
_timer = None
_log_handler = None
_menu_action = None
_mesh_deadline = None
_shader_gate = sync_module.ChangeGate("painter.shader", SHADER_QUIET_SECONDS)
_texture_gate = sync_module.ChangeGate("painter.textures", TEXTURE_QUIET_SECONDS)
_dirty_textures = set()
_texture_serial = 0
_shader_poll_cost = None
_shader_poll_due = 0.0
_pending_display_names = {}


def _on_texture_state(event):
    """Painter's only signal that a stroke landed. Kept to two stores.

    Painter warns that work done in this callback hurts the painting experience,
    and it fires per texture per throttle window while a brush is down, so
    nothing is resolved here -- the ids become names later, once, after the burst
    has settled.
    """
    global _texture_serial
    _dirty_textures.add((event.stack_id, event.channel_type))
    _texture_serial += 1


def _resolve_dirty_textures():
    by_texture_set = {}
    for stack_id, channel_type in _dirty_textures:
        try:
            name = substance_painter.textureset.Stack(stack_id).material().name
        except Exception as error:
            LOG.warning("stack %s no longer resolves: %s", stack_id, error)
            continue
        by_texture_set.setdefault(name, set()).add(channel_type.name.lower())
    return by_texture_set


def _publish_dirty_textures():
    dirty = _resolve_dirty_textures()
    _dirty_textures.clear()
    _texture_gate.prime({"dirty": [], "serial": _texture_serial})
    if not dirty:
        return None
    generation = texture_publish.publish(
        CONNECTION.arena, CONNECTION.publisher, _panel.preset_box.currentText(),
        sorted(dirty), dirty)
    _panel.set_status("live: sent {0} of {1}".format(
        ", ".join(sorted({channel for names in dirty.values() for channel in names})),
        ", ".join(sorted(dirty))))
    return generation


def _values_by_texture_set(values_by_label):
    """Re-key the shader instances' values by the Texture Sets that run them.

    Keyed by identity, which is what the other side addresses materials with;
    instance labels mean nothing over there, and several Texture Sets can share
    one instance.
    """
    identity_by_display = {
        texture_set.name: texture_set.original_name
        for texture_set in substance_painter.textureset.all_texture_sets()}
    by_texture_set = {}
    for display, body in shader_state.assignment().get("texturesets", {}).items():
        groups = values_by_label.get(body.get("shader"))
        if not groups:
            continue
        flattened = {}
        for members in groups.values():
            flattened.update(members)
        by_texture_set[identity_by_display.get(display, display)] = flattened
    return by_texture_set


def live_sync():
    """Publish what changed on this side, once it has stopped changing.

    The shader poll paces itself. Asking Painter for every uniform costs under a
    millisecond on a default project and a quarter of a second on a character
    with a dozen Texture Sets on a generated shader, so a fixed interval is
    either wasteful or ruinous. Each poll instead sets the next one far enough
    out that watching never takes more than a small share of a core, measured
    rather than assumed.
    """
    global _shader_poll_cost
    if not CONNECTION.is_open or _panel is None or not _panel.live_enabled():
        return
    if _mesh_deadline is not None or not substance_painter.project.is_in_edition_state():
        return
    if substance_painter.project.is_busy():
        return
    if _panel.live_textures_enabled() and _dirty_textures and _texture_gate.should_publish(
            {"dirty": sorted((stack, channel.name) for stack, channel in _dirty_textures),
             "serial": _texture_serial}):
        _publish_dirty_textures()
    global _shader_poll_due
    if not _panel.live_values_enabled() or time.monotonic() < _shader_poll_due:
        return
    started = time.monotonic()
    values = shader_state.parameter_values()
    cost = time.monotonic() - started
    _shader_poll_due = time.monotonic() + cost * SHADER_POLL_DUTY
    if _shader_poll_cost is None or abs(cost - _shader_poll_cost) > 0.5 * _shader_poll_cost:
        _shader_poll_cost = cost
        LOG.info("watching shader values costs %.0f ms; asking again in %.1f s",
                 cost * 1000.0, cost * SHADER_POLL_DUTY)
    if _shader_gate.should_publish(values):
        CONNECTION.state_writer.write(record_module.shader_values(
            "painter", _values_by_texture_set(values)))
        _panel.set_status("live: sent shader values")


def _handle(generation):
    """Apply one generation. Returns True when Painter is now busy with it."""
    global _mesh_deadline
    if generation.kind == record_module.KIND_MESH:
        _pending_display_names.clear()
        _pending_display_names.update(
            {row["identity"]: row["name"] for row in generation.record.get("materials", [])
             if row.get("identity")})
        _mesh_deadline = time.monotonic() + MESH_LOAD_DEADLINE_SECONDS
        try:
            intent = mesh_ingest.apply(generation, _panel.texture_resolution)
        except Exception:
            _mesh_deadline = None
            raise
        _panel.set_status("mesh generation {0}: {1} ({2})".format(
            generation.number, intent, mesh_ingest.describe_scene(generation)))
        return True
    if generation.kind == record_module.KIND_EXPORT_REQUEST:
        preset = generation.record.get("preset_name") or _panel.preset_box.currentText()
        published = texture_publish.publish(
            CONNECTION.arena, CONNECTION.publisher, preset,
            generation.record.get("texture_sets"))
        _panel.set_status("export request {0} answered with generation {1}".format(
            generation.number, published.number))
        return False
    LOG.warning("ignoring generation %d of unknown kind %r",
                generation.number, generation.kind)
    return False


def take_shader_values():
    """Apply values arriving in the control block, and never echo them back."""
    if not CONNECTION.is_open or not substance_painter.project.is_open():
        return None
    payload = CONNECTION.state_reader.take()
    if payload is None:
        return None
    report = shader_state.apply_by_texture_set(
        payload.get("by_texture_set", {}),
        payload.get("shader_url_by_texture_set"))
    refused = [label for label in ("unknown", "mismatched", "conflicting", "unmapped")
               if report[label]]
    for label in refused:
        LOG.warning("shader values %s: %s", label, report[label])
    values = shader_state.parameter_values()
    _shader_gate.suppress(values)
    if refused:
        CONNECTION.state_writer.write(record_module.shader_values(
            "painter", _values_by_texture_set(values)))
        LOG.info("wrote back what actually stuck, because %s", ", ".join(refused))
    if _panel is not None:
        _panel.set_status("applied shader values: {0}".format(report["applied"] or "nothing"))
    return report


def pump():
    """Consume what Blender published, stopping at anything that leaves Painter busy.

    Loading a mesh is asynchronous, so the generation after it cannot be applied
    in the same tick: a texture export queued behind a project creation would run
    before the project exists. Each generation is acknowledged as it is finished
    and the batch ends there, so the next tick resumes exactly where this stopped.

    Waiting on ``ProjectEditionEntered`` rather than on ``is_busy`` is deliberate:
    there is a window between queueing a project creation and Painter reporting
    itself busy, and an export request handled inside that window would fail on a
    project that is about to exist. The deadline exists so a load that never
    signals cannot wedge the channel for the rest of the session.

    A generation that raises is acknowledged all the same. Leaving it unread would
    mean retrying it at every tick forever, which turns one bad request into a
    channel that never moves again -- the failure belongs in the log, not in the
    queue.
    """
    global _mesh_deadline
    if not CONNECTION.is_open or _panel is None:
        return
    if _mesh_deadline is not None:
        if time.monotonic() < _mesh_deadline:
            return
        LOG.warning("mesh load never signalled ProjectEditionEntered within %.0fs; "
                    "resuming the channel", MESH_LOAD_DEADLINE_SECONDS)
        _mesh_deadline = None
    CONNECTION.arena.touch(record_module.CHANNEL_TO_BLENDER)
    if substance_painter.project.is_busy():
        return
    take_shader_values()
    for generation in CONNECTION.subscriber.pending():
        try:
            became_busy = _handle(generation)
        except Exception as error:
            LOG.error("generation %d (%s) failed and is being skipped: %s",
                      generation.number, generation.kind, error)
            _panel.set_status("generation {0} failed: {1}".format(generation.number, error))
            became_busy = False
        CONNECTION.subscriber.acknowledge(generation)
        if became_busy:
            break
    _panel.refresh_slots()


def _on_timer():
    try:
        pump()
        live_sync()
    except Exception as error:
        LOG.error("pump failed: %s", error)
        if _panel is not None:
            _panel.set_status("pump failed: {0}".format(error))


def _on_project_ready(_event):
    """A project just became editable.

    Reopening a bound project happens in two steps, because Painter opens
    asynchronously: the project arrives here, and only then can the mesh that
    asked for it go in. While that reload runs the project holds the previous
    mesh, so settling waits for it rather than reporting on what is still there.
    """
    global _mesh_deadline
    _mesh_deadline = None
    if mesh_ingest.resume_after_open(_on_project_settled):
        _mesh_deadline = time.monotonic() + MESH_LOAD_DEADLINE_SECONDS
        return
    _on_project_settled()


def _on_project_settled():
    """The project now holds the mesh that was sent. Show the panel, adopt values.

    The dock belongs to Painter's project modes, so on the home screen it exists
    but is not shown -- which looks exactly like a plugin that failed. Opening a
    project is the moment it can be seen, so that is the moment to make sure it
    is, rather than leaving it to whatever the saved layout happened to hold.

    Priming rather than publishing is what stops a freshly created project's
    defaults from overwriting values the other side authored: on connect neither
    side asserts, and only an actual change afterwards travels.
    """
    global _mesh_deadline
    _mesh_deadline = None
    if _dock is not None and not _dock.isVisible():
        _dock.setVisible(True)
        _dock.raise_()
    if _dock is not None:
        LOG.info("panel visible: %s", _dock.isVisible())
    try:
        _shader_gate.prime(shader_state.parameter_values())
    except shader_state.ShaderStateError as error:
        LOG.error("could not adopt the shader values: %s", error)
    if _pending_display_names:
        texture_publish.apply_display_names(_pending_display_names)
    if _panel is not None:
        _panel.refresh_presets()
    publish_project_state()
    try:
        publish_shader_state()
    except shader_state.ShaderStateError as error:
        LOG.error("could not read the shader state: %s", error)


def _on_project_closed(_event):
    publish_project_state()


def _on_project_saved(_event):
    """Tell Blender where the project now lives, so the scene can bind to it.

    Saving is the only moment a project acquires a path, and it is somebody
    pressing a key in Painter, not anything the bridge drives. Publishing the
    state here is how the other side learns a path it never chose.
    """
    publish_project_state()


def show_panel():
    """Bring the dock back, from a menu item that is always there.

    A dock lives in the saved layout and in the mode it was registered for, and
    an icon only offers a way back once Painter decides to show the strip. A menu
    entry depends on neither, so there is always one place to look when the panel
    is not where somebody expects it.
    """
    if _dock is None:
        LOG.warning("the panel has not been created yet")
        return
    _dock.setVisible(True)
    _dock.raise_()
    LOG.info("panel visible: %s, floating: %s", _dock.isVisible(), _dock.isFloating())


def start_plugin():
    global _panel, _dock, _timer, _log_handler, _menu_action
    _log_handler = log_module.install_callable_sink(_emit_to_painter)
    _panel = RuriBridgePanel()
    _dock = substance_painter.ui.add_dock_widget(
        _panel, substance_painter.ui.UIMode.Edition
        | substance_painter.ui.UIMode.Visualisation
        | substance_painter.ui.UIMode.Baking)
    _dock.setVisible(True)
    _dock.raise_()
    _menu_action = QtGui.QAction("RuriBridge")
    _menu_action.triggered.connect(show_panel)
    substance_painter.ui.add_action(
        substance_painter.ui.ApplicationMenu.Window, _menu_action)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectEditionEntered, _on_project_ready)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectClosed, _on_project_closed)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectSaved, _on_project_saved)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.TextureStateEvent, _on_texture_state)
    _timer = QtCore.QTimer(_panel)
    _timer.timeout.connect(_on_timer)
    _timer.start(POLL_MILLISECONDS)
    try:
        CONNECTION.open(default_session())
        _panel.attach_button.setText("Detach")
        _panel.set_status("attached: {0}".format(CONNECTION.arena.directory))
        publish_project_state()
        _panel.refresh_slots()
    except Exception as error:
        LOG.error("could not attach on start: %s", error)
        _panel.set_status("attach failed: {0}".format(error))
    LOG.info("panel added (visible: %s); Window > RuriBridge reopens it, and the "
             "'RB' button in the right-hand strip does too. It belongs to the "
             "project modes, so it shows once a project is open.",
             _dock.isVisible())
    LOG.info("plugin started from %s", REPOSITORY_ROOT)


def close_plugin():
    global _panel, _dock, _timer, _log_handler, _menu_action
    if _timer is not None:
        _timer.stop()
        _timer = None
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectEditionEntered, _on_project_ready)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectClosed, _on_project_closed)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectSaved, _on_project_saved)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.TextureStateEvent, _on_texture_state)
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
