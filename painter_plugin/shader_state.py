# -*- coding: utf-8 -*-
"""Painter's viewport shader instances, reached through its JavaScript engine.

The Python API has no shader surface at all -- ``substance_painter`` ships no
``shaders`` module -- so shader instances, their parameters and which Texture Set
uses which instance live only behind ``alg.shaders``. ``substance_painter.js`` is
itself part of the Python API and already returns parsed JSON rather than text,
so calling through it is Python calling Painter, not a second scripting language
in the design.

The engine behind it is the legacy one, so the snippets here stay ES5: ``var``,
plain functions, no template literals.

Nothing here knows a parameter name. A push offers whatever the other side calls
a material data row, and the intersection with what the shader actually exposes
is computed against ``alg.shaders.parameters`` -- the shader's own answer. Names
it does not have are reported back rather than dropped, because a silently
ignored uniform is indistinguishable from one that had no effect.
"""

from __future__ import annotations

import json

import substance_painter.js

from ruri_bridge.log import logger

LOG = logger("painter.shaders")

_LINE_SEPARATORS = {0x2028: "\\u2028", 0x2029: "\\u2029"}


class ShaderStateError(RuntimeError):
    """A shader query or assignment the JavaScript engine refused."""


def _literal(value):
    """A JSON value safe to paste into ES5 source.

    U+2028 and U+2029 are ordinary characters inside a JSON string but line
    terminators in ES5 source, so a material row carrying one would end the
    statement in the middle of a string literal.
    """
    return json.dumps(value, ensure_ascii=True).translate(_LINE_SEPARATORS)


def evaluate(code):
    try:
        return substance_painter.js.evaluate(code)
    except RuntimeError as error:
        raise ShaderStateError("{0}\nwhile evaluating: {1}".format(error, code))


def instances():
    """Every shader instance in the open project."""
    return evaluate("alg.shaders.instances()")


def parameters(shader_id):
    """One instance's parameters, keyed by identifier."""
    return evaluate("alg.shaders.parameters({0})".format(int(shader_id)))


def assignment():
    """Painter's own description of instances and the Texture Sets on them."""
    return evaluate("alg.shaders.shaderInstancesToObject()")


def set_parameters(shader_id, values):
    """Assign parameter values to one instance."""
    evaluate("alg.shaders.setParameters({0}, {1})".format(int(shader_id), _literal(values)))


def update_shader(shader_id, shader_url):
    """Swap the shader an instance runs, keeping the instance and its Texture Sets."""
    evaluate("alg.shaders.updateShaderInstance({0}, {1})".format(
        int(shader_id), _literal(shader_url)))


def read_state():
    """Everything the other side needs to know about this project's shaders."""
    found = instances()
    return {
        "instances": found,
        "parameters": {str(entry["id"]): parameters(entry["id"]) for entry in found},
        "assignment": assignment(),
    }


def parameter_values():
    """Just the values, for watching. Small enough to ask for on a timer.

    ``parameters()`` carries every parameter's full description -- labels, help
    text, widget hints -- which is tens of kilobytes per instance and pointless
    to re-read while looking for a changed number. The assignment object holds
    the same values with none of that.
    """
    return {label: body.get("parameters", {})
            for label, body in assignment().get("shaders", {}).items()}


def instance_by_texture_set():
    """Texture Set identity -> shader instance id.

    Painter states this in two halves: the assignment names, per Texture Set, the
    shader instance *label* it uses, and the instance list carries the id every
    other call wants. Joining them here keeps that two-step in one place.

    Keyed by identity rather than by the displayed name, because the other side
    speaks identities -- a Texture Set renamed on either side has to stay the
    same Texture Set.
    """
    import substance_painter.textureset

    identifier_by_label = {entry["label"]: entry["id"] for entry in instances()}
    identity_by_display = {texture_set.name(): texture_set.original_name
                           for texture_set in substance_painter.textureset.all_texture_sets()}
    mapping = {}
    for display, body in assignment().get("texturesets", {}).items():
        label = body.get("shader")
        identity = identity_by_display.get(display, display)
        if label in identifier_by_label:
            mapping[identity] = identifier_by_label[label]
        else:
            LOG.warning("Texture Set %r names shader instance %r, which is not in the "
                        "instance list", display, label)
    return mapping


def _coerce(value, data_type):
    """Fit one offered value to a parameter's declared type, or refuse it.

    The arity is read off the type name rather than looked up, so a shader with a
    Float4 uniform needs nothing added here.
    """
    digits = "".join(character for character in data_type if character.isdigit())
    arity = int(digits) if digits else 1
    kind = data_type[:len(data_type) - len(digits)] if digits else data_type
    if arity == 1:
        if kind == "Bool":
            return isinstance(value, (bool, int, float)), bool(value)
        if kind == "Int":
            return isinstance(value, (bool, int)), int(value)
        if kind == "Float":
            return isinstance(value, (bool, int, float)), float(value)
        if kind == "String":
            return isinstance(value, str), value
        return False, value
    if not isinstance(value, (list, tuple)) or len(value) != arity:
        return False, value
    if not all(isinstance(component, (bool, int, float)) for component in value):
        return False, value
    caster = int if kind == "Int" else float
    return True, [caster(component) for component in value]


def apply_by_texture_set(values_by_texture_set, shader_url_by_texture_set=None):
    """Set what the shader on each Texture Set actually exposes; report the rest.

    Several Texture Sets share one shader instance until somebody gives them
    different shaders, so an offer aimed at two of them lands on the same
    uniforms. Where those two disagree on a value, neither is written: taking one
    silently would make the viewport show a number nobody asked for.
    """
    identifier_by_set = instance_by_texture_set()
    for texture_set, url in (shader_url_by_texture_set or {}).items():
        identifier = identifier_by_set.get(texture_set)
        if identifier is None:
            LOG.warning("no shader instance for Texture Set %r; shader not swapped",
                        texture_set)
            continue
        update_shader(identifier, url)
    identifier_by_set = instance_by_texture_set()

    offers = {}
    report = {"applied": {}, "unknown": {}, "mismatched": {}, "conflicting": {},
              "unmapped": sorted(set(values_by_texture_set) - set(identifier_by_set))}
    for texture_set, values in values_by_texture_set.items():
        identifier = identifier_by_set.get(texture_set)
        if identifier is None:
            continue
        exposed = parameters(identifier)
        for name, value in values.items():
            if name not in exposed:
                report["unknown"].setdefault(texture_set, []).append(name)
                continue
            accepted, coerced = _coerce(value, exposed[name]["description"]["dataType"])
            if not accepted:
                report["mismatched"].setdefault(texture_set, []).append(
                    "{0} expects {1}".format(name, exposed[name]["description"]["dataType"]))
                continue
            claimed = offers.setdefault(identifier, {})
            if name in claimed and claimed[name] != coerced:
                report["conflicting"].setdefault(str(identifier), []).append(name)
                continue
            claimed[name] = coerced

    for identifier, values in offers.items():
        for name in report["conflicting"].get(str(identifier), []):
            values.pop(name, None)
        if values:
            set_parameters(identifier, values)
            report["applied"][str(identifier)] = sorted(values)
    return report
