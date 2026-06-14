"""Pure-Python mowing-path map renderer — no external dependencies.

getPath.value binary format
---------------------------
Offset  Size  Type        Description
0       2     uint16 LE   Version (always 1)
2       2     uint16 LE   Map width  in pixels (e.g. 912)
4       2     uint16 LE   Map height in pixels (e.g. 705)
6       8     bytes       Metadata (origin coords, varies per snapshot)
14      4     uint32 LE   Compressed-payload length
18      …     zlib        Compressed bitmap: (width × height) bits,
                          row-major, MSB-first within each byte.

Pixel (x, y):
  byte  = y * (width // 8) + x // 8
  bit   = 7 − (x % 8)          # 1 = mowed
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Sequence

# RGB colours for successive zone sessions (Material Design palette)
ZONE_COLORS: list[tuple[int, int, int]] = [
    (76, 175, 80),    # green
    (33, 150, 243),   # blue
    (255, 152, 0),    # orange
    (156, 39, 176),   # purple
    (0, 188, 212),    # cyan
    (255, 87, 34),    # deep-orange
]

_BG_BYTES = bytes((230, 230, 230))   # light grey background (unmowed)
_POS_BYTES = bytes((244, 67, 54))    # red  — mower position dot
_DIR_BYTES = bytes((255, 193, 7))    # amber — direction tip


def decode_path_value(value: bytes) -> tuple[int, int, bytes] | None:
    """Decode getPath.value → (width, height, bitmap) or None on error."""
    if len(value) < 20:
        return None
    try:
        w = struct.unpack_from('<H', value, 2)[0]
        h = struct.unpack_from('<H', value, 4)[0]
        if w == 0 or h == 0:
            return None
        zlib_idx = value.index(b'x\xda')
        bitmap = zlib.decompress(value[zlib_idx:])
        if len(bitmap) < (w * h + 7) // 8:
            return None
        return w, h, bitmap
    except (ValueError, zlib.error, struct.error):
        return None


def bitmap_diff(before: bytes, after: bytes) -> bytes:
    """Return a bitmap with bits that are set in *after* but not in *before*."""
    n = min(len(before), len(after))
    result = bytearray(len(after))
    for i in range(n):
        result[i] = after[i] & ~before[i]
    result[n:] = after[n:]
    return bytes(result)


def render_map_png(
    past_sessions: Sequence[tuple[bytes, tuple[int, int, int]]],
    current_diff: bytes | None,
    current_color: tuple[int, int, int],
    w: int,
    h: int,
    pos: tuple[int, int, float] | None,
    scale: int = 4,
) -> bytes:
    """Render a PNG map from zone-session bitmaps.

    Parameters
    ----------
    past_sessions   Completed zones: [(diff_bitmap, rgb_color), …]
    current_diff    Diff bitmap for the ongoing zone (None if not mowing)
    current_color   RGB colour for the ongoing zone
    w, h            Original bitmap dimensions (e.g. 912, 705)
    pos             (x, y, angle_rad) current mower position, or None
    scale           Downsampling factor; default 4 → 228 × 176 output
    """
    ow = w // scale
    oh = h // scale
    row_bytes = w // 8
    valid_bytes = row_bytes * h

    # Flat RGB canvas initialised to background colour
    canvas = bytearray(_BG_BYTES * (ow * oh))

    def _paint(bitmap: bytes, color: tuple[int, int, int]) -> None:
        cr, cg, cb = color
        for byte_idx, byte_val in enumerate(bitmap[:valid_bytes]):
            if not byte_val:
                continue
            src_row = byte_idx // row_bytes
            col_byte = byte_idx % row_bytes
            for bit in range(8):
                if byte_val & (1 << (7 - bit)):
                    gx = (col_byte * 8 + bit) // scale
                    gy = src_row // scale
                    if 0 <= gx < ow and 0 <= gy < oh:
                        i = (gy * ow + gx) * 3
                        canvas[i] = cr
                        canvas[i + 1] = cg
                        canvas[i + 2] = cb

    for bitmap, color in past_sessions:
        _paint(bitmap, color)
    if current_diff is not None:
        _paint(current_diff, current_color)

    # Mower position: filled circle + direction tip
    if pos is not None:
        px, py, angle = pos
        gx, gy = int(px) // scale, int(py) // scale
        dot_r = 2
        for dy in range(-dot_r, dot_r + 1):
            for dx in range(-dot_r, dot_r + 1):
                if dx * dx + dy * dy <= dot_r * dot_r + 1:
                    x2, y2 = gx + dx, gy + dy
                    if 0 <= x2 < ow and 0 <= y2 < oh:
                        i = (y2 * ow + x2) * 3
                        canvas[i:i + 3] = _POS_BYTES
        ex = int(gx + (dot_r + 3) * math.cos(angle))
        ey = int(gy + (dot_r + 3) * math.sin(angle))
        if 0 <= ex < ow and 0 <= ey < oh:
            i = (ey * ow + ex) * 3
            canvas[i:i + 3] = _DIR_BYTES

    # Assemble PNG rows (filter byte 0 = no filter)
    row_w3 = ow * 3
    rows = bytearray(oh * (1 + row_w3))
    for gy in range(oh):
        off = gy * (1 + row_w3)
        rows[off] = 0
        rows[off + 1: off + 1 + row_w3] = canvas[gy * row_w3: (gy + 1) * row_w3]

    idat = zlib.compress(bytes(rows), level=6)
    ihdr = struct.pack('>IIBBBBB', ow, oh, 8, 2, 0, 0, 0)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b'\x89PNG\r\n\x1a\n'
        + _chunk(b'IHDR', ihdr)
        + _chunk(b'IDAT', idat)
        + _chunk(b'IEND', b'')
    )
