# -*- coding: utf-8 -*-
"""Painter's self-describing data stream as a tree that writes back to the same bytes.

The document, the viewer settings and the baking settings of a project are each one stream: a header (magic
number, stream version, schema version, the root value's type code, how many types the stream defines) and
the root value. An object is the offset from the stream's start where it ends, zero for none, then its type
-- ``0xFFFFFFFF`` and the type's definition (its name, then each field's name and type code) the first time
the type is used, the index of that definition after that -- then its fields in the definition's order.

``Definition`` is a type, ``Object`` a value of one, and an array of records whose fields all have a fixed
size stays the block of bytes it is (``Packed``): a document holds millions of stroke points and timeline
keys, and none of them is worth an object. Fields keep the bytes they were read as and decode on demand, so
a stream that was not changed writes back byte for byte; ``read`` and ``write`` are each other's inverse.
"""

from __future__ import annotations

import struct

MAGIC = 0x1B7C2FDD
NEW_TYPE = 0xFFFFFFFF
_HEADER = struct.Struct("<5I")
_NUMBER = struct.Struct("<I")

TEXT = 0x10
RECORDS = 0x11
OBJECT = 0x12
OBJECTS = 0x13
EMBEDDED = 0x14
#: A block of sixteen bytes, kept as it is.
BLOCK = 0x16

#: The fixed-size values: the code of each, and the ``struct`` format that decodes it.
FIXED = {
    0x01: "<f", 0x02: "<2f", 0x03: "<3f", 0x04: "<4f", 0x05: "<i", 0x06: "<2i", 0x07: "<3i", 0x08: "<4i",
    0x09: "<I", 0x0A: "<B", 0x0B: "<I", 0x0C: "<Q", 0x0D: "<9f", 0x0E: "<16f", 0x0F: "<Q", 0x15: "<Q", BLOCK: "<16s",
}
_FORMATS = {code: struct.Struct(layout) for code, layout in FIXED.items()}


class StreamError(ValueError):
    """A stream that does not follow the grammar."""


def decode(code, raw):
    """The value of a fixed-size field held as ``raw``: a number, a tuple of numbers, or the bytes of a block."""
    values = _FORMATS[code].unpack(raw)
    return values[0] if len(values) == 1 else values


def encode(code, value):
    """The bytes of a fixed-size field holding ``value``."""
    layout = _FORMATS[code]
    return layout.pack(*value) if isinstance(value, (tuple, list)) else layout.pack(value)


class Definition:
    """A type of the stream: its name and its fields, each a name and a type code."""

    __slots__ = ("name", "fields", "index", "signature", "packed")

    def __init__(self, name, fields):
        self.name = name
        self.fields = tuple(fields)
        self.index = {field: position for position, (field, _code) in enumerate(self.fields)}
        self.signature = (name, self.fields)
        codes = [code for _field, code in self.fields]
        self.packed = struct.Struct("<" + "".join(_FORMATS[code].format[1:] for code in codes)) if all(
            code in FIXED for code in codes) and codes else None


class Object:
    """A value of a type: the raw bytes of its fixed-size fields and text, and its objects and arrays as they are."""

    __slots__ = ("definition", "values")

    def __init__(self, definition, values):
        self.definition = definition
        self.values = values

    @property
    def type(self):
        return self.definition.name

    def __getitem__(self, field):
        return self.values[self.definition.index[field]]

    def __setitem__(self, field, value):
        self.values[self.definition.index[field]] = value

    def __contains__(self, field):
        return field in self.definition.index

    def code(self, field):
        return self.definition.fields[self.definition.index[field]][1]

    def number(self, field):
        """The fixed-size field decoded."""
        return decode(self.code(field), self[field])

    def set_number(self, field, value):
        self[field] = encode(self.code(field), value)

    def text(self, field):
        return self[field].decode("utf-8")

    def set_text(self, field, value):
        self[field] = value.encode("utf-8")

    def items(self):
        """``(field, code, value)`` for each field in order."""
        return [(field, code, self.values[position]) for position, (field, code) in enumerate(self.definition.fields)]

    def copy(self):
        """A deep copy: objects and arrays are copied, the bytes they hold are shared."""
        return Object(self.definition, [_copy(value) for value in self.values])

    def __repr__(self):
        return "<{0} {1}>".format(self.definition.name, ", ".join(field for field, _code in self.definition.fields[:6]))


def _copy(value):
    if isinstance(value, Object):
        return value.copy()
    if isinstance(value, list):
        return [_copy(element) for element in value]
    if isinstance(value, Packed):
        return Packed(value.definition, value.count, value.block)
    return value


class Packed:
    """An array of records of a fixed size, kept as the bytes they are."""

    __slots__ = ("definition", "count", "block")

    def __init__(self, definition, count, block):
        self.definition = definition
        self.count = count
        self.block = block

    def rows(self):
        """Each record as a tuple of its fields' values, vectors flattened."""
        return list(self.definition.packed.iter_unpack(self.block))

    @classmethod
    def of(cls, definition, rows):
        """The array of ``rows`` -- tuples of the flattened values of each record -- of ``definition``."""
        layout = definition.packed
        return cls(definition, len(rows), b"".join(layout.pack(*row) for row in rows))


def walk(value):
    """Every object in ``value`` -- an object, or an array of them -- itself first, in the order they are written."""
    pending = [value]
    while pending:
        found = pending.pop()
        if isinstance(found, Object):
            yield found
            pending.extend(reversed([child for child in found.values if isinstance(child, (Object, list))]))
        elif isinstance(found, list):
            pending.extend(reversed([child for child in found if child is not None]))


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = _HEADER.size
        self.types = []

    def number(self):
        value = _NUMBER.unpack_from(self.data, self.position)[0]
        self.position += 4
        return value

    def text(self):
        length = self.number()
        value = bytes(self.data[self.position:self.position + length])
        if len(value) != length:
            raise StreamError("a text of {0} bytes runs past the end of the stream".format(length))
        self.position += length
        return value

    def definition(self):
        reference = self.number()
        if reference != NEW_TYPE:
            return self.types[reference]
        name = self.text().decode("utf-8")
        fields = []
        for _index in range(self.number()):
            field = self.text().decode("utf-8")
            fields.append((field, self.number()))
        found = Definition(name, fields)
        self.types.append(found)
        return found

    def object(self):
        end = self.number()
        if end == 0:
            return None
        found = self.definition()
        values = [self.value(code) for _field, code in found.fields]
        if self.position != end:
            raise StreamError("{0} ends at {1}, its end offset says {2}".format(found.name, self.position, end))
        return Object(found, values)

    def value(self, code):
        layout = _FORMATS.get(code)
        if layout is not None:
            raw = bytes(self.data[self.position:self.position + layout.size])
            if len(raw) != layout.size:
                raise StreamError("a value of type code {0:#x} runs past the end of the stream".format(code))
            self.position += layout.size
            return raw
        if code == TEXT:
            return self.text()
        if code in (OBJECT, EMBEDDED):
            return self.object()
        if code == OBJECTS:
            return [self.object() for _index in range(self.number())]
        if code == RECORDS:
            return self.records()
        raise StreamError("type code {0:#x} at {1} is not known".format(code, self.position))

    def records(self):
        count = self.number()
        if not count:
            return []
        found = self.definition()
        if found.packed is not None:
            size = found.packed.size * count
            block = bytes(self.data[self.position:self.position + size])
            if len(block) != size:
                raise StreamError("{0} records of {1} run past the end of the stream".format(count, found.name))
            self.position += size
            return Packed(found, count, block)
        return [Object(found, [self.value(code) for _field, code in found.fields]) for _index in range(count)]


class Stream:
    """A stream read: its header values and its root."""

    def __init__(self, version, schema, code, root):
        self.version = version
        self.schema = schema
        self.code = code
        self.root = root


def read(data):
    """The ``Stream`` held in ``data``. Zero bytes after the root -- the padding of the file's last block -- are allowed."""
    magic, version, schema, code, _types = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise StreamError("not a Painter data stream")
    reader = _Reader(data)
    root = reader.value(code)
    if any(reader.data[reader.position:]):
        raise StreamError("the stream holds data after its root")
    return Stream(version, schema, code, root)


class _Writer:
    def __init__(self):
        self.parts = bytearray(_HEADER.size)
        self.written = {}

    def number(self, value):
        self.parts += _NUMBER.pack(value)

    def text(self, value):
        self.number(len(value))
        self.parts += value

    def definition(self, found):
        index = self.written.get(found.signature)
        if index is not None:
            self.number(index)
            return
        self.written[found.signature] = len(self.written)
        self.number(NEW_TYPE)
        self.text(found.name.encode("utf-8"))
        self.number(len(found.fields))
        for field, code in found.fields:
            self.text(field.encode("utf-8"))
            self.number(code)

    def object(self, value):
        if value is None:
            self.number(0)
            return
        start = len(self.parts)
        self.number(0)
        self.definition(value.definition)
        for (_field, code), field in zip(value.definition.fields, value.values):
            self.value(code, field)
        _NUMBER.pack_into(self.parts, start, len(self.parts))

    def value(self, code, value):
        if code in _FORMATS:
            self.parts += value
        elif code == TEXT:
            self.text(value)
        elif code in (OBJECT, EMBEDDED):
            self.object(value)
        elif code == OBJECTS:
            self.number(len(value))
            for element in value:
                self.object(element)
        elif code == RECORDS:
            self.records(value)
        else:
            raise StreamError("type code {0:#x} cannot be written".format(code))

    def records(self, value):
        if isinstance(value, Packed):
            self.number(value.count)
            if value.count:
                self.definition(value.definition)
                self.parts += value.block
            return
        self.number(len(value))
        if value:
            self.definition(value[0].definition)
            for record in value:
                for (_field, code), field in zip(record.definition.fields, record.values):
                    self.value(code, field)


def write(stream):
    """The bytes of a ``Stream``."""
    writer = _Writer()
    writer.value(stream.code, stream.root)
    _HEADER.pack_into(writer.parts, 0, MAGIC, stream.version, stream.schema, stream.code, len(writer.written))
    return bytes(writer.parts)
