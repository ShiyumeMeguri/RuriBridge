# -*- coding: utf-8 -*-
"""Taking a performance out of the session and onto the rig that is already here.

The model is not among the things that arrive: this application is where it is
authored, so nobody else states one. What arrives is a performance, and it is
applied onto the rig this document has been animating. Importing the payload's
skeleton instead would leave a second, differently-oriented copy of those bones
beside the first, and every round trip would rotate the axes a little further.
So the import is a means, not the result: what comes in is read for its action,
the action is re-keyed onto the rig by name, and everything the importer built is
removed again.

Nothing here parses glTF. Blender's own importer is the reader, because a second
reader for a format the application already reads is a second set of rounding.
"""

from __future__ import annotations

import bpy

from ...Kernel.log import logger

LOG = logger("blender.glb")


def _before():
    return {one.name for one in bpy.data.objects}


def _arrivals(known):
    return [one for one in bpy.data.objects if one.name not in known]


def _action_of(objects):
    for one in objects:
        animation = getattr(one, "animation_data", None)
        if animation is not None and animation.action is not None:
            return one, animation.action
    return None, None


def _bone_channels(action):
    """Which bones an action actually writes, by name.

    Read off the data paths rather than off the rig it arrived on: the names are
    the whole of what crosses, and the rig it arrived on is about to be deleted.
    """
    names = set()
    for curve in action.fcurves:
        path = curve.data_path
        if path.startswith('pose.bones["') and '"]' in path:
            names.add(path.split('"')[1])
    return names


def apply_performance(path, rig, name=None):
    """Put the payload's performance onto ``rig``, then take the payload away.

    Returns what landed and what did not, by name, because a bone the payload
    animates that this rig does not have is the one thing worth saying out loud:
    it means the two sides disagree about the skeleton, and silence would let
    that pass as a performance that simply did not move.
    """
    if rig is None or rig.type != "ARMATURE":
        raise RuntimeError(
            "a performance is applied onto a rig, and the active object is "
            "{0}".format("nothing" if rig is None else rig.type.lower()))
    known = _before()
    bpy.ops.import_scene.gltf(filepath=str(path))
    arrived = _arrivals(known)
    try:
        source, action = _action_of(arrived)
        if action is None:
            raise RuntimeError("the payload carries no animation")
        animated = _bone_channels(action)
        here = {bone.name for bone in rig.pose.bones}
        missing = sorted(animated - here)
        if rig.animation_data is None:
            rig.animation_data_create()
        action.name = name or action.name
        action.use_fake_user = True
        rig.animation_data.action = action
        LOG.info("applied %r onto %s: %d bone(s) matched, %d did not",
                 action.name, rig.name, len(animated & here), len(missing))
        return {"action": action.name, "matched": sorted(animated & here),
                "missing": missing, "source": source.name if source else ""}
    finally:
        for one in arrived:
            if one.name in bpy.data.objects:
                bpy.data.objects.remove(one, do_unlink=True)
