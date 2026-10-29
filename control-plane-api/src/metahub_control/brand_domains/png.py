"""Bounded PNG validation for owner-qualified PRD-142 brand assets."""

from __future__ import annotations

import binascii
import hashlib
import struct
import zlib

from metahub_shared.runtime.errors import ApiError

MAX_ASSET_BYTES = 512 * 1024
MIN_DIMENSION = 16
MAX_DIMENSION = 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_brand_png(value: object) -> tuple[bytes, int, int, str]:
    if type(value) is not bytes or not value.startswith(PNG_SIGNATURE):
        raise ApiError("branding.logo_invalid")
    if len(value) > MAX_ASSET_BYTES:
        raise ApiError("branding.logo_too_large")
    if len(value) < 33:
        raise ApiError("branding.logo_invalid")
    offset = len(PNG_SIGNATURE)
    seen_ihdr = seen_idat = seen_iend = False
    width = height = color_type = 0
    compressed = bytearray()
    while offset < len(value):
        if len(value) - offset < 12:
            raise ApiError("branding.logo_invalid")
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        chunk_type = value[offset + 4 : offset + 8]
        end = offset + 12 + length
        if chunk_type not in {b"IHDR", b"IDAT", b"IEND"} or end > len(value):
            raise ApiError("branding.logo_invalid")
        body = value[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", value[offset + 8 + length : end])[0]
        if (binascii.crc32(chunk_type + body) & 0xFFFFFFFF) != crc:
            raise ApiError("branding.logo_invalid")
        if chunk_type == b"IHDR":
            if seen_ihdr or length != 13:
                raise ApiError("branding.logo_invalid")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", body)
            )
            if not (
                MIN_DIMENSION <= width <= MAX_DIMENSION
                and MIN_DIMENSION <= height <= MAX_DIMENSION
                and bit_depth == 8
                and color_type in {2, 6}
                and compression == 0
                and filter_method == 0
                and interlace == 0
            ):
                raise ApiError("branding.logo_invalid")
            seen_ihdr = True
        elif chunk_type == b"IDAT":
            if not seen_ihdr or seen_iend:
                raise ApiError("branding.logo_invalid")
            seen_idat = True
            compressed.extend(body)
        else:
            if not seen_ihdr or not seen_idat or seen_iend or length != 0 or end != len(value):
                raise ApiError("branding.logo_invalid")
            seen_iend = True
        offset = end
    if not (seen_ihdr and seen_idat and seen_iend):
        raise ApiError("branding.logo_invalid")
    expected = height * (1 + width * (4 if color_type == 6 else 3))
    if expected > MAX_DIMENSION * (1 + MAX_DIMENSION * 4):
        raise ApiError("branding.logo_invalid")
    try:
        inflater = zlib.decompressobj()
        decoded = inflater.decompress(bytes(compressed), expected + 1)
        decoded += inflater.flush()
    except zlib.error:
        raise ApiError("branding.logo_invalid") from None
    if (
        len(decoded) != expected
        or inflater.unconsumed_tail
        or inflater.unused_data
        or not inflater.eof
    ):
        raise ApiError("branding.logo_invalid")
    return value, width, height, hashlib.sha256(value).hexdigest()


__all__ = ["MAX_ASSET_BYTES", "validate_brand_png"]
