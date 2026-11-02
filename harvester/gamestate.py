"""Read the game's own idea of what's going on (player, ball, credits...)
from its memory while it runs, using the game_state section of a
pinball-memory-map. Used for the log, and so the auto-player knows when a
game has really ended instead of guessing."""
from __future__ import annotations

import json


def _num(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v)


def _decode(raw: bytes, enc: str) -> int:
    if enc == "bcd":
        n = 0
        for b in raw:
            n = n * 100 + (b >> 4) * 10 + (b & 15)
        return n
    return int.from_bytes(raw, "big")


def addrs(d: dict) -> list[int]:
    """The byte addresses of a map entry: "start" + "length", or a list of
    "offsets" (some boards keep a score's bytes in separate places)."""
    if "offsets" in d:
        return [_num(o) for o in d["offsets"]]
    a = _num(d["start"])
    return list(range(a, a + int(d.get("length", 1))))


def _has(d: dict) -> bool:
    return isinstance(d, dict) and ("start" in d or "offsets" in d)


def _read(pm, d: dict) -> bytes | None:
    a = addrs(d)
    if a == list(range(a[0], a[0] + len(a))):
        return pm.read_memory(a[0], len(a))
    out = b""
    for x in a:
        r = pm.read_memory(x, 1)
        if r is None:
            return None
        out += r
    return out


def _write(pm, d: dict, data: bytes) -> bool:
    a = addrs(d)
    if a == list(range(a[0], a[0] + len(a))):
        return pm.write_memory(a[0], data)
    return all(pm.write_memory(x, data[i:i + 1]) for i, x in enumerate(a))


class GameState:
    FIELDS = ("current_player", "current_ball", "player_count", "ball_count", "credits")

    def __init__(self, map_path: str):
        m = json.load(open(map_path))
        gs = m.get("game_state", {})
        self.fields = {k: gs[k] for k in self.FIELDS if k in gs and _has(gs[k])}
        self.scores = [s for s in gs.get("scores", []) if _has(s)]
        self.table = [h for h in m.get("high_scores", []) if _has(h.get("score", {}))]
        self._sums = m.get("_metadata", {}).get("validation", {}).get("checksum", [])

    def has_checksum_over_scores(self) -> bool:
        from harvester.nvram import _checksum_regions
        regions = list(_checksum_regions({"_metadata": {"validation": {"checksum": self._sums}}}))
        for h in self.table:
            if any(start <= x < stop for x in addrs(h["score"]) for start, stop, *_ in regions):
                return True
        return False

    def lower_high_scores(self, pm, base: int = 1000) -> bool:
        """Set the table in live memory to n*base ... base, so any real score
        beats it. Only for maps without a checksum over the high scores."""
        n, ok = len(self.table), True
        for i, h in enumerate(self.table):
            d = h["score"]
            if d.get("encoding", "bcd") != "bcd":
                return False
            L = len(addrs(d))
            digits = f"{(n - i) * base:0{L * 2}d}"[-L * 2:]
            data = bytes(int(digits[k]) << 4 | int(digits[k + 1]) for k in range(0, L * 2, 2))
            ok &= _write(pm, d, data)
        return ok and n > 0

    def set_credits(self, pm, n: int) -> bool:
        d = self.fields.get("credits")
        if not d:
            return False
        L = len(addrs(d))
        if d.get("encoding", "int") == "bcd":
            digits = f"{n:0{L * 2}d}"
            data = bytes(int(digits[k]) << 4 | int(digits[k + 1]) for k in range(0, L * 2, 2))
        else:
            data = n.to_bytes(L, "big")
        return _write(pm, d, data)

    def table_full(self, pm, low: int = 10000) -> bool:
        """True when every table entry is above `low` (factory reset, or filled
        with real scores), so auto-play scores may no longer get in."""
        vals = []
        for h in self.table:
            d = h["score"]
            raw = _read(pm, d)
            if raw is None:
                return False
            vals.append(_decode(raw, d.get("encoding", "bcd")))
        return bool(vals) and min(vals) > low

    def high_scores(self, pm) -> str:
        """The high-score table as the running game sees it, e.g. '#1 5,000  #2 4,000'."""
        out = []
        for h in self.table:
            d = h["score"]
            raw = _read(pm, d)
            if raw is not None:
                out.append(f"{h.get('short_label', h.get('label', '?'))} {_decode(raw, d.get('encoding', 'bcd')):,}")
        return "  ".join(out) or "?"

    def read(self, pm) -> dict:
        out = {}
        for k, d in self.fields.items():
            raw = _read(pm, d)
            if raw is not None:
                out[k] = _decode(raw, d.get("encoding", "int"))
        sc = []
        for s in self.scores:
            raw = _read(pm, s)
            if raw is not None:
                sc.append(_decode(raw, s.get("encoding", "bcd")))
        out["scores"] = sc
        out["game_over"] = out.get("current_player", 1) == 0
        return out

    @staticmethod
    def describe(st: dict) -> str:
        if not st:
            return "unknown"
        if st.get("game_over"):
            head = "GAME OVER (attract)"
        else:
            head = (f"player {st.get('current_player', '?')}/{st.get('player_count', '?')}, "
                    f"ball {st.get('current_ball', '?')}/{st.get('ball_count', '?')}")
        sc = [f"{s:,}" for s in st.get("scores", [])[:max(1, st.get('player_count') or 1)]]
        return f"{head}, credits {st.get('credits', '?')}, scores {' / '.join(sc)}"
