# -*- coding: utf-8 -*-
"""从「日常小助手-原图.png」生成多尺寸 exe / 托盘图标（需要 Pillow）。"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "日常小助手-原图.png")
OUT = os.path.join(HERE, "GoldDesk.ico")
PREVIEW = os.path.join(HERE, "ui", "icon.png")


def build():
    im = Image.open(SRC).convert("RGBA")
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2)).resize((1024, 1024), Image.LANCZOS)
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 1023, 1023], radius=210, fill=255)
    im.putalpha(mask)
    im.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    im.resize((128, 128), Image.LANCZOS).save(PREVIEW)
    return OUT


if __name__ == "__main__":
    out = build()
    print("icon written:", out, os.path.getsize(out), "bytes, sizes", SIZES)
