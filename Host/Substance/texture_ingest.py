# -*- coding: utf-8 -*-
"""The textures that arrived with the model, put into the channels they belong in.

A project created from a model and nothing else starts grey, and everything the
other side already had -- the base colour it was rendering with, its normal map --
would sit unused in the payload beside the mesh. This puts them in: one fill layer
per Texture Set, each channel sourced from the image that fed the matching input
over there.

**One layer, named, and reused.** The point is to hand somebody the ground they
paint on, not to own their stack: anything they add goes above it, and a second
send re-sources the same layer instead of stacking another. The name is the only
mark that survives a save and a reopen, so the name is how it is found again.

**Colour space comes from the record**, which took it from the image datablock --
the only thing that knows. Nothing here derives it from the channel or the file
name; both are measured in these tools to be silently wrong, once by a factor of
68 in brightness.

Everything happens inside one ``ScopedModification``: the layer stack recomputes
its textures on every edit otherwise, so a scene's worth of channels would be a
scene's worth of recomputes and a history nobody can undo in one step.
"""

from __future__ import annotations

import substance_painter.colormanagement
import substance_painter.layerstack
import substance_painter.resource
import substance_painter.textureset

from ...Kernel import record as record_module
from ...Kernel.log import logger

LOG = logger("painter.textures")

#: The neutral channel names the record uses, and what this application calls
#: them. The mapping lives on this side because it is this application's
#: vocabulary: the sending side states what an image IS and never learns our
#: spelling for it.
#:
#: The names on the left are the surface semantics the whole toolchain declares
#: its textures in, so a generated material and a hand-built one arrive spelled
#: the same way and only one column here ever has to change.
CHANNELS = (
    ("BaseColor", "BaseColor"),
    ("Metallic", "Metallic"),
    ("Roughness", "Roughness"),
    ("TangentNormal", "Normal"),
    ("Emission", "Emissive"),
    ("Opacity", "Opacity"),
    ("SpecularLevel", "Specular"),
    ("Height", "Height"),
    ("Occlusion", "AmbientOcclusion"),
)

#: What a layer this made is called, and how it is found again.
LAYER_NAME = "RuriBridge base"


class TextureIngestError(RuntimeError):
    """Textures that arrived and could not be put anywhere."""


def _channel_type(name):
    found = getattr(substance_painter.textureset.ChannelType, name, None)
    if found is None:
        raise TextureIngestError("this build has no {0} channel".format(name))
    return found


def _format_for(color_space):
    """A channel wide enough for what is going into it.

    Colour goes in an sRGB channel and data goes in a linear one, and the record
    says which this is -- the image did.
    """
    formats = substance_painter.textureset.ChannelFormat
    return (formats.sRGB8 if color_space == record_module.COLOR_SPACE_SRGB
            else formats.L8)


def _texture_sets_by_name():
    """Every Texture Set, under both names it answers to.

    ``original_name`` is what the mesh called the material and never moves;
    ``name`` is what somebody may have renamed it to. The sender addresses the
    first, so that is the one that matters -- but a project made by hand may only
    match on the second.
    """
    found = {}
    for texture_set in substance_painter.textureset.all_texture_sets():
        for reader in ("original_name", "name"):
            value = getattr(texture_set, reader, None)
            try:
                key = value() if callable(value) else value
            except Exception:
                continue
            if key:
                found.setdefault(str(key), texture_set)
    return found


def _existing_layer(stack):
    for node in substance_painter.layerstack.get_root_layer_nodes(stack):
        try:
            if node.get_name() == LAYER_NAME:
                return node
        except Exception:
            continue
    return None


def _fill_layer(stack):
    """The layer this made, or a new one."""
    found = _existing_layer(stack)
    if found is not None:
        return found
    position = substance_painter.layerstack.InsertPosition.from_textureset_stack(stack)
    layer = substance_painter.layerstack.insert_fill(position)
    layer.set_name(LAYER_NAME)
    return layer


def _set_channel(layer, channel_type, resource_id):
    """Point one channel of the fill at one imported image."""
    layer.set_source(channel_type, resource_id)


def _field(identifier, name):
    """One field of a resource identifier, whether or not it is wrapped."""
    attribute = getattr(identifier, name, None)
    try:
        return attribute() if callable(attribute) else attribute
    except TypeError:
        return attribute


def _imported(path):
    """One image in this project's own shelf, and the url that names it.

    A live bridge sends the same ramp again on every push, so an import that
    always imports would grow the shelf a copy at a time. Same name, same
    project, same resource.
    """
    stem = path.stem
    try:
        for found in substance_painter.resource.search(stem):
            identifier = found.identifier()
            if _field(identifier, "name") == stem and _field(identifier, "context") == "project":
                return found, _field(identifier, "url") or str(identifier)
    except Exception:
        pass
    resource = substance_painter.resource.import_project_resource(
        str(path), substance_painter.resource.Usage.TEXTURE)
    identifier = resource.identifier()
    return resource, _field(identifier, "url") or str(identifier)


def apply(generation):
    """Put the payload's textures into the open project.

    Two destinations, because the payload names two kinds of texture. A surface
    channel becomes a fill layer's source, which is a thing the artist then
    paints over. A LOOKUP -- a ramp, a LUT, an SDF map -- is not paintable
    material: the shader samples it directly through a parameter of its own
    name, so it lands as an imported resource whose url goes to that parameter.
    Putting a ramp in a channel would be a wrong picture in a right-looking
    place; leaving it out entirely leaves the shader sampling black.

    Returns what landed and what did not, by material, so a material this project
    has no Texture Set for is said out loud rather than dropped: it means the two
    sides disagree about the model, and that is worth hearing. ``lookups`` comes
    back as Texture Set identity -> parameter name -> url, for the shader value
    writer to push the same way it pushes every other parameter.
    """
    section = generation.record.get("textures") or {}
    by_material = section.get("by_material") or {}
    if not by_material:
        return {"applied": 0, "sets": [], "homeless": [], "lookups": {}}
    directory = generation.directory / section.get("directory", "")
    known = _texture_sets_by_name()
    applied = 0
    touched = []
    homeless = []
    lookups = {}

    def payload_file(identity, detail):
        path = directory / detail["file"]
        if not path.exists():
            LOG.warning("%s names %s and the payload does not contain it",
                        identity, detail["file"])
            return None
        return path

    with substance_painter.layerstack.ScopedModification("RuriBridge textures"):
        for identity, sections in sorted(by_material.items()):
            texture_set = known.get(identity)
            if texture_set is None:
                homeless.append(identity)
                continue
            channels = sections.get("channels") or {}
            stack = texture_set.get_stack()
            layer = None
            for neutral, painter_name in CHANNELS:
                detail = channels.get(neutral)
                if detail is None:
                    continue
                path = payload_file(identity, detail)
                if path is None:
                    continue
                channel_type = _channel_type(painter_name)
                if not stack.has_channel(channel_type):
                    stack.add_channel(channel_type,
                                      _format_for(detail.get("color_space")))
                resource, _url = _imported(path)
                if layer is None:
                    layer = _fill_layer(stack)
                _set_channel(layer, channel_type, resource.identifier())
                applied += 1
            if layer is not None:
                touched.append(identity)
            for slot, detail in sorted((sections.get("lookups") or {}).items()):
                path = payload_file(identity, detail)
                if path is None:
                    continue
                _resource, url = _imported(path)
                lookups.setdefault(identity, {})[slot] = url
                applied += 1
    if homeless:
        LOG.warning("%d material(s) in the payload have no Texture Set here: %s",
                    len(homeless), ", ".join(homeless[:4]))
    LOG.info("put %d texture(s) into %d Texture Set(s), %d of them lookups the "
             "shader samples by name", applied, len(touched),
             sum(len(entries) for entries in lookups.values()))
    return {"applied": applied, "sets": touched, "homeless": homeless,
            "lookups": lookups}
