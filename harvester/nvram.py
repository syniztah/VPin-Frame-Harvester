"""Lower a game's high-score table in its PinMAME .nv file, so ordinary
auto-play scores beat it and every game ends in initials entry.

Uses the pinball-memory-maps (see games/nvram_maps/README.md) to find the
high scores, and recomputes any checksum covering the changed bytes. Before
changing anything it checks that its checksum formula reproduces the values
already stored in the file; if not, it refuses, so a file is never corrupted.
"""
from __future__ import annotations

import json
import shutil


def _num(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v)


def _bcd(value: int, length: int) -> bytes:
    digits = f"{value:0{length * 2}d}"[-length * 2:]
    return bytes(int(digits[i]) << 4 | int(digits[i + 1]) for i in range(0, len(digits), 2))


def _checksum_regions(m: dict):
    """Yield (data_start, data_end_exclusive, sum_addr, bits, complement)."""
    for c in m.get("_metadata", {}).get("validation", {}).get("checksum", []):
        bits = int(c.get("bits", 8))
        reps = c.get("repeat") or {"count": 1, "step": 0}
        for r in range(int(reps.get("count", 1))):
            start = _num(c["start"]) + r * int(reps.get("step", 0))
            end = _num(c["end"]) if "end" in c else start + _num(c["length"]) - 1
            end += 0 if "end" not in c or not reps.get("step") else r * int(reps["step"])
            nbytes = bits // 8
            if "checksum" in c:
                yield start, end + 1, _num(c["checksum"]), bits, bool(c.get("complement"))
            else:                              # checksum is the last byte(s) of the range
                yield start, end + 1 - nbytes, end + 1 - nbytes, bits, bool(c.get("complement"))


def _calc(buf, start, stop, bits, complement) -> int:
    mask = (1 << bits) - 1
    s = sum(buf[start:stop]) & mask
    return (mask - s) if complement else s


def _stored(buf, addr, bits) -> int:
    return int.from_bytes(buf[addr:addr + bits // 8], "big")


def lower_high_scores(nv_path: str, map_path: str, base: int = 1000, dry_run: bool = False) -> str:
    m = json.load(open(map_path))
    buf = bytearray(open(nv_path, "rb").read())
    entries = [h["score"] for h in m.get("high_scores", []) if "score" in h]
    if not entries:
        return "no high scores in this map"
    regions = list(_checksum_regions(m))
    changed = set()
    for e in entries:
        if e.get("encoding") != "bcd":
            return f"unsupported score encoding {e.get('encoding')} - not changed"
    # 1) safety: every checksum that will be affected must verify as-is
    from harvester.gamestate import addrs
    touched = {x for e in entries for x in addrs(e)}
    affected = [r for r in regions if any(r[0] <= x < r[1] for x in touched)]
    for start, stop, addr, bits, comp in affected:
        if _calc(buf, start, stop, bits, comp) != _stored(buf, addr, bits):
            return ("checksum check failed before changing anything "
                    f"(region 0x{start:X}-0x{stop - 1:X}) - file NOT changed")
    # 2) write small descending scores
    n = len(entries)
    for i, e in enumerate(entries):
        ad = addrs(e)
        for x, b in zip(ad, _bcd((n - i) * base, len(ad))):
            buf[x] = b
        changed.update(ad)
    # 3) fix checksums
    for start, stop, addr, bits, comp in affected:
        buf[addr:addr + bits // 8] = _calc(buf, start, stop, bits, comp).to_bytes(bits // 8, "big")
    if not dry_run:
        shutil.copy2(nv_path, nv_path + ".before-low-scores")
        open(nv_path, "wb").write(bytes(buf))
    return (f"set {n} high scores to {n * base:,} ... {base:,}"
            + (f", fixed {len(affected)} checksum(s)" if affected else "")
            + ("" if dry_run else f"; original kept as {nv_path}.before-low-scores"))


def set_bytes(nv_path: str, map_path: str, address: int, data: bytes) -> str:
    """Change bytes in a .nv file (e.g. an adjustment like free play) and
    fix any checksum covering them. Refuses if those checksums don't verify
    first. Returns a message; the caller keeps its own backup."""
    m = json.load(open(map_path))
    buf = bytearray(open(nv_path, "rb").read())
    if address + len(data) > len(buf):
        return "address outside the file - NOT changed"
    affected = [r for r in _checksum_regions(m) if address < r[1] and r[0] < address + len(data)]
    for start, stop, addr, bits, comp in affected:
        if _calc(buf, start, stop, bits, comp) != _stored(buf, addr, bits):
            return f"checksum check failed (region 0x{start:X}-0x{stop - 1:X}) - file NOT changed"
    buf[address:address + len(data)] = data
    for start, stop, addr, bits, comp in affected:
        buf[addr:addr + bits // 8] = _calc(buf, start, stop, bits, comp).to_bytes(bits // 8, "big")
    open(nv_path, "wb").write(bytes(buf))
    return f"set 0x{address:X} = {data.hex()}" + (f", fixed {len(affected)} checksum(s)" if affected else "")
