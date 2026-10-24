# -*- coding: utf-8 -*-
"""The surface a project paints on: ``paint/scene.assbin``, read as a node tree, meshes and material names.

Painter writes its scene in the shape of Assimp's binary dump with chunks framed its own way. After the
512-byte header come the eight bytes saying how long the rest is and a count this reader has no use for;
then three runs, the node tree, the meshes and the materials, the last two each behind a count of its
chunks. A chunk begins with its length, counted from that eight-byte field and reaching to the next chunk's
eight-byte field, so the next one begins where this one ends:

* a node: its name, its 4x4 transform, how many children, meshes and metadata entries it has, the indices of
  its meshes, the count of its children and the children themselves as chunks, then its metadata, of which the
  named matrices are read (``import_transform`` is the one that matters) and the rest is skipped;
* a mesh: its name, its primitive type, its material index and the flags telling which arrays follow
  (``1`` positions, ``2`` normals, ``4`` tangents and bitangents, ``0x100 << n`` the n-th UV set), each array
  behind its element count (a UV set behind its component count as well, three floats a vertex whatever it
  is), then the faces, each a type, an index count and the indices;
* a material: its properties, each a chunk of a key, a semantic, an index, a type and its data; only the name
  (``?mat.name``) is read.

Arrays come back as ``array.array`` in the file's own layout, so a caller with numpy wraps them without a copy,
and each mesh says where in the file its arrays lie (``where``) so an edit that keeps the topology is
written back in place (``overwrite``).
"""

from __future__ import annotations

import struct
from array import array
from collections import namedtuple

MAGIC = b"ASSIMP.binary-dump."
HEADER = 512
_LENGTH_AND_COUNT = 12
_HAS_POSITIONS = 0x1
_HAS_NORMALS = 0x2
_HAS_TANGENTS = 0x4
_FIRST_UV = 0x100
_UV_SETS = 8
_FIRST_COLOR = 0x10000
_COLOR_SETS = 8
_TRIANGLES = 4
_NAME_KEY = "?mat.name"
_STRING = 3

IMPORT_TRANSFORM = "ALG_PTV_TOTAL_TRANSFORM"
_MATRIX_TYPE = 6

Node = namedtuple("Node", "name transform meshes children metadata")
Mesh = namedtuple("Mesh", "name material positions normals tangents bitangents uvs indices where")
Scene = namedtuple("Scene", "source root meshes materials")


class SceneError(ValueError):
    """A scene file this reader does not follow."""


class _Cursor:
    def __init__(self, data, position):
        self.data = data
        self.position = position

    def take(self, layout):
        values = struct.unpack_from(layout, self.data, self.position)
        self.position += struct.calcsize(layout)
        return values[0] if len(values) == 1 else values

    def text(self):
        length = self.take("<I")
        value = bytes(self.data[self.position:self.position + length]).decode("utf-8")
        self.position += length
        return value

    def floats(self, count):
        found = array("f")
        found.frombytes(self.data[self.position:self.position + 4 * count])
        if len(found) != count:
            raise SceneError("{0} floats run past the end of the scene".format(count))
        self.position += 4 * count
        return found

    def floats_at(self, count, where, key):
        where[key] = self.position
        return self.floats(count)


def _metadata(cursor):
    """The matrices a node carries by name; what does not follow the one layout seen is left unread."""
    try:
        cursor.take("<H")
        count = cursor.take("<I")
        keys = [cursor.text() for _index in range(count)]
        if cursor.take("<I") != count:
            return {}
        found = {}
        for key in keys:
            if cursor.take("<I") != _MATRIX_TYPE:
                return found
            found[key] = cursor.take("<16f")
        return found
    except (struct.error, UnicodeDecodeError):
        return {}


def _node(cursor):
    start = cursor.position
    length = cursor.take("<Q")
    name = cursor.text()
    transform = cursor.take("<16f")
    children, meshes, metadata = cursor.take("<3I")
    indices = [cursor.take("<I") for _index in range(meshes)]
    if cursor.take("<I") != children:
        raise SceneError("{0} counts its children twice, differently".format(name))
    found = [_node(cursor) for _index in range(children)]
    entries = _metadata(cursor) if metadata else {}
    cursor.position = start + length
    return Node(name, transform, indices, found, entries)


def _mesh(cursor):
    start = cursor.position
    length = cursor.take("<Q")
    name = cursor.text()
    primitives, material, flags = cursor.take("<3I")
    if primitives != _TRIANGLES:
        raise SceneError("{0} holds primitives of type {1}, not triangles".format(name, primitives))
    arrays = {}
    where = {}
    for kind, present in (("positions", _HAS_POSITIONS), ("normals", _HAS_NORMALS), ("tangents", _HAS_TANGENTS),
                          ("bitangents", _HAS_TANGENTS)):
        if flags & present:
            arrays[kind] = cursor.floats_at(3 * cursor.take("<I"), where, kind)
    uvs = []
    for number in range(_UV_SETS):
        if flags & (_FIRST_UV << number):
            components = cursor.take("<I")
            uvs.append((components, cursor.floats_at(3 * cursor.take("<I"), where, "uv{0}".format(number))))
    if any(flags & (_FIRST_COLOR << number) for number in range(_COLOR_SETS)):
        raise SceneError("{0} has vertex colours, which this reader does not place".format(name))
    indices = array("I")
    for _face in range(cursor.take("<I")):
        _kind, corners = cursor.take("<2I")
        if corners != 3:
            raise SceneError("{0} has a face of {1} corners".format(name, corners))
        indices.extend(cursor.take("<3I"))
    cursor.position = start + length
    return Mesh(name, material, arrays.get("positions"), arrays.get("normals"), arrays.get("tangents"),
                arrays.get("bitangents"), uvs, indices, where)


def _material_name(cursor):
    start = cursor.position
    length = cursor.take("<Q")
    found = None
    for _property in range(cursor.take("<I")):
        property_start = cursor.position
        property_length = cursor.take("<Q")
        key = cursor.text()
        _semantic, _index, kind, _size = cursor.take("<4I")
        if key == _NAME_KEY and kind == _STRING:
            found = cursor.text()
        cursor.position = property_start + property_length
    cursor.position = start + length
    if found is None:
        raise SceneError("a material without a name")
    return found


def import_transform(found):
    """``(scale, offset)`` of the transform Painter applied to the geometry it was given, to make the scene:
    scene position = scale * given position + offset. ``None`` where no node carries it."""
    pending = [found.root]
    while pending:
        node = pending.pop()
        matrix = node.metadata.get(IMPORT_TRANSFORM)
        if matrix is not None:
            if matrix[0] != matrix[5] or matrix[0] != matrix[10]:
                raise SceneError("the import transform {0} is not a uniform scale".format(matrix))
            return matrix[0], (matrix[3], matrix[7], matrix[11])
        pending.extend(node.children)
    return None


def overwrite(data, mesh, kind, values):
    """Write ``values`` over the ``kind`` array (``positions``, ``uv0``, ...) of ``mesh`` in ``data``, a bytearray.

    The mesh must come from a ``read`` of these bytes and ``values`` must be as long as the array it replaces:
    an edit that keeps the mesh's topology is written in place.
    """
    arrays = {"positions": mesh.positions}
    for number, (_components, uvs) in enumerate(mesh.uvs):
        arrays["uv{0}".format(number)] = uvs
    if len(values) != len(arrays[kind]):
        raise SceneError("{0} floats cannot replace the {1} of {2}".format(len(values), len(arrays[kind]), kind))
    start = mesh.where[kind]
    data[start:start + 4 * len(values)] = values.tobytes()


def read(data):
    """The ``Scene`` held in the bytes of a ``scene.assbin``."""
    if bytes(data[:len(MAGIC)]) != MAGIC:
        raise SceneError("not an Assimp binary dump")
    _major, _minor, _revision, _flags, shortened, compressed = struct.unpack_from("<4I2H", data, 44)
    if shortened or compressed:
        raise SceneError("a shortened or compressed dump")
    source = bytes(data[64:320]).split(b"\x00")[0].decode("utf-8")
    cursor = _Cursor(data, HEADER + _LENGTH_AND_COUNT)
    root = _node(cursor)
    meshes = [_mesh(cursor) for _index in range(cursor.take("<I"))]
    materials = [_material_name(cursor) for _index in range(cursor.take("<I"))]
    return Scene(source, root, meshes, materials)
