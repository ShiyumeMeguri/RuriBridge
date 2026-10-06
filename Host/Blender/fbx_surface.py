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
"""

from __future__ import annotations

import numpy

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
