# -*- coding: utf-8 -*-
"""The surface as an ASCII FBX: polygons as they are, every UV set, materials by name.

FBX is the format the texturing tool reads more than one UV set from and still
takes polygons rather than triangles -- it triangulates them itself, and what a
polygon fill or a stroke is recorded against is its own triangulation. Its
materials are names and nothing else here, so no importer turns a material
description into a layer nobody painted.

Every number is written as the exact value of a 32-bit float. The tool stores
32-bit floats, and a decimal that is not one of them is rounded by whichever
parser reads it: two of that tool's own importers round the same text to
different neighbours, which moves a vertex by one unit in the last place and
flips which of two overlapping triangles owns a texel. A value that is already a
32-bit float reads back as itself through either.

The tool's FBX reader keeps a short line buffer, so arrays are written sixteen
values to a line; one long line comes back as a scene with no meshes.

``read`` is ``write``'s inverse for the files it makes, and for no others: the surface a modelling
application sent is read back as the parts it was written from, so that other parts can be put beside them
in one file (the canvases of a project, see ``retarget.canvas_geometry``).
"""

from __future__ import annotations

import re
from collections import namedtuple

import numpy

#: One object's polygons: ``positions`` (vertices, 3), the vertex of every corner (``corner_vertex``), how many corners
#: each polygon has, the normal and the coordinates of every corner in each UV set (``corner_normal``, ``uv_sets``),
#: and the Texture Set each polygon paints into, an index into ``texture_sets`` (``polygon_texture_set``).
Part = namedtuple("Part", "name positions corner_vertex polygon_totals corner_normal uv_sets polygon_texture_set texture_sets")

_VALUES_PER_LINE = 16
_GEOMETRY_BASE = 1000000
_MODEL_BASE = 2000000
_MATERIAL_BASE = 3000000

_HEADER = (
    "; FBX 7.4.0 project file",
    "FBXHeaderExtension:  {",
    "\tFBXHeaderVersion: 1003",
    "\tFBXVersion: 7400",
    '\tCreator: "RuriBridge"',
    "}",
    "GlobalSettings:  {",
    "\tVersion: 1000",
    "\tProperties70:  {",
    '\t\tP: "UpAxis", "int", "Integer", "",1',
    '\t\tP: "UpAxisSign", "int", "Integer", "",1',
    '\t\tP: "FrontAxis", "int", "Integer", "",2',
    '\t\tP: "FrontAxisSign", "int", "Integer", "",1',
    '\t\tP: "CoordAxis", "int", "Integer", "",0',
    '\t\tP: "CoordAxisSign", "int", "Integer", "",1',
    '\t\tP: "OriginalUpAxis", "int", "Integer", "",1',
    '\t\tP: "OriginalUpAxisSign", "int", "Integer", "",1',
    '\t\tP: "UnitScaleFactor", "double", "Number", "",1',
    '\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1',
    "\t}",
    "}",
)


def _exact(values):
    """Values as the exact 32-bit floats the reader will hold, in a form it reads back
    as those very floats."""
    return numpy.asarray(values, dtype=numpy.float32).astype(numpy.float64).reshape(-1).tolist()


def _array(lines, indent, name, values, as_text):
    lines.append("{0}{1}: *{2} {{".format(indent, name, len(values)))
    rows = [",".join(as_text(value) for value in values[start:start + _VALUES_PER_LINE])
            for start in range(0, len(values), _VALUES_PER_LINE)]
    lines.append("{0}\ta: {1}".format(indent, (",\n" + indent + "\t").join(rows)))
    lines.append("{0}}}".format(indent))


def _layer_element(lines, kind, index, name, mapping, reference):
    lines.extend(("\t\t{0}: {1} {{".format(kind, index),
                  "\t\t\tVersion: {0}".format(102 if kind == "LayerElementNormal" else 101),
                  '\t\t\tName: "{0}"'.format(name),
                  '\t\t\tMappingInformationType: "{0}"'.format(mapping),
                  '\t\t\tReferenceInformationType: "{0}"'.format(reference)))


def _layers(lines, uv_set_count):
    for index in range(uv_set_count):
        lines.extend(("\t\tLayer: {0} {{".format(index), "\t\t\tVersion: 100"))
        kinds = (("LayerElementNormal", "LayerElementMaterial") if index == 0 else ())
        for kind in kinds + ("LayerElementUV",):
            lines.extend(("\t\t\tLayerElement:  {", '\t\t\t\tType: "{0}"'.format(kind),
                          "\t\t\t\tTypedIndex: {0}".format(index if kind == "LayerElementUV" else 0),
                          "\t\t\t}"))
        lines.append("\t\t}")


def _geometry(lines, identifier, part, uv_set_names):
    lines.append('\tGeometry: {0}, "Geometry::{1}", "Mesh" {{'.format(identifier, part.name))
    _array(lines, "\t\t", "Vertices", _exact(part.positions), repr)
    polygon_vertex = part.corner_vertex.astype(numpy.int64).copy()
    ends = numpy.cumsum(part.polygon_totals) - 1
    polygon_vertex[ends] = -polygon_vertex[ends] - 1
    _array(lines, "\t\t", "PolygonVertexIndex", polygon_vertex.tolist(), str)
    lines.append("\t\tGeometryVersion: 124")
    _layer_element(lines, "LayerElementNormal", 0, "", "ByPolygonVertex", "Direct")
    _array(lines, "\t\t\t", "Normals", _exact(part.corner_normal), repr)
    lines.append("\t\t}")
    for index, (name, coordinates) in enumerate(zip(uv_set_names, part.uv_sets)):
        unique, corner_uv = numpy.unique(numpy.asarray(coordinates, dtype=numpy.float32), axis=0,
                                         return_inverse=True)
        _layer_element(lines, "LayerElementUV", index, name, "ByPolygonVertex", "IndexToDirect")
        _array(lines, "\t\t\t", "UV", _exact(unique), repr)
        _array(lines, "\t\t\t", "UVIndex", corner_uv.reshape(-1).tolist(), str)
        lines.append("\t\t}")
    _layer_element(lines, "LayerElementMaterial", 0, "", "ByPolygon", "IndexToDirect")
    _array(lines, "\t\t\t", "Materials", part.polygon_texture_set.tolist(), str)
    lines.append("\t\t}")
    _layers(lines, len(uv_set_names))
    lines.append("\t}")


class SurfaceFileError(ValueError):
    """A file that ``write`` did not make."""


def write(path, parts, uv_set_names):
    """Write every part into one FBX. Each part holds one object's polygons, painting
    into its ``texture_sets`` by ``polygon_texture_set``; ``uv_set_names`` labels the
    UV sets every part carries, in order."""
    material_names = sorted({name for part in parts for name in part.texture_sets})
    quoted = sorted(name for name in material_names + [part.name for part in parts] if '"' in name)
    if quoted:
        raise ValueError("an FBX name cannot hold a double quote: {0}".format(", ".join(quoted)))
    material_ids = {name: _MATERIAL_BASE + index for index, name in enumerate(material_names)}
    lines = list(_HEADER)
    lines.extend(("Definitions:  {", "\tVersion: 100",
                  "\tCount: {0}".format(1 + 2 * len(parts) + len(material_names))))
    for kind, count in (("GlobalSettings", 1), ("Model", len(parts)), ("Geometry", len(parts)),
                        ("Material", len(material_names))):
        lines.extend(('\tObjectType: "{0}" {{'.format(kind), "\t\tCount: {0}".format(count), "\t}"))
    lines.extend(("}", "Objects:  {"))
    connections = []
    for index, part in enumerate(parts):
        geometry = _GEOMETRY_BASE + index
        model = _MODEL_BASE + index
        _geometry(lines, geometry, part, uv_set_names)
        lines.extend(('\tModel: {0}, "Model::{1}", "Mesh" {{'.format(model, part.name),
                      "\t\tVersion: 232", "\t\tProperties70:  {", "\t\t}", "\t\tShading: T",
                      '\t\tCulling: "CullingOff"', "\t}"))
        connections.append('\tC: "OO",{0},0'.format(model))
        connections.append('\tC: "OO",{0},{1}'.format(geometry, model))
        connections.extend('\tC: "OO",{0},{1}'.format(material_ids[name], model)
                           for name in part.texture_sets)
    for name in material_names:
        lines.extend(('\tMaterial: {0}, "Material::{1}", "" {{'.format(material_ids[name], name),
                      "\t\tVersion: 102", '\t\tShadingModel: "lambert"', "\t\tMultiLayer: 0",
                      "\t\tProperties70:  {", "\t\t}", "\t}"))
    lines.extend(("}", "Connections:  {"))
    lines.extend(connections)
    lines.append("}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


_GEOMETRY = re.compile(r'^Geometry: (\d+), "Geometry::(.*)", "Mesh" \{$')
_MODEL = re.compile(r'^Model: (\d+), "Model::(.*)", "Mesh" \{$')
_MATERIAL = re.compile(r'^Material: (\d+), "Material::(.*)", "" \{$')
_ELEMENT = re.compile(r"^(LayerElementNormal|LayerElementUV|LayerElementMaterial): (\d+) \{$")
_ARRAY = re.compile(r"^(\w+): \*(\d+) \{$")
_NAME = re.compile(r'^Name: "(.*)"$')
_CONNECTION = re.compile(r'^C: "OO",(\d+),(\d+)$')
_CREATOR = 'Creator: "RuriBridge"'


def _numbers(lines, start, count, dtype):
    """The array whose payload begins on ``lines[start]``, and the line after its closing brace."""
    end = start
    while lines[end].strip() != "}":
        end += 1
    payload = "".join(lines[start:end]).strip()
    if not payload.startswith("a:"):
        raise SurfaceFileError("an array of {0} values does not begin with its values".format(count))
    values = numpy.array(payload[2:].replace("\n", "").replace("\t", "").strip().split(","), dtype=dtype) if count else numpy.zeros(0, dtype=dtype)
    if len(values) != count:
        raise SurfaceFileError("an array says it holds {0} values and holds {1}".format(count, len(values)))
    return values, end + 1


def read(path):
    """The ``(parts, uv_set_names)`` an FBX that ``write`` made was written from."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    if not any(line.strip() == _CREATOR for line in lines[:8]):
        raise SurfaceFileError("{0} was not written by this module".format(path))
    geometries, models, materials, connections = [], {}, {}, []
    current = element = None
    position = 0
    while position < len(lines):
        line = lines[position].strip()
        position += 1
        found = _GEOMETRY.match(line)
        if found:
            current = {"id": int(found.group(1)), "name": found.group(2), "uv": {}}
            geometries.append(current)
            element = None
            continue
        found = _MODEL.match(line)
        if found:
            models[int(found.group(1))] = found.group(2)
            current = element = None
            continue
        found = _MATERIAL.match(line)
        if found:
            materials[int(found.group(1))] = found.group(2)
            current = element = None
            continue
        found = _CONNECTION.match(line)
        if found:
            connections.append((int(found.group(1)), int(found.group(2))))
            continue
        if current is None:
            continue
        found = _ELEMENT.match(line)
        if found:
            element = (found.group(1), int(found.group(2)))
            continue
        found = _NAME.match(line)
        if found and element is not None and element[0] == "LayerElementUV":
            current["uv"].setdefault(element[1], {})["name"] = found.group(1)
            continue
        found = _ARRAY.match(line)
        if found:
            name, count = found.group(1), int(found.group(2))
            integers = name in ("PolygonVertexIndex", "UVIndex", "Materials")
            values, position = _numbers(lines, position, count, numpy.int64 if integers else numpy.float64)
            if name == "UV":
                current["uv"].setdefault(element[1], {})["unique"] = values.reshape(-1, 2)
            elif name == "UVIndex":
                current["uv"].setdefault(element[1], {})["index"] = values
            else:
                current[name] = values
    geometry_ids = {geometry["id"] for geometry in geometries}
    parent = {child: owner for child, owner in connections if child in geometry_ids}
    parts, names = [], None
    for geometry in geometries:
        indices = geometry["PolygonVertexIndex"]
        ends = numpy.flatnonzero(indices < 0)
        corner_vertex = numpy.where(indices < 0, -indices - 1, indices).astype(numpy.int64)
        starts = numpy.concatenate(([0], ends[:-1] + 1)) if len(ends) else numpy.zeros(0, dtype=numpy.int64)
        owner = parent[geometry["id"]]
        texture_sets = [materials[child] for child, holder in connections if holder == owner and child in materials]
        uv_sets = [geometry["uv"][index]["unique"][geometry["uv"][index]["index"]].astype(numpy.float32) for index in sorted(geometry["uv"])]
        labels = [geometry["uv"][index]["name"] for index in sorted(geometry["uv"])]
        if names is not None and labels != names:
            raise SurfaceFileError("{0} names its UV sets {1}, not {2}".format(geometry["name"], labels, names))
        names = labels
        parts.append(Part(models[owner], geometry["Vertices"].reshape(-1, 3).astype(numpy.float32), corner_vertex, (ends - starts + 1).astype(numpy.int64),
                          geometry["Normals"].reshape(-1, 3).astype(numpy.float32), uv_sets, geometry["Materials"].astype(numpy.int64), texture_sets))
    return parts, names or []
