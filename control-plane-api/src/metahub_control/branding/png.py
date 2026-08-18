"""受限 PNG 解析器：只接受无交错 8-bit RGB/RGBA，拒绝脚本载荷和解压炸弹。"""

from __future__ import annotations

import binascii
import struct
import zlib

from metahub_shared.runtime.errors import ApiError

MAX_LOGO_BYTES = 128 * 1024
MIN_LOGO_DIMENSION = 16
MAX_LOGO_DIMENSION = 512
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_png(value: object) -> tuple[bytes, int, int, str]:
    """校验 PNG 结构并返回 bytes、宽、高和完整小写 SHA-256。"""

    if type(value) is not bytes or not value.startswith(PNG_SIGNATURE):
        raise ApiError("branding.logo_invalid")
    if len(value) > MAX_LOGO_BYTES:
        raise ApiError("branding.logo_too_large")
    if len(value) < 33:
        raise ApiError("branding.logo_invalid")
    offset = len(PNG_SIGNATURE)
    seen_ihdr = False
    seen_idat = False
    seen_iend = False
    width = height = color_type = bit_depth = 0
    compressed = bytearray()
    expected_chunk = {b"IHDR", b"IDAT", b"IEND"}
    while offset < len(value):
        if len(value) - offset < 12:
            raise ApiError("branding.logo_invalid")
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        chunk_type = value[offset + 4 : offset + 8]
        end = offset + 12 + length
        if chunk_type not in expected_chunk or end > len(value):
            raise ApiError("branding.logo_invalid")
        chunk = value[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", value[offset + 8 + length : end])[0]
        if (binascii.crc32(chunk_type + chunk) & 0xFFFFFFFF) != expected_crc:
            raise ApiError("branding.logo_invalid")
        if chunk_type == b"IHDR":
            if seen_ihdr or length != 13:
                raise ApiError("branding.logo_invalid")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk)
            if not (
                MIN_LOGO_DIMENSION <= width <= MAX_LOGO_DIMENSION
                and MIN_LOGO_DIMENSION <= height <= MAX_LOGO_DIMENSION
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
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            if not seen_ihdr or not seen_idat or seen_iend or length != 0 or end != len(value):
                raise ApiError("branding.logo_invalid")
            seen_iend = True
        offset = end
    if not (seen_ihdr and seen_idat and seen_iend):
        raise ApiError("branding.logo_invalid")
    bytes_per_pixel = 4 if color_type == 6 else 3
    expected_size = height * (1 + width * bytes_per_pixel)
    if expected_size > MAX_LOGO_BYTES * 16:
        raise ApiError("branding.logo_invalid")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), expected_size + 1)
        if len(decoded) > expected_size or decompressor.unconsumed_tail or not decompressor.eof:
            raise ApiError("branding.logo_invalid")
        decoded += decompressor.flush()
    except (zlib.error, ValueError):
        raise ApiError("branding.logo_invalid") from None
    if len(decoded) != expected_size or decompressor.unused_data:
        raise ApiError("branding.logo_invalid")
    import hashlib

    return value, width, height, hashlib.sha256(value).hexdigest()


__all__ = ["MAX_LOGO_BYTES", "validate_png"]
