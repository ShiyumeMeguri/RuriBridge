# -*- coding: utf-8 -*-
"""The Cascadeur driver: one visit, not a residency.

Every other application here loads a plugin and keeps it, so its leg is a pump.
Cascadeur runs a command and exits. That is not a limitation to work around -- it
is what this application is -- so the leg is shaped like it: **one visit takes
everything owed, answers what was asked, and leaves.** There is no timer, and
:meth:`schedule` refuses rather than pretending, because a driver that quietly
did nothing would turn "the pump never ran" into a silence.

Being summoned is the only thing that makes this application different from the
others, and it is stated once, in the roster (``peers.CASCADEUR.resident``).
Everything else -- which topics it speaks, which it hears, what a channel is
called, when a payload may be retired -- comes out of the same join the other two
go through.

**GLB in both directions**, through ``csc.glb``, which this application ships as
a first-class door (measured on the real installation: ``process_import`` and
``process_export``, with options for animation, selection and frame rate). No
intermediate format and no temporary file: the path handed to it is inside the
session, so the bytes it reads are pages the publisher already wrote.

A performance arrives as **channels against names**, applied onto the rig that is
already here (``is_update_mode``), never as a skeleton to rebuild. Rebuilding one
would hand this application a second, differently-oriented copy of the bones it
is already animating, and every round trip would rotate the axes a little
further.
"""

from __future__ import annotations

import csc

from ...Kernel import arena as arena_module
from ...Kernel import host as host_port
from ...Kernel import log as log_module
from ...Kernel import peers as peers_module
from ...Kernel import record as record_module
from ...Kernel import session as session_module
from ...Kernel import topic as topic_module

LOG = log_module.logger("cascadeur")

PEER = peers_module.CASCADEUR

#: This application works in centimetres and glTF is metres. One constant, used
#: in both directions, so the two conversions cannot drift apart.
CENTIMETRES_PER_METRE = 100.0

#: The scene handed to a command, for the duration of one visit. It is how this
#: application says things to its own user, and it does not outlive the visit.
_SCENE = None


class CascadeurHost(host_port.Host):
    """This application, as the bridge uses it."""

    @property
    def name(self):
        return PEER.name

    @property
    def capabilities(self):
        return PEER.capabilities

    def log(self, level, message):
        if _SCENE is not None:
            reporter = getattr(_SCENE, "error" if level == host_port.ERROR else "info", None)
            if reporter is not None:
                reporter("[RuriBridge] " + str(message))
        getattr(LOG, level if level != host_port.WARNING else "warning")(message)

    def schedule(self, seconds, function):
        raise NotImplementedError(
            "Cascadeur runs a command and exits, so there is no loop to schedule "
            "on; it is summoned per message (peers.CASCADEUR.resident is False) "
            "and a visit does everything a pump would have done")

    def redraw(self):
        """Nothing to redraw: this application has no panel of ours."""

    def receive(self, topic, generation):
        return _receive(topic, generation)

    def collect(self, topic):
        return None


HOST = host_port.bind(CascadeurHost())


def domain_scene():
    application = csc.app.get_application()
    return application.get_scene_manager().current_scene().domain_scene()


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------
def _import_options(with_animation, with_objects, onto_what_is_here):
    options = csc.glb.ImportOptions()
    options.include_animation = with_animation
    options.include_objects = with_objects
    options.is_update_mode = onto_what_is_here
    options.throw_exception = True
    options.scale_factor = CENTIMETRES_PER_METRE
    return options


def _payload_path(generation):
    """The GLB inside a generation, named by the record rather than guessed."""
    name = generation.record.get("scene_file") or record_module.SCENE_FILE_NAME
    path = generation.directory / name
    if not path.exists():
        raise RuntimeError(
            "{0} says its payload is {1!r} and the generation does not "
            "contain it".format(generation.record.get("source"), name))
    return str(path)


def _receive(topic, generation):
    if topic is topic_module.MESH:
        path = _payload_path(generation)
        csc.glb.process_import(
            domain_scene(), path,
            _import_options(with_animation=False, with_objects=True,
                            onto_what_is_here=False))
        return "imported the model from {0}".format(generation.record.get("source"))
    if topic is topic_module.ANIMATION:
        path = _payload_path(generation)
        csc.glb.process_import(
            domain_scene(), path,
            _import_options(with_animation=True, with_objects=False,
                            onto_what_is_here=True))
        return "applied the performance from {0} onto what is already here".format(
            generation.record.get("source"))
    if topic is topic_module.REQUEST:
        asked = generation.record.get("for")
        if asked == record_module.ASK_FOR_ANIMATION:
            return "published {0}".format(
                publish(topic_module.ANIMATION).number)
        raise RuntimeError(
            "{0} asked for {1!r}, which this application does not answer".format(
                generation.record.get("source"), asked))
    raise RuntimeError(
        "nothing here receives {0!r} yet, and the topic says this application "
        "hears it".format(topic.key))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def _export_options(with_animation, selected_only):
    """What leaves: the performance, at this application's own frame rate."""
    options = csc.glb.ExportOptions()
    options.include_animation = with_animation
    options.for_selected_objects = selected_only
    options.throw_exception = True
    options.scale_factor = 1.0 / CENTIMETRES_PER_METRE
    options.normalize_weights = True
    options.remove_empty_nodes = True
    return options


def publish(topic, selected_only=False, connection=None):
    """Write the performance this application currently holds.

    Only the performance: this is where a rig is animated, not where a model is
    authored, and a model published from here could only be a worse copy of the
    one that arrived. The roster says the same thing (no SCENE_GRAPH), so asking
    for any other topic is refused by the session before it reaches here.
    """
    live = connection or CONNECTION
    if not live.is_open:
        raise RuntimeError("not attached to a bridge session")
    publisher = live.session.publisher(topic)
    with publisher.staging() as staging:
        path = staging.path(record_module.SCENE_FILE_NAME)
        csc.glb.process_export(
            domain_scene(), str(path),
            _export_options(with_animation=True, selected_only=selected_only))
        if not path.exists():
            raise RuntimeError(
                "csc.glb.process_export wrote nothing to {0}".format(path))
        # It wrote the file itself, so it is an ordinary one until it is asked to
        # stay resident -- and the reader on the other side maps these pages.
        arena_module.keep_in_memory(path)
        payload = record_module.mesh(
            HOST.name, record_module.INTENT_AUTO,
            {"name": _scene_name()}, [], CENTIMETRES_PER_METRE, "Y")
        payload["kind"] = topic.key
        generation = staging.publish(payload)
    HOST.log(host_port.INFO, "published {0} generation {1} ({2} bytes)".format(
        topic.key, generation.number, path.stat().st_size))
    return generation


def _scene_name():
    return csc.app.get_application().get_scene_manager().current_scene().name()


# ---------------------------------------------------------------------------
# One visit
# ---------------------------------------------------------------------------
class _Connection:
    """One visit's attachment. It does not outlive the visit."""

    def __init__(self):
        self.session = None

    @property
    def is_open(self):
        return self.session is not None

    def open(self, session, root=None):
        self.close()
        self.session = session_module.Session.open(
            HOST.name, HOST.capabilities, session=session, root=root)
        return self.session

    def close(self):
        if self.session is not None:
            self.session.close()
        self.session = None


CONNECTION = _Connection()


def visit(scene=None, session="default", root=None, publish_topic=None,
          selected_only=False):
    """Attach, take everything owed, publish if asked, detach.

    Everything owed rather than only the newest: this application is not here
    most of the time, so what is waiting is not backlog to skip -- it is exactly
    what was published FOR it while it was away.
    """
    global _SCENE
    _SCENE = scene
    handled = []
    try:
        CONNECTION.open(session, root)
        CONNECTION.session.touch()
        for endpoint, generation in CONNECTION.session.incoming():
            try:
                handled.append((endpoint.topic.key, generation.number,
                                _receive(endpoint.topic, generation)))
            except Exception as error:
                HOST.log(host_port.ERROR, "{0} generation {1} from {2} failed: {3}".format(
                    endpoint.topic.key, generation.number, endpoint.peer, error))
                handled.append((endpoint.topic.key, generation.number, str(error)))
            endpoint.reader.acknowledge(generation)
        CONNECTION.session.writer(topic_module.PRESENCE).write(
            record_module.presence(HOST.name, True, "", "", []))
        if publish_topic is not None:
            handled.append((publish_topic.key, 0,
                            publish(publish_topic, selected_only).number))
        for entry in handled:
            HOST.log(host_port.INFO, "{0} {1}: {2}".format(*entry))
        if not handled:
            HOST.log(host_port.INFO, "nothing was waiting")
    finally:
        CONNECTION.close()
        _SCENE = None
    return handled


# ---------------------------------------------------------------------------
# The plugin lifecycle the other applications have, answered honestly
# ---------------------------------------------------------------------------
def register():
    raise NotImplementedError(
        "Cascadeur has no resident plugin: it is summoned per message and "
        "visit() is the whole of its lifecycle")


def unregister():
    raise NotImplementedError(register.__doc__)


def start_plugin():
    raise NotImplementedError(register.__doc__)


def close_plugin():
    raise NotImplementedError(register.__doc__)
