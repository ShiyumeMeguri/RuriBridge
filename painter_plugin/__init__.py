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
``substance_painter.textureset``. Nothing goes through the JavaScript engine.
"""

import os
import sys
import time


def _install_core_path():
    here = os.path.dirname(os.path.realpath(__file__))
    candidate = os.path.dirname(here)
    while True:
        if os.path.isfile(os.path.join(candidate, "ruri_bridge", "__init__.py")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            raise ImportError(
                "RuriBridge cannot find the ruri_bridge core above {0}; the plugin must "
                "stay inside its checkout (junction the checkout, do not copy one "
                "folder out of it)".format(here))
        candidate = parent


REPOSITORY_ROOT = _install_core_path()

from PySide6 import QtCore, QtWidgets

import substance_painter.event
import substance_painter.logging
import substance_painter.project
import substance_painter.ui

from ruri_bridge import arena as arena_module
from ruri_bridge import channel as channel_module
from ruri_bridge import log as log_module
from ruri_bridge import record as record_module

from . import mesh_ingest, texture_publish

LOG = log_module.logger("painter")

SESSION_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_SESSION"
POLL_MILLISECONDS = 250
DEFAULT_TEXTURE_RESOLUTION = 2048
MESH_LOAD_DEADLINE_SECONDS = 600.0

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

    @property
    def is_open(self):
        return self.arena is not None

    def open(self, session):
        self.close()
        self.arena = arena_module.Arena.open_session(record_module.CHANNELS, session=session)
        self.publisher = channel_module.Publisher(self.arena, record_module.CHANNEL_TO_BLENDER)
        self.subscriber = channel_module.Subscriber(self.arena, record_module.CHANNEL_TO_PAINTER)
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


def default_session():
    return os.environ.get(SESSION_ENVIRONMENT_VARIABLE, arena_module.DEFAULT_SESSION)


def publish_project_state():
    if not CONNECTION.is_open:
        return None
    return CONNECTION.publisher.publish_record(texture_publish.current_project_state())


def publish_textures(preset_name):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    return texture_publish.publish(CONNECTION.arena, CONNECTION.publisher, preset_name)


class RuriBridgePanel(QtWidgets.QWidget):
    """The dock: attach, watch the slots, push textures back by hand."""

    def __init__(self):
        super().__init__()
        self.setObjectName("RuriBridgePanel")
        self.setWindowTitle("RuriBridge")

        layout = QtWidgets.QVBoxLayout(self)
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

        self.send_button = QtWidgets.QPushButton("Send Textures To Blender")
        layout.addWidget(self.send_button)

        self.status_label = QtWidgets.QLabel("detached")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.slot_label = QtWidgets.QLabel("")
        self.slot_label.setWordWrap(True)
        layout.addWidget(self.slot_label)
        layout.addStretch(1)

        self.attach_button.clicked.connect(self._toggle_attachment)
        self.send_button.clicked.connect(self._send_textures)

    @property
    def texture_resolution(self):
        return int(self.resolution_box.currentData())

    def set_status(self, message):
        self.status_label.setText(message)

    def refresh_slots(self):
        if not CONNECTION.is_open:
            self.slot_label.setText("")
            return
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
_mesh_deadline = None


def _handle(generation):
    """Apply one generation. Returns True when Painter is now busy with it."""
    global _mesh_deadline
    if generation.kind == record_module.KIND_MESH:
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
    if substance_painter.project.is_busy():
        return
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
    except Exception as error:
        LOG.error("pump failed: %s", error)
        if _panel is not None:
            _panel.set_status("pump failed: {0}".format(error))


def _on_project_ready(_event):
    global _mesh_deadline
    _mesh_deadline = None
    if _panel is not None:
        _panel.refresh_presets()
    publish_project_state()


def _on_project_closed(_event):
    publish_project_state()


def start_plugin():
    global _panel, _dock, _timer, _log_handler
    _log_handler = log_module.install_callable_sink(_emit_to_painter)
    _panel = RuriBridgePanel()
    _dock = substance_painter.ui.add_dock_widget(_panel)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectEditionEntered, _on_project_ready)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectClosed, _on_project_closed)
    _timer = QtCore.QTimer(_panel)
    _timer.timeout.connect(_on_timer)
    _timer.start(POLL_MILLISECONDS)
    try:
        CONNECTION.open(default_session())
        _panel.attach_button.setText("Detach")
        _panel.set_status("attached: {0}".format(CONNECTION.arena.directory))
        _panel.refresh_slots()
    except Exception as error:
        LOG.error("could not attach on start: %s", error)
        _panel.set_status("attach failed: {0}".format(error))
    LOG.info("plugin started from %s", REPOSITORY_ROOT)


def close_plugin():
    global _panel, _dock, _timer, _log_handler
    if _timer is not None:
        _timer.stop()
        _timer = None
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectEditionEntered, _on_project_ready)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ProjectClosed, _on_project_closed)
    CONNECTION.close()
    if _dock is not None:
        substance_painter.ui.delete_ui_element(_dock)
        _dock = None
    _panel = None
    if _log_handler is not None:
        log_module.root().removeHandler(_log_handler)
        _log_handler = None
