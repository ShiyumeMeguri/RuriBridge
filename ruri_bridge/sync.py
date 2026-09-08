# -*- coding: utf-8 -*-
"""Deciding when a change is worth publishing, on both sides, by one rule.

Live sync between two applications is three problems, and only the first is
about speed:

* **Noticing.** Neither host tells a plugin "a value changed" for everything the
  bridge carries, so a change is noticed either from an event the host does emit
  or by comparing a cheap fingerprint of the state on a timer.
* **Settling.** A stroke, a slider drag or a vertex tug produces a burst, not one
  change. Publishing per event would send hundreds of generations and, on the
  mesh leg, ask the other side to reload a mesh mid-drag. So a change publishes
  only once the fingerprint has stopped moving for a quiet period.
* **Not echoing.** Two sides that publish whatever they observe will bounce a
  value between them forever: A pushes, B applies, B observes its own new state
  and pushes it back, A applies... The fix is not a timer or a flag that decays;
  it is remembering the exact fingerprint that arrived from the other side and
  refusing to publish that one.

``ChangeGate`` is all three, and it holds no host types, so the mesh leg, the
texture leg and the shader leg all get the same behaviour rather than three
hand-rolled debouncers that drift apart.
"""

from __future__ import annotations

import hashlib
import json
import time

from .log import logger

LOG = logger("sync")

DEFAULT_QUIET_SECONDS = 0.4


def fingerprint(value):
    """A short, stable digest of any JSON-shaped state.

    Sorted keys, so two dictionaries that differ only in insertion order are the
    same state; floats formatted by repr, so a value that round-tripped through
    the wire still matches the one that was sent.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, default=repr).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


class ChangeGate:
    """One watched thing: when it settles, and when it must stay quiet."""

    __slots__ = ("name", "quiet_seconds", "_published", "_suppressed", "_pending",
                 "_pending_since")

    def __init__(self, name, quiet_seconds=DEFAULT_QUIET_SECONDS):
        self.name = name
        self.quiet_seconds = quiet_seconds
        self._published = None
        self._suppressed = None
        self._pending = None
        self._pending_since = 0.0

    def prime(self, current):
        """Adopt the current state as already published, without sending it."""
        self._published = fingerprint(current)
        self._pending = None
        return self._published

    def suppress(self, current):
        """Remember a state that came from the other side, so it is never echoed."""
        digest = fingerprint(current)
        self._suppressed = digest
        self._published = digest
        self._pending = None
        return digest

    def should_publish(self, current, now=None):
        """True once this state has differed from the last publish and settled."""
        now = time.monotonic() if now is None else now
        digest = fingerprint(current)
        if digest == self._published or digest == self._suppressed:
            self._pending = None
            return False
        if digest != self._pending:
            self._pending = digest
            self._pending_since = now
            return False
        if now - self._pending_since < self.quiet_seconds:
            return False
        self._published = digest
        self._suppressed = None
        self._pending = None
        LOG.debug("%s settled after %.2fs", self.name, now - self._pending_since)
        return True
