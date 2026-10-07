# -*- coding: utf-8 -*-
"""What a Substance package declares, read from the package itself.

A ``.sbsar`` is a 7z archive holding, per assembly, the cooked graph and a description of
it (``.xml``): every graph's inputs, image inputs among them. Painter shows only some of a
graph's image inputs; the others it binds itself, from the surface's layout in UV set 0, and
nothing else can be bound to them. The description is the one statement of all of them.

Only what the packages Painter loads use is read: streams stored as they are or coded with
LZMA, one folder per file, the header itself coded or stored the same ways.
"""

from __future__ import annotations

import lzma
import re
import struct

_SIGNATURE = b"7z\xbc\xaf\x27\x1c"
_LZMA = b"\x03\x01\x01"
_COPY = (b"", b"\x00")
_END, _HEADER, _MAIN_STREAMS, _FILES, _PACK_INFO, _UNPACK_INFO, _SUBSTREAMS = 0, 1, 4, 5, 6, 7, 8
_SIZE, _CRC, _FOLDER, _UNPACK_SIZE, _STREAM_COUNT, _ENCODED_HEADER, _NAME = 9, 10, 11, 12, 13, 23, 17
_IMAGE = "5"


class _Reader:
    __slots__ = ("data", "at")

    def __init__(self, data):
        self.data = data
        self.at = 0

    def byte(self):
        value = self.data[self.at]
        self.at += 1
        return value

    def number(self):
        first = self.byte()
        mask = 0x80
        value = 0
        for index in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * index))
            value |= self.byte() << (8 * index)
            mask >>= 1
        return value

    def take(self, count):
        value = self.data[self.at:self.at + count]
        self.at += count
        return value

    def skip_bits(self, count):
        if not self.byte():
            self.take((count + 7) // 8)


def _streams(reader):
    """Pack offset and sizes, and per folder its coder, the coder's properties and its
    unpacked size."""
    position, sizes, folders = 0, [], []
    while True:
        kind = reader.byte()
        if kind == _END:
            return position, sizes, folders
        if kind == _PACK_INFO:
            position = reader.number()
            count = reader.number()
            while True:
                inner = reader.byte()
                if inner == _END:
                    break
                if inner == _SIZE:
                    sizes = [reader.number() for _ in range(count)]
                elif inner == _CRC:
                    reader.skip_bits(count)
                    reader.take(4 * count)
        elif kind == _UNPACK_INFO:
            if reader.byte() != _FOLDER:
                raise ValueError("a 7z unpack description without folders")
            count = reader.number()
            if reader.byte():
                raise ValueError("a 7z folder list kept elsewhere")
            for _ in range(count):
                if reader.number() != 1:
                    raise ValueError("a 7z folder with several coders")
                flags = reader.byte()
                coder = reader.take(flags & 0x0F)
                properties = reader.take(reader.number()) if flags & 0x20 else b""
                if coder != _LZMA and coder not in _COPY:
                    raise ValueError("a 7z folder coded with {0}".format(coder.hex()))
                folders.append([coder, properties, 0])
            while True:
                inner = reader.byte()
                if inner == _END:
                    break
                if inner == _UNPACK_SIZE:
                    for folder in folders:
                        folder[2] = reader.number()
                elif inner == _CRC:
                    reader.skip_bits(len(folders))
                    reader.take(4 * len(folders))
        elif kind == _SUBSTREAMS:
            while True:
                inner = reader.byte()
                if inner == _END:
                    break
                if inner == _STREAM_COUNT:
                    if any(reader.number() != 1 for _ in folders):
                        raise ValueError("a 7z folder holding several files")
                elif inner == _CRC:
                    reader.skip_bits(len(folders))
                    reader.take(4 * len(folders))
        else:
            raise ValueError("a 7z stream description of kind {0}".format(kind))


def _unpacked(data, position, sizes, folders):
    """Every folder's bytes."""
    found = []
    offset = 32 + position
    for size, (coder, properties, length) in zip(sizes, folders):
        packed = data[offset:offset + size]
        offset += size
        if coder in _COPY:
            found.append(packed[:length])
            continue
        settings = properties[0]
        lc, lp, pb = settings % 9, (settings // 9) % 5, settings // 45
        dictionary = struct.unpack("<I", properties[1:5])[0]
        decompressor = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dictionary}])
        found.append(decompressor.decompress(packed, max_length=length))
    return found


def _files(reader):
    """The names of the archive's files, in order."""
    count = reader.number()
    names = []
    while True:
        kind = reader.byte()
        if kind == _END:
            return names
        size = reader.number()
        payload = reader.take(size)
        if kind == _NAME:
            text = payload[1:].decode("utf-16-le")
            names = text.split("\x00")[:count]


def files(path):
    """Every file of a 7z archive by name, as bytes."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:6] != _SIGNATURE:
        raise ValueError("{0} is no 7z archive".format(path))
    offset, size = struct.unpack("<QQ", data[12:28])
    reader = _Reader(data[32 + offset:32 + offset + size])
    if reader.byte() == _ENCODED_HEADER:
        header = _unpacked(data, *_streams(reader))[0]
        reader = _Reader(header)
        if reader.byte() != _HEADER:
            raise ValueError("a 7z header of another kind")
    found, names = [], []
    while True:
        kind = reader.byte()
        if kind == _END:
            break
        if kind == _MAIN_STREAMS:
            found = _unpacked(data, *_streams(reader))
        elif kind == _FILES:
            names = _files(reader)
        else:
            raise ValueError("a 7z header part of kind {0}".format(kind))
    return dict(zip(names, found))


def image_inputs(path, graph):
    """The identifiers of every image input the graph ``graph`` of a package declares."""
    declared = set()
    for name, content in files(path).items():
        if not name.lower().endswith(".xml"):
            continue
        text = content.decode("utf-8", errors="replace")
        for block in re.finditer(r'<graph\b[^>]*pkgurl="pkg://([^"]+)"[^>]*>(.*?)</graph>', text, re.DOTALL):
            if block.group(1) != graph:
                continue
            for match in re.finditer(r'<input\b[^>]*\bidentifier="([^"]+)"[^>]*\btype="(\d+)"', block.group(2)):
                if match.group(2) == _IMAGE:
                    declared.add(match.group(1))
    return declared
