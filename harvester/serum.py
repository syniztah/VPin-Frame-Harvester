"""Read a ColorizingDMD/Serum project and recognise frames the way Serum does.

Serum identifies a frame by a CRC-32 of its pixels OUTSIDE the frame's
comparison mask (mask byte 0 = compared). In "shape mode" every lit pixel
counts as 1. This module reads the hashes, masks and shape modes from a
.cRom (uncompressed project ROM, as saved by ColorizingDMD), and optionally
the original frames from the matching .cRP, so captured frames can be checked:
  - which project frames were reached (coverage),
  - which captured frames Serum would NOT recognise (left uncoloured).

Format follows Load_cRom / Save_cRP in github.com/SerumColor/ColorizingDMD.
"""
from __future__ import annotations

import os
import struct
import zlib
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .frames import Frame


@dataclass
class SerumProject:
    name: str
    width: int
    height: int
    hashes: np.ndarray        # uint32 per project frame
    shape: np.ndarray         # uint8 per project frame (1 = shape mode)
    mask_id: np.ndarray       # uint8 per project frame (255 = no mask)
    masks: np.ndarray         # (n_masks, h, w) uint8, nonzero = ignored
    originals: np.ndarray | None = None   # (n, h, w) from the .cRP, if given

    def __post_init__(self):
        # (mask_id, shape) -> {hash: [project frame indexes]}
        self._lookup: dict[tuple, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        for i, (h, s, m) in enumerate(zip(self.hashes.tolist(), self.shape.tolist(),
                                          self.mask_id.tolist())):
            self._lookup[(m, s)][h].append(i)
        self._keep = {m: (self.masks[m].ravel() == 0) for m, _ in self._lookup if m != 255}

    @property
    def n(self) -> int:
        return len(self.hashes)

    def identify(self, frame: Frame) -> list[int]:
        """Project frames Serum would match this frame to (empty = unrecognised)."""
        if (frame.width, frame.height) != (self.width, self.height):
            return []
        px = np.frombuffer(frame.pixels, np.uint8)
        found: list[int] = []
        for (m, s), table in self._lookup.items():
            v = np.minimum(px, 1) if s else px
            if m != 255:
                v = v[self._keep[m]]
            hit = table.get(zlib.crc32(v.tobytes()))
            if hit:
                found.extend(hit)
        return found


def load(crom_path: str, crp_path: str | None = None) -> SerumProject:
    with open(crom_path, "rb") as f:
        name = f.read(64).split(b"\0")[0].decode("latin1")
        (lh,) = struct.unpack("<I", f.read(4))
        w, h = struct.unpack("<II", f.read(8))
        new = lh >= 14 * 4
        if new:
            f.read(8)                                    # extra-resolution size
        n, _ncol = struct.unpack("<II", f.read(8))
        if not new:
            f.read(4)
        (n_masks,) = struct.unpack("<I", f.read(4))
        if not new:
            f.read(4)
        f.read(4)                                        # sprites
        if lh >= 13 * 4:
            f.read(2)                                    # backgrounds (UINT16)
        is256 = struct.unpack("<i", f.read(4))[0] if lh >= 20 * 4 else 0
        if is256:
            raise SystemExit("256x64 projects aren't supported yet.")
        hashes = np.frombuffer(f.read(4 * n), "<u4").copy()
        shape = np.frombuffer(f.read(n), np.uint8).copy()
        mask_id = np.frombuffer(f.read(n), np.uint8).copy()
        if not new:
            f.read(n)                                    # old MovRctID
        masks = np.frombuffer(f.read(n_masks * w * h), np.uint8).reshape(n_masks, h, w).copy()
    originals = None
    if crp_path is None:
        guess = os.path.splitext(crom_path)[0] + ".cRP"
        crp_path = guess if os.path.exists(guess) else None
    if crp_path:
        with open(crp_path, "rb") as f:
            f.read(64)
            originals = np.frombuffer(f.read(n * w * h), np.uint8).reshape(n, h, w).copy()
    return SerumProject(name, w, h, hashes, shape, mask_id, masks, originals)


def sheet(frames: list[np.ndarray], labels: list[str], path: str, levels: int = 4,
          cols: int = 4, masks: list | None = None) -> bool:
    """Picture sheet of frames with a label above each. If `masks` is given
    (one per frame, or None), the masked (ignored) area is tinted red, so you
    can see which part of the screen Serum actually compares."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    if not frames:
        return False
    h, w = frames[0].shape
    cw, ch = w * 2 + 8, h * 2 + 16
    rows = (len(frames) + cols - 1) // cols
    img = Image.new("RGB", (cols * cw, rows * ch), (35, 35, 35))
    d = ImageDraw.Draw(img)
    k = 255 // max(1, levels - 1)
    for i, (fr, lab) in enumerate(zip(frames, labels)):
        x, y = (i % cols) * cw + 4, (i // cols) * ch
        g = np.minimum(fr.astype(np.int32) * k, 255).astype(np.uint8)
        rgb = np.stack([g, g, g], -1)
        m = masks[i] if masks else None
        if m is not None:
            sel = m > 0
            rgb[sel] = (rgb[sel] * 0.45 + np.array([200, 30, 30]) * 0.55).astype(np.uint8)
        img.paste(Image.fromarray(rgb).resize((w * 2, h * 2), Image.NEAREST), (x, y + 13))
        d.text((x, y + 1), lab, fill=(220, 220, 220))
    img.save(path)
    return True


def _planes(px: np.ndarray) -> np.ndarray:
    """(N, H*W) shade values -> (N, 4, H*W/8) packed bit planes."""
    px = np.minimum(px, 15).astype(np.uint8)
    return np.stack([np.packbits((px >> b) & 1, axis=-1) for b in range(4)], 1)


_POP = np.array([bin(i).count("1") for i in range(256)], np.uint16)


def nearest(targets: list[np.ndarray], target_masks: list, captured: np.ndarray):
    """For each target frame, the captured frame with the fewest differing
    pixels (outside the target's mask). Returns [(index, n_diff)]."""
    cap = _planes(captured.reshape(len(captured), -1))           # (N,4,B)
    out = []
    for fr, m in zip(targets, target_masks):
        t = _planes(fr.reshape(1, -1))[0]                          # (4,B)
        keep = np.packbits((m.reshape(-1) == 0) if m is not None else np.ones(fr.size, bool))
        x = np.bitwise_or.reduce(cap ^ t[None], axis=1) & keep      # (N,B) differing pixels
        cnt = _POP[x].sum(1)
        j = int(cnt.argmin())
        out.append((j, int(cnt[j])))
    return out
