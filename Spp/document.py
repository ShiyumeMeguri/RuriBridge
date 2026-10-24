# -*- coding: utf-8 -*-
"""What the streams of a project share: objects that can be pointed at.

An object of the document, the viewer settings or the baking settings that something else can name has a
``uid``, a number nothing else in that stream has. Where one object names another it does so in a field of
the reference type, holding the other's uid.
"""

from __future__ import annotations

from . import stream

UID = "uid"
UID_CODE = 0x0C
REFERENCE_CODE = 0x0F
#: The reference that names nothing.
NONE = 0


class DocumentError(ValueError):
    """A stream that does not hold what its schema says it does."""


def _has_uid(found):
    return UID in found and found.code(UID) == UID_CODE


class Uids:
    """Hands out uids for one stream: each above every one the stream already uses."""

    def __init__(self, root):
        self._next = 1 + max((found.number(UID) for found in stream.walk(root) if _has_uid(found)), default=0)

    def take(self):
        value = self._next
        self._next += 1
        return value


def repeated(root):
    """The uids more than one object under ``root`` uses: a stream whose objects cannot be told apart by their uids is one Painter
    cannot open, so nothing that changes a stream is written while this is not empty."""
    seen, found = set(), set()
    for item in stream.walk(root):
        if _has_uid(item):
            uid = item.number(UID)
            (found if uid in seen else seen).add(uid)
    return sorted(found)


def require_unique(root, what):
    """Raise ``DocumentError`` naming ``what`` if two objects under ``root`` share a uid."""
    twice = repeated(root)
    if twice:
        raise DocumentError("{0} would use {1} uid(s) twice, among them {2}".format(what, len(twice), ", ".join(str(uid) for uid in twice[:5])))


def renewed(source, uids):
    """A copy of ``source`` whose every uid is new, and the new uid of each old one: ``(copy, {old: new})``."""
    copy = source.copy()
    mapping = {}
    for found in stream.walk(copy):
        if _has_uid(found):
            old = found.number(UID)
            mapping[old] = uids.take()
            found.set_number(UID, mapping[old])
    return copy, mapping


def references(root):
    """``(object, field, uid)`` for each reference under ``root`` that names something."""
    found = []
    for item in stream.walk(root):
        for field, code, value in item.items():
            if code == REFERENCE_CODE and stream.decode(code, value) != NONE:
                found.append((item, field, stream.decode(code, value)))
    return found
