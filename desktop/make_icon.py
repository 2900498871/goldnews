# -*- coding: utf-8 -*-
"""生成多尺寸 exe 图标（纯标准库，无需 Pillow）：深蓝底 + 金色币 + A 字。"""
from __future__ import annotations

import math
import os
import struct

SIZES = (16, 32, 48, 64, 128)
BG = (58, 33, 18, 255)        # 深蓝 BGRA
GOLD = (70, 178, 232, 255)    # 金色 BGRA
GOLD_EDGE = (38, 130, 180, 255)
INK = (58, 33, 18, 255)       # 币面上的 A 用深蓝


def _dist_seg(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def pixel(x, y, s):
    u, v = (x + 0.5) / s, (y + 0.5) / s
    # 圆角方形底
    r = 0.10
    cx = min(max(u, r), 1 - r)
    cy = min(max(v, r), 1 - r)
    if math.hypot(u - cx, v - cy) > r:
        return (0, 0, 0, 0)
    # 金色圆币
    d = math.hypot(u - 0.5, v - 0.5)
    if d > 0.42:
        return BG
    if d > 0.385:
        return GOLD_EDGE
    # 币面上的字母 A（两条斜边 + 中横）
    lw = 0.045
    if (_dist_seg(u, v, 0.32, 0.72, 0.50, 0.28) < lw or
            _dist_seg(u, v, 0.68, 0.72, 0.50, 0.28) < lw or
            _dist_seg(u, v, 0.39, 0.57, 0.61, 0.57) < lw):
        return INK
    return GOLD


def image(s):
    rows = []
    for y in range(s - 1, -1, -1):
        rows.append(b"".join(struct.pack("<4B", *pixel(x, y, s)) for x in range(s)))
    xor = b"".join(rows)
    mask = b"\x00" * (s * (((s + 31) // 32) * 4))  # 每行按 32 位对齐
    bih = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return bih + xor + mask


def build(path: str):
    imgs = [(s, image(s)) for s in SIZES]
    n = len(imgs)
    offset = 6 + 16 * n
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    for s, data in imgs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(d for _, d in imgs))
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GoldDesk.ico")
    build(out)
    print("icon written:", out, os.path.getsize(out), "bytes, sizes", SIZES)
