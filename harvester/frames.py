"""Frame normalisation, masking and hashing."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    t_ms: int
    width: int
    height: int
    pixels: bytes   # one byte per pixel, values 0..levels-1


@dataclass(frozen=True)
class Mask:
    """Rectangle (inclusive x,y; exclusive x+w,y+h) blanked before hashing."""
    x: int
    y: int
    w: int
    h: int
    name: str = ""


def quantize(raw: bytes, levels: int) -> bytes:
    """Map 0-255 brightness to 0..levels-1 (for BRIGHTNESS-mode data)."""
    if levels >= 256:
        return bytes(raw)
    table = bytes(min(levels - 1, (v * levels) // 256) for v in range(256))
    return raw.translate(table)


def clamp(raw: bytes, levels: int) -> bytes:
    """RAW-mode data is already 0..levels-1; just guard against stray values."""
    top = levels - 1
    if max(raw, default=0) <= top:
        return bytes(raw)
    return bytes(min(v, top) for v in raw)


def normalize(raw_pixels: bytes, width: int, height: int, t_ms: int, levels: int,
              raw_mode: bool = False) -> Frame:
    px = clamp(raw_pixels, levels) if raw_mode else quantize(raw_pixels, levels)
    return Frame(t_ms, width, height, px)


def apply_masks(frame: Frame, masks: list[Mask]) -> bytes:
    if not masks:
        return frame.pixels
    buf = bytearray(frame.pixels)
    w, h = frame.width, frame.height
    for m in masks:
        x0, x1 = max(0, m.x), min(w, m.x + m.w)
        y0, y1 = max(0, m.y), min(h, m.y + m.h)
        if x0 >= x1:
            continue
        blank = bytes(x1 - x0)
        for y in range(y0, y1):
            buf[y * w + x0: y * w + x1] = blank
    return bytes(buf)


@dataclass(frozen=True)
class Patch:
    """An exact picture fragment: rows of hex shade digits at (x, y)."""
    x: int
    y: int
    rows: tuple[str, ...]

    @property
    def w(self) -> int:
        return len(self.rows[0])

    def matches(self, frame: Frame) -> bool:
        W = frame.width
        if self.x + self.w > W or self.y + len(self.rows) > frame.height:
            return False
        for r, row in enumerate(self.rows):
            off = (self.y + r) * W + self.x
            seg = frame.pixels[off:off + self.w]
            if any(v != int(c, 16) for v, c in zip(seg, row)):
                return False
        return True

    @staticmethod
    def cut(frame: Frame, x: int, y: int, w: int, h: int) -> "Patch":
        W = frame.width
        return Patch(x, y, tuple("".join("0123456789abcdef"[v] for v in
                                         frame.pixels[(y + r) * W + x:(y + r) * W + x + w])
                                 for r in range(h)))


@dataclass(frozen=True)
class Rule:
    """A kind of screen (e.g. the score screen). If ALL trigger patches are on
    screen exactly, the rule's masks are ignored when deciding what's new -
    the same way a Serum mask makes one colorization cover every score."""
    name: str
    trigger: tuple[Patch, ...]
    masks: tuple[Mask, ...]
    layout_cell: tuple[int, int] | None = None   # keep one frame per rough layout

    def layout(self, frame: Frame) -> str:
        """Which (cw x ch) cells inside the masks have anything lit. Score
        values don't change this much; a different player layout does."""
        if not self.layout_cell:
            return ""
        cw, ch = self.layout_cell
        W, px, bits = frame.width, frame.pixels, []
        for m in self.masks:
            for y0 in range(m.y, m.y + m.h, ch):
                for x0 in range(m.x, m.x + m.w, cw):
                    lit = any(px[y * W + x]
                              for y in range(y0, min(m.y + m.h, y0 + ch, frame.height))
                              for x in range(x0, min(m.x + m.w, x0 + cw, W)))
                    bits.append("1" if lit else "0")
        return "".join(bits)

    def applies(self, frame: Frame) -> bool:
        return all(p.matches(frame) for p in self.trigger)


def rule_for(frame: Frame, rules) -> "Rule | None":
    for r in rules:
        if r.applies(frame):
            return r
    return None


def frame_key(frame: Frame, masks: list[Mask], rules=(), digits=None) -> str:
    """Stable identity of a frame, ignoring masked regions and learned digits."""
    r = rule_for(frame, rules)
    if r is not None:
        masks = list(masks) + list(r.masks)
    elif digits:
        from .glyphs import erase
        frame = Frame(frame.t_ms, frame.width, frame.height, erase(frame, digits))
    h = hashlib.blake2b(digest_size=12)
    tag = f"{r.name}/{r.layout(frame)}" if r else ""
    h.update(f"{frame.width}x{frame.height}:{tag}:".encode())
    h.update(apply_masks(frame, masks))
    return h.hexdigest()


def is_blank(frame: Frame) -> bool:
    return not any(frame.pixels)
