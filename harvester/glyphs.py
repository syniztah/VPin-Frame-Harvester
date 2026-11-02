"""Learn a game's number digits from captured frames, and ignore them when deduping.

Why: animations often show a live number (score, timer, value) on top. Every
different number made a "new" frame, so one animation turned into hundreds.

How it learns (safely): between two captured frames, look at the SMALL shapes
that changed. If only a few changed, and each one was swapped for a different
shape at the same spot and height (the way a score digit ticks over), both
shapes are counted. Shapes swapped like that several times are digits. Big
shapes (animation) may change freely meanwhile. Text that blinks, scrolls or
gets replaced by a whole new message doesn't pass these checks.

Shapes are compared lit/unlit only, so a dim score matches a bright one.
"""
from __future__ import annotations

import json
import os
from collections import Counter

from .frames import Frame

MAX_W, MAX_H = 12, 16        # bigger than this is never treated as a digit
MIN_PIXELS = 3
Shape = tuple                # (w, h, bytes of 0/1)


def components(px: bytes, W: int, H: int):
    """8-connected lit groups -> list of (shape, x, y, pixel indexes)."""
    seen = bytearray(W * H)
    out = []
    for s0 in range(W * H):
        if not px[s0] or seen[s0]:
            continue
        stack, pts = [s0], []
        seen[s0] = 1
        while stack:
            i = stack.pop()
            pts.append(i)
            x, y = i % W, i // W
            for dy in (-1, 0, 1):
                yy = y + dy
                if 0 <= yy < H:
                    for dx in (-1, 0, 1):
                        xx = x + dx
                        if 0 <= xx < W:
                            j = yy * W + xx
                            if px[j] and not seen[j]:
                                seen[j] = 1
                                stack.append(j)
        xs = [i % W for i in pts]
        ys = [i // W for i in pts]
        x0, y0 = min(xs), min(ys)
        w, h = max(xs) - x0 + 1, max(ys) - y0 + 1
        sh = bytearray(w * h)
        for i in pts:
            sh[(i // W - y0) * w + (i % W - x0)] = 1
        out.append(((w, h, bytes(sh)), x0, y0, pts))
    return out


def _small(shape: Shape) -> bool:
    w, h, sh = shape
    return w <= MAX_W and h <= MAX_H and sum(sh) >= MIN_PIXELS


def _digit_like(shape: Shape) -> bool:
    w, h, _ = shape
    return w >= 2 and h >= 4 and h >= w - 1     # no thin bars, no wide blocks


def learn(frames: list[Frame], min_count: int = 5, max_changed: int = 4,
          known: set | None = None) -> set:
    """Return the set of digit shapes (plus any `known` ones).

    A shape counts as a digit only if, besides being swapped in place often:
    - it was swapped at 2+ different spots on screen (a score has several
      digit positions; a one-off animation frame or a single sign doesn't),
    - it swapped with 2+ other candidate shapes of the same height, and
    - it belongs to a family of 5+ same-height shapes that swap with each
      other (a digit font has ten).
    Measured on real dumps: Harley-Davidson keeps its 20 digits (two fonts),
    Rescue 911's attract animations no longer turn into "digits"."""
    counts: Counter = Counter()
    partners: dict = {}
    spots: dict = {}
    prev = None
    for f in frames:
        cs = components(f.pixels, f.width, f.height)
        if prev is not None and prev[0] == (f.width, f.height):
            a_set = {(s, x, y) for s, x, y, _ in prev[1]}
            b_set = {(s, x, y) for s, x, y, _ in cs}
            gone = [k for k in a_set if k not in b_set and _small(k[0])]
            new = [k for k in b_set if k not in a_set and _small(k[0])]
            # Strict on purpose: EVERY small change must be a clean in-place
            # swap, or the pair teaches nothing. Looser versions learned letters.
            if 0 < len(new) == len(gone) <= max_changed:
                pairs = []
                for b in new:
                    m = [a for a in gone if a[0][1] == b[0][1] and a[2] == b[2]
                         and abs(a[1] - b[1]) <= 2]
                    if len(m) != 1:
                        pairs = None
                        break
                    pairs.append((m[0], b))
                for a, b in pairs or ():
                    if a[0] != b[0]:
                        for me, other, at in ((a[0], b[0], a), (b[0], a[0], b)):
                            counts[me] += 1
                            partners.setdefault(me, set()).add(other)
                            spots.setdefault(me, set()).add((at[1], at[2]))
        prev = ((f.width, f.height), cs)
    cand = {s for s, n in counts.items()
            if n >= min_count and _digit_like(s) and len(spots.get(s, ())) >= 2}
    while True:                                    # drop shapes with too few same-height partners
        keep = {s for s in cand if sum(1 for o in partners[s] if o in cand and o[1] == s[1]) >= 2}
        if keep == cand:
            break
        cand = keep
    out, seen = set(), set()
    for s in cand:                                 # families: same height, swapping together
        if s in seen:
            continue
        fam, stack = set(), [s]
        while stack:
            x = stack.pop()
            if x in fam:
                continue
            fam.add(x)
            stack += [o for o in partners[x] if o in cand and o[1] == x[1] and o not in fam]
        seen |= fam
        if len(fam) >= 5:
            out |= fam
    return out | (known or set())


def erase(f: Frame, digits: set) -> bytes:
    """Frame pixels with every learned digit blanked."""
    if not digits:
        return f.pixels
    buf = bytearray(f.pixels)
    for shape, _, _, pts in components(f.pixels, f.width, f.height):
        if shape in digits:
            for i in pts:
                buf[i] = 0
    return bytes(buf)


# --- files ---------------------------------------------------------------------

def save(digits: set, path: str):
    data = [{"w": w, "h": h, "rows": ["".join(".#"[v] for v in sh[r * w:(r + 1) * w])
                                        for r in range(h)]}
            for w, h, sh in sorted(digits, key=lambda s: (s[1], s[0], s[2]))]
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1)


def load(path: str) -> set:
    if not path or not os.path.exists(path):
        return set()
    return {(g["w"], g["h"], bytes(1 if c == "#" else 0 for row in g["rows"] for c in row))
            for g in json.load(open(path))}


def ordered(digits: set) -> list:
    """The learned shapes in a fixed order; the numbers on the sheet refer to it."""
    return sorted(digits, key=lambda s: (s[1], s[0], s[2]))


def sheet(digits: set, path: str) -> bool:
    """All learned digits side by side as a PNG, each with its number under
    it (for forget-digit), so a person can check them."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    if not digits:
        return False
    gl = ordered(digits)
    k = 4
    cw = max(max(w for w, _, _ in gl) + 2, 6) * k
    ch = (max(h for _, h, _ in gl) + 2) * k + 14
    per = 20
    img = Image.new("L", (per * cw, ((len(gl) + per - 1) // per) * ch), 30)
    d = ImageDraw.Draw(img)
    for i, (w, h, sh) in enumerate(gl):
        ox, oy = (i % per) * cw + k, (i // per) * ch + k
        for yy in range(h):
            for xx in range(w):
                if sh[yy * w + xx]:
                    d.rectangle([ox + xx * k, oy + yy * k, ox + xx * k + k - 1, oy + yy * k + k - 1], fill=255)
        d.text((ox, oy + h * k + 2), str(i + 1), fill=150)
    img.save(path)
    return True
