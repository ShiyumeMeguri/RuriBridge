# -*- coding: utf-8 -*-
"""A retarget of a project: its document repaired for the surface it holds now, given the one it was painted on."""

from __future__ import annotations

from .. import murmur, project, scene, stream
from . import engine, geometry

_SCENE_UID = "sceneMaterialUID"
_MASK = 0xFFFFFFFFFFFFFFFF


def renewed_scene_uid(material, taken):
    """A scene uid for ``material`` that nothing in ``taken`` has, derived from its current one.

    A project keeps what it rendered of each material in a cache named by this uid; a material given a new one is
    rendered again from its layers, which is what a material whose paint was just repaired needs.
    """
    value = murmur.hash128("{0}:renewed".format(material.number(_SCENE_UID)).encode("ascii"))[0]
    while value in taken or value == 0:
        value = (value + 1) & _MASK
    return value


def run(target, old_scene, names, anchor, uv_set=0):
    """Repair the materials called ``names`` of the open, writable project ``target``.

    ``old_scene`` is the bytes of the ``scene.assbin`` the paint was made on, the project's own is the surface
    it is on now, and ``anchor`` (``engine.UV`` or ``engine.SURFACE``) says what stayed. Each repaired material
    gets a new scene uid, so that the project renders it again instead of showing what it had cached.
    Returns what each material's passes did.
    """
    old = geometry.Surface.of(scene.read(old_scene), uv_set)
    new = geometry.Surface.of(scene.read(target.read(project.SCENE)), uv_set)
    document = stream.read(target.read(project.DOCUMENT))
    found = {material.text("sceneMaterialName"): material for material in document.root["materials"]}
    missing = [name for name in names if name not in found]
    if missing:
        raise ValueError("the project has no material called {0}".format(", ".join(missing)))
    report = {name: engine.run(found[name], old, new, anchor) for name in names}
    taken = {material.number(_SCENE_UID) for material in document.root["materials"]}
    for name in names:
        fresh = renewed_scene_uid(found[name], taken)
        taken.add(fresh)
        found[name].set_number(_SCENE_UID, fresh)
    target.write(project.DOCUMENT, stream.write(document))
    return report
