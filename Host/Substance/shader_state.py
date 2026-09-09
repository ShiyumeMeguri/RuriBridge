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
import substance_painter.resource
import substance_painter.textureset

from ...Kernel.log import logger

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


#: What each instance exposes, remembered against the shader it was running when
#: we read it. Keyed that way rather than by instance alone because somebody can
#: change an instance's shader in the application's own interface, and a cache
#: that could not notice would answer for a shader that is no longer there.
_EXPOSED = {}


def exposed_parameters(shader_id, shader_name):
    """What one instance exposes, read once per shader it runs.

    A parameter list is a declaration, not a state: it changes when the instance
    is given a different shader and at no other time. Re-reading it on every push
    costs a hundred kilobytes of description per Texture Set -- times a scene's
    worth of them, on the main thread -- for an answer that did not move.
    """
    key = (int(shader_id), str(shader_name))
    if key not in _EXPOSED:
        _EXPOSED[key] = parameters(shader_id)
    return _EXPOSED[key]


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
    _EXPOSED.clear()


def assign_instances(layout):
    """Replace the whole instance layout: which instances exist, and who uses them."""
    evaluate("alg.shaders.shaderInstancesFromObject({0})".format(_literal(layout)))
    _EXPOSED.clear()


def _value_of(holder, field):
    """One field of one of this application's objects, however it exposes it.

    Several of these are wrapped so that ``x.name`` and ``x.name()`` both work --
    which means reading the attribute gives a callable, not the value, and
    comparing it to a string never matches. Nothing raises: the answer is simply
    always no, for every resource, forever.
    """
    found = getattr(holder, field, None)
    if found is None:
        return None
    try:
        return found() if callable(found) else found
    except TypeError:
        return found


def shader_named(name):
    """A shader resource this application already has, by name.

    Searched rather than installed. Putting a shader in a shelf is somebody
    else's job -- the generator writes it and the importer that owns this
    application's shelves puts it there -- and a bridge that shipped its own copy
    would be a second place the shader comes from, which is how two of them end
    up differing.
    """
    usage = getattr(substance_painter.resource.Usage, "SHADER", None)
    seen = []
    for query in ("u:shader {0}".format(name), name):
        try:
            found = substance_painter.resource.search(query)
        except Exception as error:
            LOG.warning("searching this application's shelves for %r failed: %s",
                        name, error)
            return None
        for resource in found:
            identifier = _value_of(resource, "identifier")
            if identifier is None:
                continue
            candidate = str(_value_of(identifier, "name") or "")
            usages = _value_of(resource, "usages") if usage is not None else None
            seen.append("{0} ({1})".format(candidate, usages))
            if candidate.lower() != name.lower():
                continue
            if usages is not None and usage not in usages:
                continue
            return _value_of(identifier, "url")
    # What the search DID return, because "not found" and "found and rejected"
    # want different things done about them and read the same in a log.
    LOG.info("looked for a shader called %r and the shelves answered with %d "
             "candidate(s): %s", name, len(seen), ", ".join(seen[:6]) or "nothing")
    return None


def wear_shader(layout, texture_sets, shader_name):
    """Put one shader on these Texture Sets, each on an instance of its own.

    Its own instance per Texture Set, because a shared instance means shared
    uniforms: sixteen materials pointing at one instance can only ever show one
    material's values, and whichever offer arrived last would win silently.

    Returns the sets that ended up wearing it, so the caller can say which did
    not rather than assume they all did.
    """
    url = shader_named(shader_name)
    if url is None:
        return [], "this application has no shader called {0!r} in its shelves".format(
            shader_name)
    shaders = layout.object.setdefault("shaders", {})
    bindings = layout.object.setdefault("texturesets", {})
    display_by_identity = {}
    for texture_set in substance_painter.textureset.all_texture_sets():
        display_by_identity[texture_set.original_name] = texture_set.name

    touched = []
    for identity in sorted(texture_sets):
        display = display_by_identity.get(identity, identity)
        if display not in bindings:
            continue
        if not isinstance(shaders.get(display), dict):
            shaders[display] = {"shader": shader_name, "shaderInstance": display}
        bindings[display] = {"shader": display}
        touched.append((identity, display))
    if not touched:
        return [], "none of the offered Texture Sets are in this project"
    assign_instances(layout.object)

    identifier_by_label = {entry["label"]: entry["id"] for entry in instances()}
    worn = []
    for identity, display in touched:
        found = identifier_by_label.get(display)
        if found is None:
            continue
        update_shader(found, url)
        worn.append(identity)
    LOG.info("put %s on %d Texture Set(s), each on an instance of its own",
             shader_name, len(worn))
    return worn, ""


def shader_by_texture_set():
    """Which shader each Texture Set is running, by Texture Set identity.

    The shape half of a shading record. One name per Texture Set, against the
    thousands of lines of declaration that used to travel in its place.
    """
    layout = Layout()
    return {identity: layout.shader_by_instance.get(identifier, "")
            for identity, identifier in layout.instance_by_texture_set.items()}


def values_by_texture_set():
    """Every watched value, keyed by the Texture Set it belongs to. One read.

    ``parameters()`` carries every parameter's full description -- labels, help
    text, widget hints -- which is tens of kilobytes per instance and pointless
    to re-read while looking for a changed number. The layout object holds the
    same values with none of that.

    Keyed by identity rather than by instance label, because identities are what
    the other side addresses materials with and what every consumer of this
    already wanted. Keying by label and re-keying afterwards meant reading the
    layout twice -- a second each, on this character -- for one question.
    """
    layout = Layout()
    by_instance = {}
    for label, body in (layout.object.get("shaders") or {}).items():
        by_instance[label] = _flat(body.get("parameters") or {})
    found = {}
    for identity, identifier in layout.instance_by_texture_set.items():
        values = by_instance.get(layout.label_by_instance.get(identifier))
        if values:
            found[identity] = values
    return found


def _flat(by_group):
    """Values as names, out of the groups the panel arranges them in.

    This application keys an instance's values by the *group* each parameter was
    declared in and only then by name -- so a shader that declares groups hands
    back ``{"PBR basics": {"_Smoothness": 0.26}}``, and taking that as it comes
    offers the other side a parameter called "PBR basics" whose value is a
    dictionary. Nothing raises: the names simply never match, and every value
    quietly fails to land.
    """
    flattened = {}
    for group, members in by_group.items():
        if not isinstance(members, dict):
            raise ShaderStateError(
                "this build groups shader values as {0!r} -> {1}, which is not the "
                "{{group: {{name: value}}}} this reads".format(group, type(members).__name__))
        flattened.update(members)
    return flattened


class Layout:
    """One reading of which instances exist, what they run, and who uses them.

    Every question this module asks about the current arrangement comes off the
    same two calls, taken together at one moment. Asking again per question was
    a second of the main thread each time on a real character, and two answers
    taken a moment apart can also disagree -- which is a bug that only appears
    while somebody is changing shaders in the interface.
    """

    __slots__ = ("object", "instance_by_texture_set", "shader_by_instance",
                 "label_by_instance")

    def __init__(self):
        self.object = assignment()
        found = instances()
        identifier_by_label = {entry["label"]: entry["id"] for entry in found}
        self.label_by_instance = {entry["id"]: entry["label"] for entry in found}
        shaders = self.object.get("shaders") or {}
        self.shader_by_instance = {}
        for entry in found:
            body = shaders.get(entry["label"]) or {}
            self.shader_by_instance[entry["id"]] = str(
                body.get("shader") or entry.get("shader") or "")

        identity_by_display = {texture_set.name: texture_set.original_name
                               for texture_set
                               in substance_painter.textureset.all_texture_sets()}
        self.instance_by_texture_set = {}
        for display, body in (self.object.get("texturesets") or {}).items():
            label = body.get("shader")
            identity = identity_by_display.get(display, display)
            if label in identifier_by_label:
                self.instance_by_texture_set[identity] = identifier_by_label[label]
            else:
                LOG.warning("Texture Set %r names shader instance %r, which is not in "
                            "the instance list", display, label)

    def exposed(self, identifier):
        return exposed_parameters(identifier, self.shader_by_instance.get(identifier, ""))


def instance_by_texture_set():
    """Texture Set identity -> shader instance id.

    Painter states this in two halves: the assignment names, per Texture Set, the
    shader instance *label* it uses, and the instance list carries the id every
    other call wants. Joining them here keeps that two-step in one place.

    Keyed by identity rather than by the displayed name, because the other side
    speaks identities -- a Texture Set renamed on either side has to stay the
    same Texture Set.
    """
    identifier_by_label = {entry["label"]: entry["id"] for entry in instances()}
    identity_by_display = {texture_set.name: texture_set.original_name
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
            # A whole number that arrived as a float is a whole number. The other
            # side keeps its material row in floats -- every value in it, integer
            # or not -- so refusing 1.0 for an Int uniform refuses the value
            # rather than a type error. A fractional one is still refused: that
            # really is somebody offering a number this uniform cannot hold.
            if isinstance(value, bool) or isinstance(value, int):
                return True, int(value)
            if isinstance(value, float) and value.is_integer():
                return True, int(value)
            return False, value
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


def _wear_what_was_asked_for(layout, values_by_texture_set, name_by_texture_set,
                            report):
    """Give every Texture Set whose material names a shader that shader.

    Done here rather than left to somebody: an offer written for one shader and
    landing on another does nothing, and "nothing happened" is the one outcome
    that cannot be told apart from a bridge that is not running. Sets already
    wearing it cost one layout write and no swap, so arriving twice is free.
    """
    wanted = {}
    for texture_set, name in (name_by_texture_set or {}).items():
        if name and values_by_texture_set.get(texture_set):
            wanted.setdefault(name, []).append(texture_set)
    if not wanted:
        return
    worn_any = False
    for name, texture_sets in sorted(wanted.items()):
        needed = []
        for texture_set in texture_sets:
            identifier = layout.instance_by_texture_set.get(texture_set)
            if identifier is None:
                continue
            if not set(values_by_texture_set[texture_set]) & set(layout.exposed(identifier)):
                needed.append(texture_set)
        if not needed:
            continue
        worn, why = wear_shader(layout, needed, name)
        if why:
            report["no_shader"][name] = why
        worn_any = worn_any or bool(worn)
        for texture_set in needed:
            if texture_set not in worn:
                report["wrong_shader"][texture_set] = name
    return worn_any


def apply_by_texture_set(values_by_texture_set, shader_url_by_texture_set=None,
                         vocabulary_by_texture_set=None,
                         shader_name_by_texture_set=None):
    """Set what the shader on each Texture Set actually exposes; report the rest.

    Several Texture Sets share one shader instance until somebody gives them
    different shaders, so an offer aimed at two of them lands on the same
    uniforms. Where those two disagree on a value, neither is written: taking one
    silently would make the viewport show a number nobody asked for.

    An offer that says which shader it was written for, and lands on an instance
    running something else, is reported as that -- one line naming the shader --
    rather than as its hundred and thirty six names being individually unknown.
    Both are true; only the first is a thing somebody can act on.
    """
    layout = Layout()
    swapped = False
    for texture_set, url in (shader_url_by_texture_set or {}).items():
        identifier = layout.instance_by_texture_set.get(texture_set)
        if identifier is None:
            LOG.warning("no shader instance for Texture Set %r; shader not swapped",
                        texture_set)
            continue
        update_shader(identifier, url)
        swapped = True

    spoken = vocabulary_by_texture_set or {}
    offers = {}
    report = {"applied": {}, "unknown": {}, "mismatched": {}, "conflicting": {},
              "wrong_shader": {}, "no_shader": {}, "unmapped": []}
    if _wear_what_was_asked_for(layout, values_by_texture_set,
                                shader_name_by_texture_set, report) or swapped:
        layout = Layout()
    identifier_by_set = layout.instance_by_texture_set
    report["unmapped"] = sorted(set(values_by_texture_set) - set(identifier_by_set))
    for texture_set, values in values_by_texture_set.items():
        identifier = identifier_by_set.get(texture_set)
        if identifier is None:
            continue
        exposed = layout.exposed(identifier)
        wanted = spoken.get(texture_set)
        if wanted and values and not set(values) & set(exposed):
            report["wrong_shader"].setdefault(texture_set, wanted)
            continue
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
