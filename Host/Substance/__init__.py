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
import time

from PySide6 import QtCore, QtGui, QtWidgets

import substance_painter.event
import substance_painter.exception
import substance_painter.logging
import substance_painter.project
import substance_painter.textureset
import substance_painter.ui

from ...Kernel import arena as arena_module
from ...Kernel import host as host_port
from ...Kernel import log as log_module
from ...Kernel import peers as peers_module
from ...Kernel import record as record_module
from ...Kernel import session as session_module
from ...Kernel import sync as sync_module
from ...Kernel import topic as topic_module

from . import mesh_ingest, shader_state, texture_ingest, texture_publish

LOG = log_module.logger("painter")

SESSION_ENVIRONMENT_VARIABLE = "RURI_BRIDGE_SESSION"

#: Painter holds a lock on the project while it writes it out, and answers
#: nothing at all while it does. An autosave to the recovery folder takes that
#: lock like any other save, so it lands on an ordinary timer tick with no
#: warning. There is nothing to ask: ``is_busy`` is a different state and stays
#: False right through it, and the lock has no query of its own -- the only way
#: to learn is to be told, by name, in the exception.
PROJECT_LOCKED_PHRASE = "locked"


def project_is_locked(error):
    return (isinstance(error, substance_painter.exception.ProjectError)
            and PROJECT_LOCKED_PHRASE in str(error).lower())


def package_root():
    """Where the package this leg came from actually is.

    Resolved, because Painter reaches it through a directory junction: the
    unresolved path is the junction's, which says nothing about which checkout is
    live, and telling two checkouts apart is the whole reason to log it.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    return os.path.dirname(os.path.dirname(here))


#: How often the bridge looks at the mapped control block. This is the grain of
#: everything it notices, so it wants to be shorter than somebody can perceive --
#: an edit over there should be here before they look up. An idle tick reads a
#: dozen integers out of pages this process already has mapped and asks the
#: application two questions about itself: measured at 0.05 ms, which at this
#: interval is under a tenth of a percent of one core. Everything expensive is
#: behind a change gate, so a shorter interval buys latency and not work.
POLL_MILLISECONDS = 60
DEFAULT_TEXTURE_RESOLUTION = 2048
MESH_LOAD_DEADLINE_SECONDS = 600.0
SHADER_QUIET_SECONDS = 0.35
TEXTURE_QUIET_SECONDS = 0.9
#: The share of the thread this poll runs on that it may take. The thread it runs
#: on is the one that paints, and the cost is not reducible: reading this
#: application's shader values takes about a second on a character with sixteen
#: Texture Sets on a generated shader, and asking it only whether they *changed*
#: -- a digest computed inside the application, so that two numbers cross instead
#: of the whole object -- was measured at the same 1059 ms. The second is the
#: application's own bookkeeping, not the crossing, so there is nothing to make
#: cheaper and only the spacing left to choose.
#:
#: A one-second stall is not five percent of anything a person experiences, it is
#: a hitch, so the spacing is chosen to make it rare rather than to keep an
#: average low. Turn the whole watch off in the panel if even that is too often;
#: it is the leg that carries this application's own knobs back, and a model
#: authored on the other side does not take them anyway.
SHADER_POLL_DUTY = 60.0
#: However cheap the answer is, never ask more often than this many pump ticks.
#: A poll whose result cannot leave before the next one is work with nowhere to
#: go, and the pump's own period is short enough now that "one tick" would let a
#: cheap project poll sixteen times a second for an answer that changes when
#: somebody drags a slider.
SHADER_POLL_FLOOR_TICKS = 4.0
#: A cost has to move by this much, in milliseconds AND by this share of what it
#: was, before it is worth saying so. Either test alone fails at one end: a pure
#: ratio calls one millisecond against three a 200% change and says so every
#: time, and a pure floor calls half a second against nine hundred milliseconds
#: news and also says so every time. A measurement that wanders inside its own
#: noise is not a measurement changing.
SHADER_POLL_REPORT_MILLISECONDS = 25.0
SHADER_POLL_REPORT_SHARE = 0.5

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


PEER = peers_module.SUBSTANCE


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
            _panel.refresh_slots()

    def receive(self, topic, generation):
        return _handle(topic, generation)

    def collect(self, topic):
        if topic is topic_module.SHADING and substance_painter.project.is_open():
            return shader_state.values_by_texture_set()
        return None


HOST = host_port.bind(SubstanceHost())


class _Connection:
    """The one live attachment this Painter process holds."""

    def __init__(self):
        self.session = None

    @property
    def is_open(self):
        return self.session is not None

    @property
    def arena(self):
        return self.session.arena if self.session is not None else None

    def open(self, session):
        self.close()
        self.session = session_module.Session.open(
            HOST.name, HOST.capabilities, session=session)
        self._adopt_pending_work()
        for endpoint in self.session.sources(topic_module.SHADING):
            endpoint.reader.skip_to_latest()
        for endpoint in self.session.sources(topic_module.PRESENCE):
            endpoint.reader.skip_to_latest()
        LOG.info("attached to session %s at %s", session, self.session.arena.directory)
        return self.session.arena

    def publisher(self, topic):
        return self.session.publisher(topic)

    def writer(self, topic):
        return self.session.writer(topic)

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
        for one in topic_module.TOPICS:
            for endpoint in self.session.sources(one):
                if one.kind != topic_module.QUEUED:
                    continue
                if busy or one is not topic_module.MESH:
                    endpoint.reader.skip_to_latest()
                else:
                    endpoint.reader.catch_up(None)

    def close(self):
        if self.session is not None:
            self.session.close()
        self.session = None


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
    return CONNECTION.writer(topic_module.PRESENCE).write(state)


def publish_shader_state():
    """Tell the others what this project's shaders are set to, and which they are.

    The shape and the values are one record on one state topic: anything reading
    the values needs the shape to make sense of them -- and the shape is a shader
    name per Texture Set, not the shader's whole declaration.
    """
    if not CONNECTION.is_open or not substance_painter.project.is_open():
        return None
    return CONNECTION.writer(topic_module.SHADING).write(record_module.shading(
        HOST.name, shader_state.values_by_texture_set(),
        shader_name_by_texture_set=shader_state.shader_by_texture_set()))


def publish_textures(preset_name):
    if not CONNECTION.is_open:
        raise RuntimeError("not attached to a bridge session")
    return texture_publish.publish(
        CONNECTION.arena, CONNECTION.publisher(topic_module.TEXTURES), preset_name)


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
        self.binding_label = QtWidgets.QLabel("")
        self.binding_label.setWordWrap(True)
        self.binding_label.setToolTip(
            "The scene this project paints, read from the project's own metadata. It "
            "is saved with the project, so reopening it later still says this")
        layout.addWidget(self.binding_label)

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
            values = shader_state.values_by_texture_set()
            _shader_gate.prime(values)
            CONNECTION.writer(topic_module.SHADING).write(
                record_module.shading(HOST.name, values))
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
        generation = CONNECTION.publisher(topic_module.REQUEST).publish_record(
            record_module.request(HOST.name, record_module.ASK_FOR_MESH))
        self.set_status("asked Blender for the scene, generation {0}".format(
            generation.number))

    def set_status(self, message):
        self.status_label.setText(message)

    def refresh_slots(self):
        if not CONNECTION.is_open:
            self.slot_label.setText("")
            return
        self.link_label.setText(", ".join(
            "{0} {1}".format(name, "attached" if here else "away")
            for name, here in CONNECTION.session.attendance() if name != HOST.name))
        self.binding_label.setText(self._describe_binding())
        self.slot_label.setText("\n".join(
            "{0}: gen {1} ack {2} drop {3}".format(
                state.channel, state.generation, state.acknowledged_generation,
                state.dropped_generations)
            for state in CONNECTION.arena.describe()))

    def _describe_binding(self):
        """Which scene this project paints, in the words of the project itself."""
        if not substance_painter.project.is_open():
            return ""
        binding = mesh_ingest.stored_binding()
        document = binding.get("document")
        if document:
            return "painting {0}".format(document)
        if binding.get("scene_identity"):
            return "painting an unsaved Blender file"
        return "not bound to a scene yet"

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
#: Something happened that changes what this project is -- a save, most of the
#: time -- and it happened at a moment the project could not be asked about it.
#: The pump says it on the first tick that can.
_project_state_due = False
_pending_display_names = {}
#: The generation whose textures still have to be put in, once the project holds
#: the mesh they came with. Cleared when they are applied, so a reload that
#: carries none does not re-apply the previous send's.
_pending_textures = [None]
#: An offer that named a shader this application did not have. Kept so it can be
#: applied when the shelves finish discovering their resources -- the sending
#: side publishes state once and has no reason to say it again.
_offer_awaiting_a_shader = [None]


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
        CONNECTION.arena, CONNECTION.publisher(topic_module.TEXTURES),
        _panel.preset_box.currentText(), sorted(dirty), dirty)
    _panel.set_status("live: sent {0} of {1}".format(
        ", ".join(sorted({channel for names in dirty.values() for channel in names})),
        ", ".join(sorted(dirty))))
    return generation


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
    values = shader_state.values_by_texture_set()
    cost = time.monotonic() - started
    interval = max(cost * SHADER_POLL_DUTY,
                   POLL_MILLISECONDS / 1000.0 * SHADER_POLL_FLOOR_TICKS)
    _shader_poll_due = time.monotonic() + interval
    moved = (_shader_poll_cost is None
             or abs(cost - _shader_poll_cost) > max(
                 SHADER_POLL_REPORT_MILLISECONDS / 1000.0,
                 SHADER_POLL_REPORT_SHARE * _shader_poll_cost))
    if moved:
        _shader_poll_cost = cost
        LOG.info("watching shader values costs %.0f ms; asking again in %.1f s",
                 cost * 1000.0, interval)
    if _shader_gate.should_publish(values):
        CONNECTION.writer(topic_module.SHADING).write(
            record_module.shading(HOST.name, values))
        _panel.set_status("live: sent shader values")


def _handle(topic, generation):
    """Apply one arrival. Returns True when Painter is now busy with it."""
    global _mesh_deadline
    if topic is topic_module.MESH:
        _pending_textures[0] = generation if generation.record.get("textures") else None
        _pending_display_names.clear()
        _pending_display_names.update(
            {row["identity"]: row["name"] for row in generation.record.get("materials", [])
             if row.get("identity")})
        _mesh_deadline = time.monotonic() + MESH_LOAD_DEADLINE_SECONDS
        try:
            intent = mesh_ingest.apply(generation, _panel.texture_resolution,
                                       ask_for_names=_ask_blender_for_names)
        except Exception:
            _mesh_deadline = None
            raise
        _panel.set_status("mesh generation {0}: {1} ({2})".format(
            generation.number, intent, mesh_ingest.describe_scene(generation)))
        return True
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if not topic_module.can_answer(asked, HOST.capabilities):
            return False
        preset = generation.record.get("preset_name") or _panel.preset_box.currentText()
        published = texture_publish.publish(
            CONNECTION.arena, CONNECTION.publisher(topic_module.TEXTURES), preset,
            generation.record.get("texture_sets"))
        _panel.set_status("texture request {0} answered with generation {1}".format(
            generation.number, published.number))
        return False
    raise RuntimeError(
        "nothing here receives {0!r} yet, and the topic says this application "
        "hears it".format(topic.key))


#: How many refused names to name. A material sheet accumulates every property
#: anybody ever set on it, so most of an offer is history the shader never had --
#: hundreds of names per Texture Set, times a scene's worth of them. The count is
#: the fact; a handful of names says which kind they are.
REFUSED_NAMES_SHOWN = 6


def _say_what_was_refused(label, by_texture_set):
    """One line for a whole category, not one line per name per material."""
    if not by_texture_set:
        return
    names = sorted({name for entry in by_texture_set.values() for name in entry})
    LOG.warning("shader values %s: %d name(s) across %d material(s), e.g. %s",
                label, len(names), len(by_texture_set),
                ", ".join(names[:REFUSED_NAMES_SHOWN]))


def take_shader_values():
    """Apply values arriving in the control block, and never echo them back."""
    if not CONNECTION.is_open or not substance_painter.project.is_open():
        return None
    payload = None
    for endpoint, arrived in CONNECTION.session.changed_state():
        if endpoint.topic is topic_module.SHADING:
            payload = arrived
    if payload is None:
        payload = _offer_awaiting_a_shader[0]
        _offer_awaiting_a_shader[0] = None
    if payload is None:
        return None
    return _apply_shader_values(payload)


def _on_shelf_settled(_event):
    """The shelves finished discovering resources. Anything waiting on one?"""
    if _offer_awaiting_a_shader[0] is None or not substance_painter.project.is_open():
        return
    payload = _offer_awaiting_a_shader[0]
    _offer_awaiting_a_shader[0] = None
    try:
        _apply_shader_values(payload)
    except Exception as error:
        LOG.error("could not apply the waiting shader values: %s", error)


def _apply_lookup_textures(lookups):
    """Point the shader's own texture parameters at the images that just landed.

    These go through the same writer as every other parameter, because to the
    shader they ARE parameters: the value of ``_DiffRampMap`` is the url of a
    resource, the same way the value of ``_Metallic`` is a number. The gate is
    primed with the result so this write does not read back as somebody else's
    change and bounce straight out again."""
    report = shader_state.apply_by_texture_set(lookups)
    for label in ("unknown", "mismatched", "conflicting"):
        _say_what_was_refused(label, report[label])
    if report["unmapped"]:
        LOG.warning("lookup textures for %d material(s) this project has no Texture "
                    "Set for: %s", len(report["unmapped"]),
                    ", ".join(report["unmapped"][:4]))
    written = sum(len(names) for names in report["applied"].values())
    LOG.info("pointed %d shader texture parameter(s) at the images that arrived", written)
    try:
        _shader_gate.suppress(shader_state.values_by_texture_set())
    except shader_state.ShaderStateError as error:
        LOG.error("could not re-read the shader values after the lookups: %s", error)
    return report


def _apply_shader_values(payload):
    report = shader_state.apply_by_texture_set(
        payload.get("by_texture_set", {}),
        payload.get("shader_url_by_texture_set"),
        payload.get("vocabulary_by_texture_set"),
        payload.get("shader_name_by_texture_set"))
    for texture_set, wanted in sorted(report["wrong_shader"].items()):
        LOG.warning("%s speaks %r and the shader it runs exposes none of it; give that "
                    "Texture Set the shader for %s and the values land",
                    texture_set, wanted, wanted)
    for name, why in sorted(report["no_shader"].items()):
        LOG.warning("a material asks for the shader %r and %s; keeping the offer and "
                    "trying again when the shelves settle", name, why)
    _offer_awaiting_a_shader[0] = payload if report["no_shader"] else None
    refused = [label for label in ("unknown", "mismatched", "conflicting", "unmapped",
                                   "wrong_shader", "no_shader")
               if report[label]]
    for label in ("unknown", "mismatched", "conflicting"):
        _say_what_was_refused(label, report[label])
    if report["unmapped"]:
        LOG.warning("shader values for %d material(s) this project has no Texture Set "
                    "for: %s", len(report["unmapped"]),
                    ", ".join(report["unmapped"][:4]))
    values = shader_state.values_by_texture_set()
    _shader_gate.suppress(values)
    if refused:
        CONNECTION.writer(topic_module.SHADING).write(
            record_module.shading(HOST.name, values))
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
    CONNECTION.session.touch()
    if substance_painter.project.is_busy():
        return
    take_shader_values()
    for endpoint, generation in CONNECTION.session.incoming():
        try:
            became_busy = _handle(endpoint.topic, generation)
        except Exception as error:
            LOG.error("%s generation %d from %s failed and is being skipped: %s",
                      endpoint.topic.key, generation.number, endpoint.peer, error)
            _panel.set_status("generation {0} failed: {1}".format(generation.number, error))
            became_busy = False
        endpoint.reader.acknowledge(generation)
        if became_busy:
            break
    _panel.refresh_slots()


def _on_timer():
    global _project_state_due
    try:
        if _project_state_due and not substance_painter.project.is_busy():
            publish_project_state()
            _project_state_due = False
        pump()
        live_sync()
    except Exception as error:
        if project_is_locked(error):
            return
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
    """The project now holds the mesh that was sent. Adopt its values.

    Priming rather than publishing is what stops a freshly created project's
    defaults from overwriting values the other side authored: on connect neither
    side asserts, and only an actual change afterwards travels.
    """
    global _mesh_deadline
    _mesh_deadline = None
    try:
        _shader_gate.prime(shader_state.values_by_texture_set())
    except shader_state.ShaderStateError as error:
        LOG.error("could not adopt the shader values: %s", error)
    if _pending_display_names:
        texture_publish.apply_display_names(_pending_display_names)
    arrived = _pending_textures[0]
    _pending_textures[0] = None
    if arrived is not None:
        try:
            report = texture_ingest.apply(arrived)
            if report["lookups"]:
                _apply_lookup_textures(report["lookups"])
            if _panel is not None and report["applied"]:
                _panel.set_status("took {0} texture(s) into {1} Texture Set(s)".format(
                    report["applied"], len(report["sets"])))
        except Exception as error:
            LOG.error("could not put the incoming textures in: %s", error)
    mesh_ingest.save_where_the_scene_asked()
    if _panel is not None:
        _panel.refresh_presets()
    publish_project_state()
    try:
        publish_shader_state()
    except shader_state.ShaderStateError as error:
        LOG.error("could not read the shader state: %s", error)


def _ask_blender_for_names():
    """Say what this project already has, then ask for the scene again.

    The order is the whole point: Blender can only take these Texture Set names
    as identities if it has them, so the state goes first and the request second.
    Clearing the deadline says the mesh is no longer on its way in -- it is being
    sent again, addressed properly this time.
    """
    global _mesh_deadline
    _mesh_deadline = None
    publish_project_state()
    CONNECTION.publisher(topic_module.REQUEST).publish_record(
        record_module.request(HOST.name, record_module.ASK_FOR_MESH))


def _on_project_closed(_event):
    mesh_ingest.forget_asking()
    publish_project_state()


def _on_project_saved(_event):
    """The project may have acquired a path. Say so as soon as it can be asked.

    Saving is the only moment a project acquires a path, and it is somebody
    pressing a key in Painter, not anything the bridge drives. Publishing the
    state is how the other side learns a path it never chose.

    Not from here, though: this fires while Painter still holds the save lock, so
    reading the project raises -- and an autosave fires it too, four times an
    hour, on a project whose path did not change at all. Nothing about a path is
    urgent and the pump runs four times a second, so the read waits for a tick
    where the project is answering.
    """
    global _project_state_due
    _project_state_due = True


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


def _rest_in_the_strip(_event=None):
    """Leave the panel folded into the right-hand strip, not spread down a column.

    A dock that is closed lives in that strip as its icon and costs no room; one
    that is open takes a slice of the column the layer stack is in. Forcing it
    open on every launch would take that choice away for good, so it is folded
    away exactly once -- the first run that knows to -- and after that whatever it
    was left as is what comes back. The way in is the 'RB' button in the strip, or
    Window > RuriBridge.

    Folding waits for the interface to come up, because Painter restores its saved
    layout after the plugins have started: a dock closed any earlier is reopened a
    moment later by the layout that remembers it open, which is exactly what this
    is here to stop.
    """
    if _dock is None:
        return
    settings = QtCore.QSettings()
    key = "python_plugins/RuriBridge/dock_folded_once"
    if not settings.value(key):
        settings.setValue(key, True)
        _dock.setVisible(False)
    LOG.info("panel is %s; the 'RB' button in the right-hand strip and "
             "Window > RuriBridge both open it",
             "open" if _dock.isVisible() else "folded into the strip")


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
    substance_painter.ui.add_action(
        substance_painter.ui.ApplicationMenu.Window, _menu_action)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectEditionEntered, _on_project_ready)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectClosed, _on_project_closed)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ProjectSaved, _on_project_saved)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.GraphicalUserInterfaceStarted, _rest_in_the_strip)
    if substance_painter.ui.get_main_window().isVisible():
        _rest_in_the_strip()
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.TextureStateEvent, _on_texture_state)
    substance_painter.event.DISPATCHER.connect_strong(
        substance_painter.event.ShelfCrawlingEnded, _on_shelf_settled)
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
    LOG.info("plugin started from %s", package_root())


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
        substance_painter.event.GraphicalUserInterfaceStarted, _rest_in_the_strip)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.TextureStateEvent, _on_texture_state)
    substance_painter.event.DISPATCHER.disconnect(
        substance_painter.event.ShelfCrawlingEnded, _on_shelf_settled)
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
