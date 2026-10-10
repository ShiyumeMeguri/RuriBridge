# -*- coding: utf-8 -*-
"""What a Painter project holds of its strokes and polygon fills, read from the project file.

Painter's API shows the layer stack but no stroke in it, and how a stroke comes back when the
surface's UVs change depends on where it was made. A stroke keeps the screen path it was drawn
along, the camera it was drawn through and the view it was drawn in, and is cast again onto
the surface Painter holds now: one made in the 3D view lands on the same points of the
surface, one made in the 2D view on the same UV coordinates -- on whatever the new layout
puts there, or nowhere. A stroke made in 3D still takes its stamp from the UVs when its brush
is aligned to them, sized in texture space or lays a material; a polygon fill clicked in the
2D view picks polygons by UV position, and one filling UV chunks picks them by the UV islands.
Content holding any of these is laid out in UV space, like a picture (``bound``). Every polygon
fill, wherever it was clicked, is kept as the triangles of its Texture Set it picked, so none of
it follows a face into another Texture Set (``bound`` with ``triangles``); and paint of any kind
-- strokes, paths, polygon fills -- shows only in the Texture Set its layer belongs to
(``painted``).

The project file is HDF5: a small file system whose ``paint/document.bin`` is the document --
every Texture Set, stack, layer and action -- in Painter's own self-describing stream. A
header (magic number, stream version, schema version, the root value's type code, how many
types the stream defines), then the root value. An object is the offset from the stream's start
where it ends, zero for none, then its type: ``0xFFFFFFFF`` and the type's definition -- its
name, then each field's name and type code -- the first time the type is used, the index of the
definition after that; then its fields in that order. An array of records names its type once,
before the first of them. Every type is defined where it is first used, so the stream is read
whole. The HDF5 side reads what reaching the document needs: superblock version 0,
symbol-table groups, version 1 object headers, compact, contiguous and chunked (deflated)
layouts.
"""

from __future__ import annotations

import mmap
import struct
import zlib

_SIGNATURE = b"\x89HDF\r\n\x1a\n"
#: Where the document lives in a project file.
DOCUMENT = ("paint", "document.bin")

_STREAM_MAGIC = 0x1B7C2FDD
_NEW_TYPE = 0xFFFFFFFF
_FIXED = {0x01: "<f", 0x02: "<2f", 0x03: "<3f", 0x04: "<4f", 0x05: "<i", 0x06: "<2i", 0x07: "<3i",
          0x08: "<4i", 0x09: "<I", 0x0A: "<B", 0x0B: "<I", 0x0C: "<Q", 0x0D: "<9f", 0x0E: "<16f", 0x0F: "<Q",
          0x15: "<Q"}
_TEXT = 0x10
_RECORDS = 0x11
_OBJECT = 0x12
_OBJECTS = 0x13
_EMBEDDED = 0x14

#: Painter's enumerations as its stream stores them: ``DataViewType_2D``,
#: ``DataBrushAlignment_UV``, ``DataBrushSizeSpace_Texture``, ``DataDecalGranularity_UVChunk``.
VIEW_2D = 0
ALIGNMENT_UV = 2
SIZE_IN_TEXTURE = 2
GRANULARITY_UV_CHUNK = 3
#: The paint action laying a stencil cast from the camera, whatever its colour comes from.
_STENCIL_PAINT = "DataActionPaintProj"
#: The brush that lays stamps along a stroke, as opposed to a ribbon laid along a 3D path.
_STAMP_BRUSH = "DataBrushStamp"
_UNIFORM = "DataSourceUniform"
_GROUP_LAYER_CHILDREN = "subStack"


class DocumentError(ValueError):
    """A project file the reader cannot follow."""


class _Hdf5:
    def __init__(self, data):
        if (data[:8] != _SIGNATURE or data[8] != 0 or data[13] != 8 or data[14] != 8
                or struct.unpack_from("<I", data, 72)[0] != 1):
            raise DocumentError("not a version 0 HDF5 file with 8-byte offsets and a cached root group")
        self.data = data
        self.root = struct.unpack_from("<QQ", data, 80)

    def _name(self, heap, offset):
        if self.data[heap:heap + 4] != b"HEAP":
            raise DocumentError("no local heap at {0}".format(heap))
        start = struct.unpack_from("<Q", self.data, heap + 24)[0] + offset
        return bytes(self.data[start:self.data.find(b"\x00", start)]).decode("utf-8")

    def _children(self, group):
        tree, heap = group
        found = {}
        self._walk(tree, heap, found)
        return found

    def _walk(self, node, heap, found):
        data = self.data
        if data[node:node + 4] != b"TREE" or data[node + 4] != 0:
            raise DocumentError("no group tree node at {0}".format(node))
        level = data[node + 5]
        for index in range(struct.unpack_from("<H", data, node + 6)[0]):
            child = struct.unpack_from("<Q", data, node + 24 + index * 16 + 8)[0]
            if level:
                self._walk(child, heap, found)
                continue
            if data[child:child + 4] != b"SNOD":
                raise DocumentError("no symbol node at {0}".format(child))
            for symbol in range(struct.unpack_from("<H", data, child + 6)[0]):
                entry = child + 8 + symbol * 40
                name_offset, header, cache = struct.unpack_from("<QQI", data, entry)
                found[self._name(heap, name_offset)] = (
                    header, struct.unpack_from("<QQ", data, entry + 24) if cache == 1 else None)

    def _messages(self, header):
        data = self.data
        if data[header] != 1:
            raise DocumentError("object header version {0} at {1}".format(data[header], header))
        count = struct.unpack_from("<H", data, header + 2)[0]
        blocks = [(header + 16, struct.unpack_from("<I", data, header + 8)[0])]
        found = []
        while blocks and len(found) < count:
            start, length = blocks.pop(0)
            position = start
            while position + 8 <= start + length and len(found) < count:
                kind, size = struct.unpack_from("<HH", data, position)
                if kind == 0x10:
                    blocks.append(struct.unpack_from("<QQ", data, position + 8))
                found.append((kind, position + 8))
                position += 8 + size
        return found

    def _group(self, path):
        current = self.root
        for part in path:
            header, cached = self._children(current)[part]
            current = cached or next((struct.unpack_from("<QQ", self.data, body)
                                      for kind, body in self._messages(header) if kind == 0x11), None)
            if current is None:
                raise DocumentError("{0} is not a group".format(part))
        return current

    def read(self, path):
        header, _cached = self._children(self._group(path[:-1]))[path[-1]]
        messages = dict(self._messages(header))
        if 0x08 not in messages:
            raise DocumentError("{0} has no data layout".format("/".join(path)))
        layout = messages[0x08]
        data = self.data
        if data[layout] != 3:
            raise DocumentError("data layout version {0}".format(data[layout]))
        if data[layout + 1] == 0:
            size = struct.unpack_from("<H", data, layout + 2)[0]
            return bytes(data[layout + 4:layout + 4 + size])
        if data[layout + 1] == 1:
            address, size = struct.unpack_from("<QQ", data, layout + 2)
            return bytes(data[address:address + size])
        if data[layout + 1] != 2:
            raise DocumentError("data layout class {0}".format(data[layout + 1]))
        filters = self._filters(messages[0x0B]) if 0x0B in messages else []
        chunks = []
        self._chunks(struct.unpack_from("<Q", data, layout + 3)[0], data[layout + 2], chunks)
        pieces = []
        for _offset, address, stored, mask in sorted(chunks):
            piece = bytes(data[address:address + stored])
            for identifier in reversed(filters):
                if mask or identifier != 1:
                    raise DocumentError("a chunk filtered by {0} (mask {1})".format(identifier, mask))
                piece = zlib.decompress(piece)
            pieces.append(piece)
        return b"".join(pieces)

    def _filters(self, body):
        data = self.data
        if data[body] != 1:
            raise DocumentError("filter pipeline version {0}".format(data[body]))
        position = body + 8
        found = []
        for _index in range(data[body + 1]):
            identifier, name_length, _flags, values = struct.unpack_from("<HHHH", data, position)
            position += 8 + (name_length + 7) // 8 * 8 + (values + values % 2) * 4
            found.append(identifier)
        return found

    def _chunks(self, node, dimensions, found):
        data = self.data
        if data[node:node + 4] != b"TREE" or data[node + 4] != 1:
            raise DocumentError("no chunk tree node at {0}".format(node))
        level = data[node + 5]
        key = 8 + 8 * dimensions
        position = node + 24
        for _index in range(struct.unpack_from("<H", data, node + 6)[0]):
            stored, mask = struct.unpack_from("<II", data, position)
            offset = struct.unpack_from("<Q", data, position + 8)[0]
            child = struct.unpack_from("<Q", data, position + key)[0]
            position += key + 8
            if level:
                self._chunks(child, dimensions, found)
            else:
                found.append((offset, child, stored, mask))


class _Stream:
    def __init__(self, data):
        magic, _version, _schema, root, _types = struct.unpack_from("<5I", data, 0)
        if magic != _STREAM_MAGIC:
            raise DocumentError("the document does not start as Painter's data stream")
        self.data = data
        self.position = 20
        self.types = []
        self.root = self._value(root)

    def _number(self):
        value = struct.unpack_from("<I", self.data, self.position)[0]
        self.position += 4
        return value

    def _text(self):
        length = self._number()
        value = self.data[self.position:self.position + length].decode("utf-8", "replace")
        self.position += length
        return value

    def _type(self):
        reference = self._number()
        if reference != _NEW_TYPE:
            return self.types[reference]
        name = self._text()
        fields = []
        for _index in range(self._number()):
            field = self._text()
            fields.append((field, self._number()))
        self.types.append((name, fields))
        return self.types[-1]

    def _fields(self, kind, end):
        name, fields = kind
        record = {"$type": name}
        for field, code in fields:
            record[field] = self._value(code, name, field)
        if end is not None and self.position != end:
            raise DocumentError("{0} ends at {1}, its own end says {2}".format(name, self.position, end))
        return record

    def _value(self, code, owner="the root", field=""):
        if code in _FIXED:
            layout = _FIXED[code]
            values = struct.unpack_from(layout, self.data, self.position)
            self.position += struct.calcsize(layout)
            return values[0] if len(values) == 1 else list(values)
        if code == _TEXT:
            return self._text()
        if code in (_OBJECT, _EMBEDDED):
            end = self._number()
            return None if end == 0 else self._fields(self._type(), end)
        if code == _OBJECTS:
            return [self._value(_OBJECT, owner, field) for _index in range(self._number())]
        if code == _RECORDS:
            count = self._number()
            if not count:
                return []
            kind = self._type()
            return [self._fields(kind, None) for _index in range(count)]
        raise DocumentError("{0}.{1} holds a value of type code {2:#x}, which the reader does not know".format(
            owner, field, code))


def read(path):
    """The document of the project file at ``path``, as nested dictionaries, each naming its
    type under ``$type``."""
    with open(path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        stream = _Hdf5(data).read(DOCUMENT)
    try:
        return _Stream(stream).root
    except struct.error as error:
        raise DocumentError("the document ends before its values do: {0}".format(error)) from error


class Bound:
    """Content laid out in UV space: the action group holding it (``uid``) -- an effect's own
    uid, or for a stack's own strokes the group its layer keeps them in -- the layer
    (``layer``), whether the layer's mask holds it (``mask``), whether it is the stack's own
    content rather than an effect in it (``own``), and why it is laid out in UV space."""

    __slots__ = ("uid", "layer", "mask", "own", "reasons")

    def __init__(self, uid, layer, mask, own, reasons):
        self.uid = uid
        self.layer = layer
        self.mask = mask
        self.own = own
        self.reasons = reasons


def _reasons(action, triangles):
    found = set()
    strokes = [stroke for stroke in action.get("strokes") or [] if stroke]
    paths = [stroke for stroke in action.get("strokes3D") or [] if stroke]
    if any(stroke.get("viewType") == VIEW_2D for stroke in strokes):
        found.add("strokes made in the 2D view")
    if strokes or paths:
        brush = action.get("brush") or {}
        if brush.get("alignment") == ALIGNMENT_UV:
            found.add("a brush aligned to the UVs")
        if brush.get("brushSizeSpace") == SIZE_IN_TEXTURE:
            found.add("a brush sized in texture space")
        if (action.get("$type") != _STENCIL_PAINT and brush.get("$type") == _STAMP_BRUSH
                and any(source and source.get("$type") != _UNIFORM for source in action.get("sourceColor") or [])):
            found.add("a brush laying a material")
    hits = action.get("hits") or []
    for hit in hits:
        if hit.get("viewType") == VIEW_2D:
            found.add("polygon fills made in the 2D view")
        if hit.get("granularity") == GRANULARITY_UV_CHUNK:
            found.add("polygon fills of whole UV chunks")
    if triangles and hits:
        found.add("polygon fills, kept as the Texture Set's triangles")
    for item in (action.get("subStack") or {}).get("items") or []:
        if item:
            found |= _reasons(item, triangles)
    return found


def _holds_paint(action):
    if any(stroke for stroke in action.get("strokes") or []) or any(path for path in action.get("strokes3D") or []):
        return True
    if action.get("hits"):
        return True
    return any(_holds_paint(item) for item in (action.get("subStack") or {}).get("items") or [] if item)


def _layers(items):
    for layer in items:
        if not layer:
            continue
        yield layer
        yield from _layers((layer.get(_GROUP_LAYER_CHILDREN) or {}).get("items") or [])


def bound(document, texture_set, triangles=False):
    """Everything of one Texture Set laid out in UV space, ``[Bound]``: of every layer, the
    content and the mask -- the first action of each is the stack's own, every later one an
    effect -- each action holding strokes or polygon fills that take where they land from the
    UVs; with ``triangles``, every action holding polygon fills as well."""
    found = []
    for material in document.get("materials") or []:
        if not material or material.get("sceneMaterialName") != texture_set:
            continue
        for stack in material.get("stacks") or []:
            for layer in _layers(((stack or {}).get("stack") or {}).get("items") or []):
                for mask, holder in ((False, layer.get("actions")), (True, layer.get("maskActions"))):
                    for index, action in enumerate((holder or {}).get("items") or []):
                        reasons = _reasons(action, triangles) if action else set()
                        if reasons:
                            found.append(Bound(int(action["uid"]), int(layer["uid"]), mask, index == 0,
                                               sorted(reasons)))
    return found


def painted(document):
    """The uids of every layer of the project whose content or mask holds paint of any kind:
    strokes, paths or polygon fills."""
    found = set()
    for material in document.get("materials") or []:
        for stack in (material or {}).get("stacks") or []:
            for layer in _layers(((stack or {}).get("stack") or {}).get("items") or []):
                actions = ((layer.get("actions") or {}).get("items") or []) + (
                    (layer.get("maskActions") or {}).get("items") or [])
                if any(action and _holds_paint(action) for action in actions):
                    found.add(int(layer["uid"]))
    return found
