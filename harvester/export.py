"""Text dump export.

Format written (one block per frame, blank line between frames):

    0x0001a2b3            <- timestamp in ms, 8 hex digits
    0000123300000...      <- one line per row, one hex digit per pixel
    ...

This is the common PinMAME/dmdext-style txt dump layout used by colorizing
tools. VERIFY against a dump your tool already imports and adjust here if
needed (e.g. timestamp units, shade range, line endings).
"""
from __future__ import annotations

from typing import IO

from .frames import Frame

HEX = "0123456789abcdef"


def frame_to_txt(frame: Frame) -> str:
    rows = [f"0x{frame.t_ms & 0xFFFFFFFF:08x}"]
    w = frame.width
    for y in range(frame.height):
        row = frame.pixels[y * w:(y + 1) * w]
        rows.append("".join(HEX[min(v, 15)] for v in row))
    return "\n".join(rows) + "\n\n"


def write_frame(fh: IO[str], frame: Frame) -> None:
    fh.write(frame_to_txt(frame))


_HEXVAL = bytes((int(chr(c), 16) if chr(c) in "0123456789abcdefABCDEF" else 0) for c in range(256))


def read_txt(path: str) -> list[Frame]:
    """Parse a text dump (ours, VPinMAME's or DMDExt's): a 0x timestamp line,
    then one line per row with one hex digit per pixel, blank line between
    frames. Fast path for big dumps; falls back to the simple parser."""
    try:
        return _read_txt_fast(path)
    except Exception:
        return _read_txt_simple(path)


def _read_txt_fast(path: str) -> list[Frame]:
    data = open(path, "rb").read()
    frames: list[Frame] = []
    t_ms, rows = None, []

    def flush():
        if t_ms is not None and rows:
            w = len(rows[0])
            good = [r for r in rows if len(r) == w]
            frames.append(Frame(t_ms, w, len(good), b"".join(good).translate(_HEXVAL)))

    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        if line[:2] in (b"0x", b"0X"):
            flush()
            t_ms, rows = int(line, 16), []
        else:
            rows.append(line)
    flush()
    return frames


def _read_txt_simple(path: str) -> list[Frame]:
    frames: list[Frame] = []
    with open(path, encoding="ascii") as fh:
        block: list[str] = []
        for line in list(fh) + ["\n"]:
            line = line.strip()
            if line:
                block.append(line)
                continue
            if block:
                t_ms = int(block[0], 16)
                rows = block[1:]
                w, h = len(rows[0]), len(rows)
                pix = bytes(HEX.index(c) for r in rows for c in r.lower())
                frames.append(Frame(t_ms, w, h, pix))
                block = []
    return frames


def save_png(frame: Frame, path: str, levels: int) -> bool:
    """Optional preview. Returns False if Pillow isn't installed."""
    try:
        from PIL import Image
    except ImportError:
        return False
    scale = 255 // max(1, levels - 1)
    img = Image.frombytes("L", (frame.width, frame.height),
                          bytes(min(255, v * scale) for v in frame.pixels))
    img = img.resize((frame.width * 4, frame.height * 4), Image.NEAREST)
    img.save(path)
    return True
