# -*- coding: utf-8 -*-
"""The textures a generated shader samples, written as a recipe over what Painter exported.

Painter paints channels; the material on the other side samples the textures it was
generated against -- a base map with opacity in its alpha, a packed normal, a gloss
map whose alpha is one minus roughness. Which channel feeds which lane of which
texture, through which operation, is not decided here: it is the shader manifest's
``reads``, the very table the shader on this side stands those textures up from while
it draws. What this module adds is only where each input sits in this export -- a
channel under its own name, an input with a baked mesh map under the map Painter
combines it into -- so the other side can do per pixel what the shader does per
fragment, and get what Painter shows.

A texture the manifest reads verbatim (a ramp, a lookup) is not painted here and gets
no recipe: the material keeps its own.
"""

from __future__ import annotations

#: What the shader's getter of an input with a baked mesh map reads, under the name
#: Painter's export writes it: the tangent-space normal with the baked normal and the
#: height folded in, and the occlusion channel times the baked occlusion. Painter's own
#: vocabulary, the same two the getters call.
BAKED_EXPORTS = {"Normal": "Normal_OpenGL", "AO": "AO_Mixed"}

#: The lane operations the other side carries out. A manifest naming another leaves the
#: texture it is in without a recipe, by name, rather than standing it up wrong.
OPERATIONS = ("", "invert", "repack_normal")


def _exported(maps):
    """Each exported map that holds one source alone, by that source."""
    found = {}
    for entry in maps:
        sources = entry["source_channels"]
        if len(sources) == 1:
            found[sources[0]] = entry["channel"]
    return found


def recipes(manifest, maps):
    """``({slot: [lane]}, {slot: why not})`` for this Texture Set's export.

    A lane is ``{"map", "component", "operation", "encoding", "absent"}`` when an input
    feeds it, ``map`` empty when this stack has no such channel; ``{"raw", "component",
    "absent"}`` when it is a lane of the material's own texture the shader keeps
    verbatim.
    """
    exported = _exported(maps)
    inputs = {entry["Id"]: entry for entry in manifest.get("inputs") or []}
    slots = {}
    refused = {}
    for read in manifest.get("reads") or []:
        if not read.get("Reshaped"):
            continue
        slot = read["Source"]
        lanes = []
        for lane in read["Lanes"]:
            entry = inputs.get(lane["Input"])
            if entry is None:
                refused[slot] = "it reads {0}, which the manifest never declares".format(
                    lane["Input"])
                break
            if entry["Kind"] == "RawTexture":
                own = next((source for source in entry["Sources"] if source["Source"] == slot),
                           None)
                if own is None:
                    refused[slot] = "{0} keeps lanes of another texture".format(lane["Input"])
                    break
                lanes.append({"raw": own["Channels"], "component": int(lane["Component"]),
                              "absent": float(lane["Absent"])})
                continue
            if lane["Operation"] not in OPERATIONS:
                refused[slot] = "lane operation {0!r} is not one the other side carries out".format(
                    lane["Operation"])
                break
            baked = entry.get("MeshMap") or ""
            if baked and baked not in BAKED_EXPORTS:
                refused[slot] = "{0} reads a baked {1} map this export does not write".format(
                    lane["Input"], baked)
                break
            source = BAKED_EXPORTS[baked] if baked else entry["Id"]
            lanes.append({"map": exported.get(source, ""), "component": int(lane["Component"]),
                          "operation": lane["Operation"], "encoding": entry["Encoding"],
                          "absent": float(lane["Absent"])})
        else:
            slots[slot] = lanes
    return slots, refused
