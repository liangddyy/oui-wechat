"""Render qrcode's matrix as a PNG without adding an imaging dependency."""

from __future__ import annotations

import struct
import zlib

import qrcode


def qr_png(content: str) -> bytes:
    qr = qrcode.QRCode(border=4)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 8
    size = len(matrix) * scale
    raw = b"".join(
        (b"\0" + bytes(pixel for bit in row for pixel in [0 if bit else 255] * scale)) * scale
        for row in matrix
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack(
            "!I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
