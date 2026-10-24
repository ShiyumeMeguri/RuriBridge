# -*- coding: utf-8 -*-
"""MurmurHash3, x64 variant, 128 bits: the checksum Painter keeps on each dataset of a project file.

Painter stores it, seeded with ``CHECKSUM_SEED``, as the two 64-bit halves in the dataset's
``m3_x64_128`` attribute; a dataset that was changed is only a dataset Painter accepts again when
its attribute says what its bytes hash to.
"""

from __future__ import annotations

import struct

#: The seed Painter hashes every dataset with.
CHECKSUM_SEED = 0xF13A0239

_MASK = 0xFFFFFFFFFFFFFFFF
_FIRST = 0x87C37B91114253D5
_SECOND = 0x4CF5AD432745937F


def _mix(value):
    value ^= value >> 33
    value = (value * 0xFF51AFD7ED558CCD) & _MASK
    value ^= value >> 33
    value = (value * 0xC4CEB9FE1A85EC53) & _MASK
    value ^= value >> 33
    return value


def hash128(data, seed=CHECKSUM_SEED):
    """The two 64-bit halves ``(low, high)`` of ``data``'s hash, as Painter stores them."""
    length = len(data)
    low = high = seed
    blocks = length - length % 16
    for first, second in struct.iter_unpack("<QQ", memoryview(data)[:blocks]):
        first = (first * _FIRST) & _MASK
        first = ((first << 31) | (first >> 33)) & _MASK
        low ^= (first * _SECOND) & _MASK
        low = ((low << 27) | (low >> 37)) & _MASK
        low = ((low + high) * 5 + 0x52DCE729) & _MASK
        second = (second * _SECOND) & _MASK
        second = ((second << 33) | (second >> 31)) & _MASK
        high ^= (second * _FIRST) & _MASK
        high = ((high << 31) | (high >> 33)) & _MASK
        high = ((high + low) * 5 + 0x38495AB5) & _MASK
    tail = bytes(data[blocks:])
    if len(tail) > 8:
        second = int.from_bytes(tail[8:], "little")
        second = (second * _SECOND) & _MASK
        second = ((second << 33) | (second >> 31)) & _MASK
        high ^= (second * _FIRST) & _MASK
    if tail:
        first = int.from_bytes(tail[:8], "little")
        first = (first * _FIRST) & _MASK
        first = ((first << 31) | (first >> 33)) & _MASK
        low ^= (first * _SECOND) & _MASK
    low ^= length
    high ^= length
    low = (low + high) & _MASK
    high = (high + low) & _MASK
    low = _mix(low)
    high = _mix(high)
    low = (low + high) & _MASK
    high = (high + low) & _MASK
    return low, high
