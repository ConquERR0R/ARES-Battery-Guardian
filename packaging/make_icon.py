"""Draw the ARES program icon and save it as assets/ares.ico (PNG-compressed ICO, 16-256 px).

The icon is drawn with Qt (no external image files or tools), so it can be regenerated on any
machine:  python packaging/make_icon.py
The generated assets/ares.ico is committed; build_installer.bat does not need to run this.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parent.parent


def draw(size: int) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    # dark rounded tile
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1b2230"))
    p.drawRoundedRect(QRectF(1 * s, 1 * s, 62 * s, 62 * s), 12 * s, 12 * s)
    # battery outline + terminal
    pen = QColor("#e6e9f0")
    p.setPen(Qt.NoPen)
    p.setBrush(pen)
    p.drawRoundedRect(QRectF(7 * s, 18 * s, 44 * s, 28 * s), 5 * s, 5 * s)
    p.drawRoundedRect(QRectF(51 * s, 26 * s, 6 * s, 12 * s), 2 * s, 2 * s)
    p.setBrush(QColor("#1b2230"))
    p.drawRoundedRect(QRectF(10 * s, 21 * s, 38 * s, 22 * s), 3 * s, 3 * s)
    # green charge level (about 80 %, the default upper limit)
    p.setBrush(QColor("#3ddc97"))
    p.drawRoundedRect(QRectF(12.5 * s, 23.5 * s, 28 * s, 17 * s), 2 * s, 2 * s)
    if size >= 32:
        f = QFont()
        f.setPixelSize(max(8, round(14 * s)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#0d1117"))
        p.drawText(QRectF(12.5 * s, 23.5 * s, 28 * s, 17 * s), Qt.AlignCenter, "A")
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: Path) -> None:
    images = [(sz, draw(sz)) for sz in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for sz, png in images:
        dim = 0 if sz >= 256 else sz          # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + blobs)


if __name__ == "__main__":
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    out = ROOT / "assets" / "ares.ico"
    write_ico(out)
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
