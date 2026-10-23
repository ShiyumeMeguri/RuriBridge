# -*- coding: utf-8 -*-
"""A delivery of cut textures, taken in one Texture Set at a time.

``material_seed.apply`` puts a Texture Set in place; after every one that took writes the
engine is waited for (``engine_barrier``), so it never has more than one Texture Set to
compute. A delivery is taken in hand when it arrives and finished on the bridge's ticks,
one Texture Set a tick: Painter answers nothing while it waits for the engine, so the
status line moves between Texture Sets.
"""

from __future__ import annotations

from ...Kernel.log import logger

from . import engine_barrier, held_imports, material_seed

LOG = logger("painter.seeding")


class _Delivery:
    """What is left of one delivery, and what became of the rest."""

    __slots__ = ("directory", "pending", "total", "reports", "failed", "owed", "inside")

    def __init__(self, directory, entries):
        self.directory = directory
        self.pending = list(entries)
        self.total = len(self.pending)
        self.reports = {}
        self.failed = {}
        self.owed = None
        self.inside = False


_current = [None]


def running():
    """Whether a delivery is still being taken in."""
    return _current[0] is not None


def begin(generation):
    """Take a delivery of cut textures in hand. Returns one line about it.

    Every file is held first: the transport retires a delivery while the Texture Sets
    after the first are still to come."""
    if running():
        raise RuntimeError("a delivery is still being taken in")
    directory = str(generation.directory)
    entries = list(generation.record["texture_sets"])
    for entry in entries:
        for item in entry["inputs"]:
            held_imports.delivered(directory, item)
    delivery = _Delivery(directory, entries)
    if not entries:
        return _summary(delivery)
    _current[0] = delivery
    return "taking {0} Texture Set(s) in from Blender".format(delivery.total)


def cancel():
    """The project went away: what is left of the delivery has nowhere to go."""
    delivery = _current[0]
    _current[0] = None
    if delivery is not None and delivery.pending:
        LOG.warning("the project went away with %d Texture Set(s) of a delivery not taken in",
                    len(delivery.pending))


def advance(is_locked):
    """Put the next Texture Set of the delivery in place and wait for the engine to compute
    it. Returns one line about where the delivery stands; ``running`` is False once it is done.

    ``is_locked`` says whether an error is Painter holding the project for a save: that one
    is raised again, and the same step is tried on the next tick. Waiting for the engine can
    run the application's event loop, and so the next tick: a tick that finds a step under
    way leaves it be."""
    delivery = _current[0]
    if delivery.inside:
        return "waiting for Painter's engine"
    delivery.inside = True
    try:
        return _step(delivery, is_locked)
    finally:
        delivery.inside = False


def _step(delivery, is_locked):
    if delivery.owed is None:
        delivery.owed = _owed(_take(delivery, delivery.pending[0], is_locked))
    while delivery.owed:
        function, arguments = delivery.owed[0]
        try:
            function(*arguments)
        except Exception as error:
            if is_locked(error):
                raise
            _current[0] = None
            LOG.error("the engine could not be waited for, so the %d Texture Set(s) still to come "
                      "are not taken in: %s", len(delivery.pending) - 1, error)
            return "the engine could not be waited for: {0}".format(error)
        delivery.owed.pop(0)
    delivery.owed = None
    delivery.pending.pop(0)
    if delivery.pending:
        return "set up {0} of {1} Texture Set(s) from Blender".format(
            len(delivery.reports) + len(delivery.failed), delivery.total)
    _current[0] = None
    return _summary(delivery)


def _take(delivery, entry, is_locked):
    """Put one Texture Set in place. Returns what was done, or None when it could not be."""
    try:
        report = material_seed.apply(entry, delivery.directory)
    except Exception as error:
        if is_locked(error):
            raise
        LOG.error("%s could not be set up from Blender: %s", entry["name"], error)
        delivery.failed[entry["name"]] = str(error)
        return None
    delivery.reports[entry["name"]] = report
    return report


def _owed(report):
    """What the engine is still owed for one Texture Set, in order: a wait for whatever was
    written, and when the stack itself was written, the bridge's layer touched."""
    if report is None or not (report["stack_writes"] or report["parameter_writes"]):
        return []
    owed = [(engine_barrier.settle, ())]
    if report["stack_writes"] and report["layer"] is not None:
        owed.append((engine_barrier.republish, (report["layer"],)))
    return owed


def _summary(delivery):
    reports = delivery.reports.values()
    changed = sum(1 for report in reports if report["stack_writes"] or report["parameter_writes"])
    line = ("set up {0} Texture Set(s) from Blender, {1} of them changed: {2} channel(s) in the "
            "Blender layer, {3} mesh map(s), {4} shader texture(s)".format(
                len(delivery.reports), changed,
                sum(len(report["channels"]) for report in reports),
                sum(len(report["mesh_maps"]) for report in reports),
                sum(len(report["parameters"]) for report in reports)))
    missing = sum(len(report["missing"]) for report in reports)
    if missing:
        line += "; {0} input(s) not set (see the log)".format(missing)
    if delivery.failed:
        line += "; failed: " + ", ".join(sorted(delivery.failed))
    return line
