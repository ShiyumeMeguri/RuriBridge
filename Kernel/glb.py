# -*- coding: utf-8 -*-
"""A GLB written straight into the shared pages, never assembled first.

glTF's binary chunk is why this container was chosen over FBX, OBJ or USD: its
buffers are tightly packed little-endian arrays whose on-file layout is exactly
the layout a vertex array already has in memory. So the whole file can be laid
out before a single byte of geometry exists -- sizes follow from counts alone --
mapped, and then handed to the producer as writable windows. The producer's one
pass over its data lands in those windows, and at that instant the bytes already
are the file Painter's importer will open. Nothing is encoded, buffered or
copied between the two.

Attributes belong to the mesh, not to the primitive: glTF lets every primitive of
a mesh point at the same POSITION accessor and differ only in its indices, so an
object is deduplicated once and each material contributes an index range. Vertices
shared across a material boundary are then stored once rather than once per
material.

The one thing not known up front is the POSITION bounds, which glTF requires and
which exist only once the data is written. Rather than assemble the JSON
afterwards -- which would move the binary chunk -- the JSON chunk is reserved at
a padded size and written last into its fixed home; glTF permits trailing spaces
in that chunk precisely so this is legal.
"""

from __future__ import annotations

import contextlib
import json
import mmap
import struct
from pathlib import Path

from .log import logger

LOG = logger("glb")

GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
CHUNK_TYPE_JSON = 0x4E4F534A
CHUNK_TYPE_BINARY = 0x004E4942

COMPONENT_FLOAT32 = 5126
COMPONENT_UNSIGNED_32 = 5125
BUFFER_TARGET_ARRAY = 34962
BUFFER_TARGET_ELEMENT_ARRAY = 34963

ELEMENT_TYPE_BY_COUNT = {1: "SCALAR", 2: "VEC2", 3: "VEC3", 4: "VEC4"}

SEMANTIC_POSITION = "POSITION"
SEMANTIC_NORMAL = "NORMAL"
SEMANTIC_COLOR_0 = "COLOR_0"
TEXCOORD_PREFIX = "TEXCOORD_"

HEADER_SIZE = 12
CHUNK_HEADER_SIZE = 8
JSON_RESERVE_SLACK = 4096
JSON_RESERVE_PER_ACCESSOR = 96

BLENDER_TO_GLTF_ROTATION = [-0.7071067811865476, 0.0, 0.0, 0.7071067811865476]


class GlbError(RuntimeError):
    """A layout or a file that does not hold together."""


class AttributeLayout:
    """One vertex attribute of one mesh. Always float32."""

    __slots__ = ("semantic", "component_count")

    def __init__(self, semantic, component_count):
        if component_count not in ELEMENT_TYPE_BY_COUNT:
            raise GlbError("attribute {0} cannot have {1} components".format(
                semantic, component_count))
        self.semantic = semantic
        self.component_count = component_count

    @property
    def element_bytes(self):
        return self.component_count * 4


class PrimitiveLayout:
    """One material's index range into its mesh's shared vertices."""

    __slots__ = ("material_name", "index_count")

    def __init__(self, material_name, index_count):
        self.material_name = material_name
        self.index_count = index_count


class MeshLayout:
    """One source object: its vertices, its node transform, its materials."""

    __slots__ = ("name", "node_matrix", "vertex_count", "attributes", "primitives")

    def __init__(self, name, node_matrix, vertex_count, attributes, primitives):
        self.name = name
        self.node_matrix = list(node_matrix) if node_matrix is not None else None
        self.vertex_count = vertex_count
        self.attributes = tuple(attributes)
        self.primitives = tuple(primitives)
        if self.node_matrix is not None and len(self.node_matrix) != 16:
            raise GlbError("mesh {0!r} node matrix must be 16 column-major floats".format(name))
        semantics = [attribute.semantic for attribute in self.attributes]
        if SEMANTIC_POSITION not in semantics:
            raise GlbError("mesh {0!r} has no POSITION".format(name))
        if len(set(semantics)) != len(semantics):
            raise GlbError("mesh {0!r} declares a semantic twice".format(name))


class SceneLayout:
    """Everything one publish puts into one GLB."""

    __slots__ = ("meshes", "root_rotation", "generator")

    def __init__(self, meshes, root_rotation=None, generator="RuriDccBridge"):
        self.meshes = tuple(meshes)
        self.root_rotation = (list(root_rotation) if root_rotation is not None
                              else list(BLENDER_TO_GLTF_ROTATION))
        self.generator = generator


class _AccessorPlan:
    __slots__ = ("index", "byte_offset", "byte_length", "count", "component_type",
                 "component_count", "target", "needs_bounds")

    def __init__(self, index, byte_offset, byte_length, count, component_type,
                 component_count, target, needs_bounds):
        self.index = index
        self.byte_offset = byte_offset
        self.byte_length = byte_length
        self.count = count
        self.component_type = component_type
        self.component_count = component_count
        self.target = target
        self.needs_bounds = needs_bounds


def attribute_key(mesh_index, semantic):
    return ("attribute", mesh_index, semantic)


def index_key(mesh_index, primitive_index):
    return ("indices", mesh_index, primitive_index)


class GlbWriter:
    """A mapped, fully sized GLB whose binary chunk is the producer's canvas."""

    def __init__(self, path, layout, handle, mapped, accessors, binary_offset,
                 json_reserved_bytes, binary_bytes):
        self.path = Path(path)
        self.layout = layout
        self._handle = handle
        self._mapped = mapped
        self._view = memoryview(mapped)
        self._accessors = accessors
        self._binary_offset = binary_offset
        self._json_reserved_bytes = json_reserved_bytes
        self._binary_bytes = binary_bytes
        self._bounds = {}
        self._closed = False

    @classmethod
    def create(cls, arena, path, layout):
        """Lay the whole file out from counts, then map it."""
        accessors = {}
        cursor = 0
        index = 0
        for mesh_index, mesh in enumerate(layout.meshes):
            for attribute in mesh.attributes:
                byte_length = mesh.vertex_count * attribute.element_bytes
                accessors[attribute_key(mesh_index, attribute.semantic)] = _AccessorPlan(
                    index, cursor, byte_length, mesh.vertex_count, COMPONENT_FLOAT32,
                    attribute.component_count, BUFFER_TARGET_ARRAY,
                    attribute.semantic == SEMANTIC_POSITION)
                cursor += byte_length
                index += 1
            for primitive_index, primitive in enumerate(mesh.primitives):
                byte_length = primitive.index_count * 4
                accessors[index_key(mesh_index, primitive_index)] = _AccessorPlan(
                    index, cursor, byte_length, primitive.index_count,
                    COMPONENT_UNSIGNED_32, 1, BUFFER_TARGET_ELEMENT_ARRAY, False)
                cursor += byte_length
                index += 1
        binary_bytes = cursor

        placeholder = cls._serialise(layout, accessors, binary_bytes, {})
        reserved = len(placeholder) + JSON_RESERVE_SLACK + JSON_RESERVE_PER_ACCESSOR * len(accessors)
        reserved += (-reserved) % 4
        binary_offset = HEADER_SIZE + CHUNK_HEADER_SIZE + reserved + CHUNK_HEADER_SIZE
        total = binary_offset + binary_bytes

        handle, mapped = arena.create_mapped_file(path, total)
        struct.pack_into("<III", mapped, 0, GLB_MAGIC, GLB_VERSION, total)
        struct.pack_into("<II", mapped, HEADER_SIZE, reserved, CHUNK_TYPE_JSON)
        mapped[HEADER_SIZE + CHUNK_HEADER_SIZE:
                HEADER_SIZE + CHUNK_HEADER_SIZE + reserved] = b" " * reserved
        struct.pack_into("<II", mapped, binary_offset - CHUNK_HEADER_SIZE,
                         binary_bytes, CHUNK_TYPE_BINARY)
        LOG.debug("laid out %s: %d accessors, %d json bytes reserved, %d binary bytes",
                  path, len(accessors), reserved, binary_bytes)
        return cls(path, layout, handle, mapped, accessors, binary_offset,
                   reserved, binary_bytes)

    @contextlib.contextmanager
    def attribute_window(self, mesh_index, semantic):
        """The final bytes of one attribute, writable for the block's duration."""
        with self._window(self._accessor(attribute_key(mesh_index, semantic))) as window:
            yield window

    @contextlib.contextmanager
    def index_window(self, mesh_index, primitive_index):
        with self._window(self._accessor(index_key(mesh_index, primitive_index))) as window:
            yield window

    def _accessor(self, key):
        try:
            return self._accessors[key]
        except KeyError:
            raise GlbError("no accessor for {0}".format(key))

    @contextlib.contextmanager
    def _window(self, plan):
        start = self._binary_offset + plan.byte_offset
        window = self._view[start:start + plan.byte_length]
        try:
            yield window
        finally:
            window.release()

    def set_bounds(self, mesh_index, minimum, maximum):
        """Record the POSITION extents glTF requires, once they exist."""
        self._bounds[attribute_key(mesh_index, SEMANTIC_POSITION)] = (
            [float(value) for value in minimum], [float(value) for value in maximum])

    def finish(self):
        """Write the final JSON into its reserved home and release the map."""
        if self._closed:
            return self.path
        document = self._serialise(self.layout, self._accessors, self._binary_bytes, self._bounds)
        if len(document) > self._json_reserved_bytes:
            raise GlbError(
                "final glTF JSON is {0} bytes but only {1} were reserved; raise "
                "JSON_RESERVE_SLACK".format(len(document), self._json_reserved_bytes))
        padded = document + b" " * (self._json_reserved_bytes - len(document))
        start = HEADER_SIZE + CHUNK_HEADER_SIZE
        self._mapped[start:start + self._json_reserved_bytes] = padded
        self._mapped.flush()
        self.close()
        return self.path

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._view.release()
            self._mapped.close()
        except BufferError as error:
            LOG.error("%s stayed mapped because a caller still holds a window: %s",
                      self.path, error)
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, error_type, error_value, traceback):
        if error_type is None:
            self.finish()
        else:
            self.close()
        return False

    @staticmethod
    def _serialise(layout, accessors, binary_bytes, bounds):
        buffer_views = []
        accessor_entries = []
        ordered = sorted(accessors.items(), key=lambda item: item[1].index)
        for key, plan in ordered:
            buffer_views.append({
                "buffer": 0,
                "byteOffset": plan.byte_offset,
                "byteLength": plan.byte_length,
                "target": plan.target,
            })
            entry = {
                "bufferView": plan.index,
                "componentType": plan.component_type,
                "count": plan.count,
                "type": ELEMENT_TYPE_BY_COUNT[plan.component_count],
            }
            if plan.needs_bounds:
                extent = bounds.get(key)
                if extent is None:
                    entry["min"] = [0.0] * plan.component_count
                    entry["max"] = [0.0] * plan.component_count
                else:
                    entry["min"], entry["max"] = extent
            accessor_entries.append(entry)

        materials = []
        material_index_by_name = {}
        meshes = []
        nodes = []
        for mesh_index, mesh in enumerate(layout.meshes):
            attributes = {attribute.semantic:
                          accessors[attribute_key(mesh_index, attribute.semantic)].index
                          for attribute in mesh.attributes}
            primitives = []
            for primitive_index, primitive in enumerate(mesh.primitives):
                name = primitive.material_name
                if name not in material_index_by_name:
                    material_index_by_name[name] = len(materials)
                    materials.append({"name": name, "doubleSided": True})
                primitives.append({
                    "attributes": dict(attributes),
                    "indices": accessors[index_key(mesh_index, primitive_index)].index,
                    "material": material_index_by_name[name],
                })
            meshes.append({"name": mesh.name, "primitives": primitives})
            node = {"name": mesh.name, "mesh": mesh_index}
            if mesh.node_matrix is not None:
                node["matrix"] = mesh.node_matrix
            nodes.append(node)

        root_index = len(nodes)
        nodes.append({
            "name": "RuriBridgeRoot",
            "rotation": layout.root_rotation,
            "children": list(range(len(layout.meshes))),
        })
        document = {
            "asset": {"version": "2.0", "generator": layout.generator},
            "scene": 0,
            "scenes": [{"nodes": [root_index]}],
            "nodes": nodes,
            "meshes": meshes,
            "materials": materials,
            "accessors": accessor_entries,
            "bufferViews": buffer_views,
            "buffers": [{"byteLength": binary_bytes}],
        }
        return json.dumps(document, separators=(",", ":")).encode("utf-8")


def read_document(path):
    """Parse a GLB's JSON chunk without touching its binary chunk."""
    with open(path, "rb") as handle:
        magic, version, total = struct.unpack("<III", handle.read(HEADER_SIZE))
        if magic != GLB_MAGIC:
            raise GlbError("{0} is not a GLB".format(path))
        if version != GLB_VERSION:
            raise GlbError("{0} is GLB version {1}".format(path, version))
        length, chunk_type = struct.unpack("<II", handle.read(CHUNK_HEADER_SIZE))
        if chunk_type != CHUNK_TYPE_JSON:
            raise GlbError("{0} does not start with a JSON chunk".format(path))
        document = json.loads(handle.read(length).decode("utf-8"))
        binary_length, binary_type = struct.unpack("<II", handle.read(CHUNK_HEADER_SIZE))
        if binary_type != CHUNK_TYPE_BINARY:
            raise GlbError("{0} has no binary chunk".format(path))
    return document, total, binary_length


def binary_offset_of(path):
    with open(path, "rb") as handle:
        handle.seek(HEADER_SIZE)
        json_length, _chunk_type = struct.unpack("<II", handle.read(CHUNK_HEADER_SIZE))
    return HEADER_SIZE + CHUNK_HEADER_SIZE + json_length + CHUNK_HEADER_SIZE


@contextlib.contextmanager
def mapped_accessor(path, accessor_index):
    """A read-only window onto one accessor's bytes, for verification."""
    document, _total, _binary_length = read_document(path)
    accessor = document["accessors"][accessor_index]
    view = document["bufferViews"][accessor["bufferView"]]
    start = (binary_offset_of(path) + view.get("byteOffset", 0)
             + accessor.get("byteOffset", 0))
    with open(path, "rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    window = memoryview(mapped)[start:start + view["byteLength"]]
    try:
        yield window
    finally:
        window.release()
        mapped.close()
