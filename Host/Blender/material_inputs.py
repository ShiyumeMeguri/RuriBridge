# -*- coding: utf-8 -*-
"""Cutting a material's own textures into the inputs a texturing tool's shader reads.

The texturing side knows its shader -- which input is which lanes of which of the
material's textures, through which operation (its manifest's ``inputs``) -- and asks.
This side holds the textures and does the per-pixel part: one file per input, published
with the hash of its bytes so the reader can tell a texture it already holds.

The operations are the generator's, by name: ``invert`` is one minus the value;
``unpack_normal_alpha_green`` is Unity's DXT5nm, X = R·A, Y = G, Z = sqrt(1 - X² - Y²);
``unpack_normal_pair`` is the two-lane form, X = the first lane, Y = the second. Both
unpacks come out as the tangent normal stored n·0.5+0.5, the way a normal map is. A name
this side does not carry out is refused by name rather than guessed at.

Lanes cross as stored (see ``pixels``). A cut of two lanes is written as the first two
of three, because the shader samples such a texture's x and y; one lane is grey.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os

import numpy

from ...Kernel import record as record_module
from ...Kernel.log import logger
from . import mesh_publish, pixels

LOG = logger("blender.inputs")

_LANE_LETTERS = "rgba"
_UNPACKS = ("unpack_normal_alpha_green", "unpack_normal_pair")


def _cut(values, job):
    """The lanes one job asks for, through its operation."""
    picked = numpy.stack([values[..., _LANE_LETTERS.index(letter)] for letter in job["channels"]],
                         axis=-1)
    operation = job["operation"]
    if operation == "":
        return picked
    if operation == "invert":
        return 1.0 - picked
    if operation in _UNPACKS:
        if picked.shape[-1] != 2:
            raise ValueError("{0} unpacks two lanes, and {1} names {2}".format(
                operation, job["input"], job["channels"]))
        packed_x = picked[..., 0]
        if operation == "unpack_normal_alpha_green":
            packed_x = packed_x * values[..., 3]
        x = packed_x * 2.0 - 1.0
        y = picked[..., 1] * 2.0 - 1.0
        z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
        return numpy.stack([x, y, z], axis=-1) * 0.5 + 0.5
    raise ValueError("operation {0!r} is not one this side carries out".format(operation))


def _laid_out(lanes):
    """Lanes as a file holds them: one is grey, two are the first two of three."""
    count = lanes.shape[-1]
    if count == 2:
        return numpy.concatenate([lanes, numpy.zeros_like(lanes[..., :1])], axis=-1)
    return lanes


def _held_images(material):
    """The images a generated material's record holds, by slot."""
    declaration = material.get(mesh_publish.SHADING_DECLARATION)
    if declaration is None:
        return None
    group = str(dict(declaration["images"])["group"])
    return dict(material.get(group) or {})


def bake(publisher, request, worn):
    """Answer one request: cut every asked-for input and publish them. Returns one line.

    ``worn`` is the materials the model wears, by name -- the table the request was
    written from, so a name means the material the meshes render with and not another
    of the same name a library brought along.
    """
    answered = []
    written = 0
    with publisher.staging() as staging:
        encode = []
        for entry in request["texture_sets"]:
            files = []
            missing = {}
            material = worn.get(entry["material"])
            held = _held_images(material) if material is not None else None
            if held is None:
                missing[""] = "the model wears no generated material called {0!r}".format(
                    entry["material"])
            read = {}
            for job in entry["jobs"] if held is not None else ():
                path = pixels.file_of(held.get(job["source"]))
                if not path:
                    missing[job["input"]] = "{0} holds no texture file in {1}".format(
                        material.name, job["source"])
                    continue
                if path not in read:
                    read[path] = pixels.read(path)
                values, wide = read[path]
                try:
                    lanes = _laid_out(_cut(values, job))
                except ValueError as error:
                    missing[job["input"]] = str(error)
                    continue
                name = "{0}_{1}.png".format(entry["name"], job["input"])
                encode.append((staging.path(name), lanes, job["wide"] or wide))
                files.append({"input": job["input"], "file": name})
            answered.append({"name": entry["name"], "material": entry["material"],
                             "shader": entry["shader"], "inputs": files, "missing": missing})
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            encoded = list(pool.map(lambda job: (job[0], pixels.png(job[1], job[2])), encode))
        digests = {}
        for path, payload in encoded:
            with open(path, "wb") as handle:
                handle.write(payload)
            digests[os.path.basename(str(path))] = hashlib.sha1(payload).hexdigest()
            written += 1
        for entry in answered:
            for item in entry["inputs"]:
                item["hash"] = digests[item["file"]]
        staging.publish(record_module.inputs("Blender", answered))
    refused = sum(len(entry["missing"]) for entry in answered)
    LOG.info("cut %d input(s) for %d Texture Set(s); %d not cut", written, len(answered), refused)
    for entry in answered:
        for name, why in sorted(entry["missing"].items()):
            LOG.warning("%s: %s not cut: %s", entry["name"], name or "nothing", why)
    return "cut {0} texture input(s) for {1} Texture Set(s){2}".format(
        written, len(answered), "; {0} could not be cut (see the log)".format(refused)
        if refused else "")
