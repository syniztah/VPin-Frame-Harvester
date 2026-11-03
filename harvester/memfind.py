"""Find a table's memory map by itself.

During a run with --find-map the harvester takes snapshots of the game's
battery-backed memory (what PinMAME saves as the .nv file) at moments it
knows something about: just before and after each coin, right after Start,
when each ball is served or drains, and every few seconds in attract mode
and during play.

Afterwards `find_map` looks for the bytes that behaved the way a credits
counter, a ball number, a player number, a player's score and a high-score
table must behave at those moments, and writes them as a
pinball-memory-map (the same format as Tom Collins' maps), marked as found
by the harvester.

Positions are .nv file positions, like the real maps. "_harvester.cpu_offset"
records where that memory sits in the CPU's own memory, which is what the
harvester reads and writes while a game runs.
"""
from __future__ import annotations

import glob
import json
import os

import numpy as np

PERIOD_MS = 4000


# ---------------------------------------------------------------- recording
class Recorder:
    def __init__(self, pm, out_dir: str, rom: str):
        self.pm, self.rom = pm, rom
        self.path = os.path.join(out_dir, "memsnap", f"{rom}_{int(__import__('time').time())}.npz")
        self.snaps: list[np.ndarray] = []
        self.meta: list[dict] = []
        self.pending: list[tuple[int, str, dict]] = []
        self.next_periodic = 0
        self.cpu_offset = None
        self._located = False
        self.nv_len = 0
        self.extra: list[tuple[int, int]] = []     # (cpu address, size) of extra memory appended
        self.source = None
        self.failed = False

    def add_events(self, events):
        self.pending.extend(events)

    def tick(self, now: int, phase: str, game_on):
        if self.failed:
            return
        due = [e for e in self.pending if e[0] <= now]
        if due:
            self.pending = [e for e in self.pending if e[0] > now]
            for t, kind, info in sorted(due, key=lambda e: e[0]):
                self.snap(now, kind, phase, game_on, info)
        if now >= self.next_periodic:
            self.next_periodic = now + PERIOD_MS
            self.snap(now, "periodic", phase, game_on, {})

    def snap(self, now, kind, phase, game_on, info):
        nv = self.pm.read_nvram()
        if nv is None:
            if not self.snaps:
                self.failed = True        # this libpinmame can't hand out the memory
            return
        if not self._located:
            self._located = True
            self.cpu_offset = locate(nv, self.pm)
            self.nv_len = len(nv)
            if self.cpu_offset == SAM_NVRAM:
                # Stern SAM keeps ball/player/game-over in working memory, not in the NVRAM:
                # take that part of memory too
                self.extra = [(SAM_RAM[0], SAM_RAM[1])]
        parts = [nv]
        for base, size in self.extra:
            got = self.pm.cpu_region(base, size)
            parts.append(got if got is not None else bytes(size))
        a = np.frombuffer(b"".join(parts), dtype=np.uint8).copy()
        if self.snaps and len(a) != len(self.snaps[0]):
            return
        self.snaps.append(a)
        self.meta.append(dict(t=now, kind=kind, phase=phase,
                              game_on=None if game_on is None else bool(game_on), **info))

    def save(self) -> str | None:
        if not self.snaps:
            return None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        np.savez_compressed(self.path, snaps=np.stack(self.snaps),
                            meta=np.array(json.dumps(dict(rom=self.rom, events=self.meta,
                                                          cpu_offset=self.cpu_offset, nv_len=self.nv_len,
                                                          extra=self.extra))))
        return self.path


SAM_NVRAM = 0x02100000                 # Stern SAM: the NVRAM's place in the CPU's memory
SAM_RAM = (0x00030000, 0x00010000)     # ... and where its games keep player/ball/game over
KNOWN_BASES = (0, SAM_NVRAM)           # most boards: .nv = memory from 0


def locate(nv: bytes, pm) -> int | None:
    """Where the .nv bytes sit in the CPU's memory: cpu position = nv position + offset."""
    # first the usual places, comparing a few distinctive pieces directly
    pieces = [o for o in range(0, max(1, len(nv) - 64), max(64, len(nv) // 64))
              if len(set(nv[o:o + 64])) >= 8][:12]
    for base in KNOWN_BASES:
        if not pieces:
            break
        same = total = 0
        for o in pieces:
            got = pm.cpu_region(base + o, 64)
            if got is None:
                total = 0
                break
            same += sum(a == b for a, b in zip(got, nv[o:o + 64]))
            total += 64
        if total and same / total > 0.9:
            return base
    cpu = pm.cpu_region()
    if not cpu:
        return None
    found = []
    step = max(64, len(nv) // 40)
    for o in range(0, max(1, len(nv) - 64), step):
        w = nv[o:o + 64]
        if len(set(w)) < 12:                  # needs a distinctive piece
            continue
        p = cpu.find(w)
        if p != -1 and cpu.find(w, p + 1) == -1:
            found.append(p - o)
        if len(found) >= 5:
            break
    if not found:
        return None
    best = max(set(found), key=found.count)
    return best if found.count(best) >= max(2, len(found) // 2) else None


# ---------------------------------------------------------------- loading
class Run:
    def __init__(self, path: str):
        z = np.load(path, allow_pickle=False)
        self.S = z["snaps"]
        m = json.loads(str(z["meta"]))
        self.rom, self.ev, self.cpu_offset = m["rom"], m["events"], m.get("cpu_offset")
        self.nv_len = m.get("nv_len") or self.S.shape[1]
        self.extra = [tuple(x) for x in m.get("extra", [])]
        # which game each snapshot belongs to
        self.game = [None] * len(self.ev)
        self.games: list[dict] = []
        g = None
        for i, e in enumerate(self.ev):
            if e["kind"] == "before_coins":
                g = None
            if e["kind"] == "started":
                g = len(self.games)
                self.games.append(dict(players=e["players"], start=i))
                # the first ball can come out before the "started" snapshot: everything after
                # the last coin belongs to this game
                k = i - 1
                while k >= 0 and self.ev[k]["kind"] not in ("coin", "before_coins") and self.game[k] is None:
                    self.game[k] = g
                    k -= 1
            self.game[i] = g

    def cpu_address(self, pos: int) -> int | None:
        """Snapshot position -> the address the harvester reads while a game runs."""
        if pos < self.nv_len:
            return None if self.cpu_offset is None else pos + self.cpu_offset
        at = self.nv_len
        for base, size in self.extra:
            if pos < at + size:
                return base + pos - at
            at += size
        return None

    def idx(self, kind=None, game=None):
        return [i for i, e in enumerate(self.ev)
                if (kind is None or e["kind"] == kind) and (game is None or self.game[i] == game)]

    def attract(self):
        """Periodic snapshots well away from any game."""
        return [i for i, e in enumerate(self.ev) if e["kind"] == "periodic" and self.game[i] is None
                and e.get("game_on") is not True and e["phase"] in ("boot", "start")]


def load_runs(sources: list[str], rom: str | None = None) -> list[Run]:
    files = []
    for s in sources:
        if os.path.isdir(s):
            files += sorted(glob.glob(os.path.join(s, "memsnap", "*.npz")))
        elif s.endswith(".npz"):
            files.append(s)
    runs = [Run(f) for f in files]
    if rom:
        runs = [r for r in runs if r.rom == rom]
    n = {r.S.shape[1] for r in runs}
    if len(n) > 1:                            # different memory sizes: keep the most common
        size = max(n, key=lambda k: sum(r.S.shape[1] == k for r in runs))
        runs = [r for r in runs if r.S.shape[1] == size]
    return runs


# ---------------------------------------------------------------- finding
def _bcd_ok(a: np.ndarray) -> np.ndarray:
    return ((a >> 4) <= 9) & ((a & 15) <= 9)


def _bcd1(a: np.ndarray) -> np.ndarray:
    return (a >> 4).astype(np.int64) * 10 + (a & 15)


def _balls(r: "Run"):
    """The serves of a run as (index, drained_since_previous_serve, steady_snapshots): the
    snapshots taken while that ball was in play (between the serve and the next drain)."""
    out = []
    drained, new, cur, steady = True, True, None, []
    for i, e in enumerate(r.ev):
        k = e["kind"]
        if k == "serve":
            if cur is not None:
                out.append((cur[0], cur[1], cur[2], steady))
            cur, steady = (i, drained, new), []
            drained = new = False
        elif k == "drain":
            drained = True
            if cur is not None:
                out.append((cur[0], cur[1], cur[2], steady))
                cur, steady = None, []
        elif k == "periodic" and cur is not None and e["phase"] == "play":
            steady.append(i)
        elif k in ("before_coins", "started"):
            drained = new = True                 # (maybe) a new game
    if cur is not None:
        out.append((cur[0], cur[1], cur[2], steady))
    return out


def _turn_counter(runs, top: int, wrap: bool):
    """Bytes that count up by one between balls (never while a ball is in play), starting from
    0/1 in a new game. wrap=True (player number): may also go back to 0/1 between balls.
    Returns (increments, wraps, violations) per position. Doesn't need exact game boundaries -
    the harvester may have pressed Start in the middle of a game."""
    N = runs[0].S.shape[1]
    inc = np.zeros(N)
    wr = np.zeros(N)
    bad = np.zeros(N)
    prev = None                     # carried over between runs: a new run is a new game
    for r in runs:
        for i, drained, new, steady in _balls(r):
            v = r.S[i].astype(np.int64)
            bad += v > top
            for j in steady:                     # no change while the ball is in play
                bad += r.S[j] != r.S[i]
            if prev is not None:
                d = v - prev
                if drained:
                    inc += d == 1
                    back = (v <= 1) & (prev > v)
                    if wrap or new:
                        wr += back          # player: back to 1 for the next round; ball: new game
                    ok = (d == 0) | (d == 1) | ((v <= 1) & (new or wrap))
                    bad += ~ok
                else:                            # multiball / ball save: same ball, same player
                    bad += d != 0
            prev = v
    return inc, wr, bad


def _clear_winner(t: np.ndarray, margin: float) -> bool:
    """The best position beats the runner-up by enough to trust it."""
    top2 = np.partition(t, -2)[-2:]
    return top2[1] - top2[0] >= margin


def find_ball(runs) -> tuple[int | None, str]:
    inc, resets, bad = _turn_counter(runs, top=9, wrap=False)
    if not any(len(_balls(r)) > 1 for r in runs):
        return None, "no game got past its first ball"
    # the ball number goes back to 1 in a new game; counters of balls played (audits) don't
    news = sum(1 for r in runs for b in _balls(r) if b[2]) - 1
    total = inc + 2 * resets - 3 * bad
    if news >= 1:
        total[resets < 1] -= 100
    best = int(np.argmax(total))
    if not _clear_winner(total, 1.0) and inc[best] < 6:
        return None, ("not sure yet - several spots behave like it (more games will tell them apart)")
    if inc[best] < 2 or bad[best] > inc[best] / 4 or total[best] < 0:
        return None, "nothing counted the balls (play longer, so games reach ball 2 and 3)"
    tied = int((total == total[best]).sum())
    return best, f"position {best:#06x}" + (f" (and {tied - 1} other possible spots)" if tied > 1 else "")


def find_player(runs, exclude=(), ball=None) -> tuple[int | None, int | None, str]:
    """(current_player position, player_count position, note)."""
    N = runs[0].S.shape[1]
    inc, wraps, bad = _turn_counter(runs, top=4, wrap=True)
    reach = np.zeros(N)
    for r in runs:
        for gi, g in enumerate(r.games):
            serves = r.idx("serve", gi)
            if len(serves) < 2:
                continue
            V = r.S[serves].astype(np.int64)
            if g["players"] == 1:
                bad += (V != V[0]).any(0)        # one player: the player number never changes
            else:
                reach += (V.max(0) >= 2) & (V.max(0) <= g["players"])
    inc = np.where(reach >= 1, inc, 0)
    for x in exclude:
        if x is not None:
            inc[x] = -1e9
    cur, cnt, notes = None, None, []
    t = inc + wraps - 3 * bad
    i = int(np.argmax(t))
    multi = any(g["players"] >= 2 for r in runs for g in r.games)
    nb = ball - 1 if ball else None
    if nb is not None and inc[nb] >= 2 and wraps[nb] >= 1 and bad[nb] <= inc[nb] / 4:
        # every board checked so far keeps the player number right before the ball number
        # (Whitestar, SAM, Data East, WPC); other copies of it exist, this one is the real one
        cur = nb
        notes.append(f"player number at {cur:#06x} (right before the ball number)")
    elif inc[i] >= 2 and wraps[i] >= 1 and bad[i] <= inc[i] / 4 and _clear_winner(t, 1.0):
        cur = i
        notes.append(f"player number at {cur:#06x}")
    else:
        notes.append("player number: not found yet" + ("" if multi else " (do a run with Players set to Mix)"))
    # number of players: steady through each game at the highest player number reached
    if cur is not None:
        ok = np.zeros(N)
        fail = np.zeros(N)
        for r in runs:
            balls = _balls(r)
            top = max(int(r.S[b[0], cur]) for b in balls) if balls else 0
            if top < 2:
                continue
            for gi in range(len(r.games)):
                dr = r.idx("drain", gi)
                serves = r.idx("serve", gi)
                if not dr or not serves:
                    continue
                hi = max(int(r.S[k, cur]) for k in serves)
                if len(serves) < 3:
                    continue
                hi = max(hi, 1)          # 1-player games count too: a fixed setting that happens to read 2 fails there
                W = r.S[dr[len(dr) // 2:]].astype(np.int64)     # later in the game: everyone's in
                good = (W == hi).all(0)
                ok += good
                fail += ~good
        if ok.max() >= 1:
            ok[cur] = -1
            j = int(np.argmax(ok - 3 * fail))
            if ok[j] >= 1 and fail[j] == 0:
                cnt = j
                notes.append(f"number of players at {cnt:#06x}")
    return cur, cnt, "; ".join(notes)


def find_credits(runs, exclude=()) -> tuple[int | None, str | None, str]:
    N = runs[0].S.shape[1]
    best = (None, None, -1e9, 0)
    for enc in ("int", "bcd"):
        ok_n = np.zeros(N)
        fail_n = np.zeros(N)
        for r in runs:
            ev = r.ev
            for b in r.idx("before_coins"):
                seq = [b]
                j = b + 1
                while j < len(ev) and ev[j]["kind"] in ("coin", "periodic"):
                    if ev[j]["kind"] == "coin":
                        seq.append(j)
                    j += 1
                # the first snapshot after Start: "started", or the first ball if it came out sooner
                if len(seq) < 3 or j >= len(ev) or ev[j]["kind"] not in ("started", "serve"):
                    continue
                st = j
                later = [k for k in r.idx("serve") if k > st][:1]   # credits are surely taken by the first ball
                if later and r.game[later[0]] == r.game[st]:
                    st = later[0]
                raw = r.S[seq + [st]]
                valid = _bcd_ok(raw).all(0) if enc == "bcd" else np.ones(N, bool)
                V = _bcd1(raw) if enc == "bcd" else raw.astype(np.int64)
                C = V[:-1]
                d = np.diff(C, axis=0)
                served = len(r.idx("serve", r.game[st])) > 0
                # one coin may buy part of a credit, or several (Sharkey's: 2); the count stops at
                # the game's maximum
                steady = valid & (d >= 0).all(0) & (d <= 4).all(0)
                rose = (C[-1] - C[0]) >= 1
                ok = steady & rose
                if served:
                    # Start used a few credits up (one game's worth per player), not all of them
                    gi = r.game[st]
                    p = r.games[gi]["players"] if gi is not None else 1
                    ok &= (V[-1] < C[-1]) & (V[-1] >= C[-1] - 4 * p)
                # a burst where the credits didn't rise (already at the game's maximum) tells
                # nothing either way; only a burst that broke the pattern counts against
                bad = ~steady | (rose & ~ok)
                ok_n += ok
                fail_n += bad
        for x in exclude:
            if x is not None:
                ok_n[x] = -1e9
        # tie-break: credits only change when coins go in or a game starts (copies for the display
        # and coin audits change at other times too)
        odd = np.zeros(N)
        for r in runs:
            first_drain = {}
            for i, e in enumerate(r.ev):
                if e["kind"] == "drain" and r.game[i] is not None:
                    first_drain.setdefault(r.game[i], i)
            for i in range(1, len(r.ev)):
                e, gi = r.ev[i], r.game[i]
                if e["kind"] == "coin" or (gi is not None and i <= first_drain.get(gi, 10**9)):
                    continue
                odd += r.S[i] != r.S[i - 1]
        t = ok_n - 3 * fail_n - 0.02 * odd
        nv_len = runs[0].nv_len
        t[:nv_len] += 0.01            # credits survive power-off: prefer the battery-backed copy
        i = int(np.argmax(t))
        unique = int((t == t[i]).sum()) == 1
        if (t[i] > best[2] or (t[i] == best[2] and enc == "int")) and unique:
            best = (i, enc, t[i], ok_n[i])
    pos, enc, t, n = best
    if pos is None or n < 1:
        return None, None, "credits: not found yet (needs a few more coin drops)"
    return pos, enc, f"credits at {pos:#06x} ({'BCD' if enc == 'bcd' else 'plain number'})"


def _bcd_values(S: np.ndarray, L: int):
    """For every position: is the L-byte BCD number valid, and its value (float)."""
    n, N = S.shape
    valid = np.ones((n, N - L + 1), bool)
    val = np.zeros((n, N - L + 1))
    for k in range(L):
        a = S[:, k:N - L + 1 + k]
        valid &= _bcd_ok(a)
        val = val * 100 + _bcd1(a)
    return valid, val


def _values(S: np.ndarray, L: int, enc: str):
    """For every position: is it a valid number of this kind, and its value (float).
    enc: "bcd", "int>" (big-endian) or "int<" (little-endian, Stern SAM's ARM)."""
    if enc == "bcd":
        return _bcd_values(S, L)
    n, N = S.shape
    val = np.zeros((n, N - L + 1))
    order = range(L) if enc == "int>" else range(L - 1, -1, -1)
    for k in order:
        val = val * 256 + S[:, k:N - L + 1 + k]
    return np.ones_like(val, dtype=bool), val


SCORE_KINDS = [(5, "bcd", 5), (4, "bcd", 4), (6, "bcd", 6), (4, "int<", 8), (4, "int>", 8)]


def find_score(runs, exclude=(), cur=None) -> tuple[int | None, int, str, str]:
    """(position, length, encoding, note). Scores are BCD on most boards, 4-byte numbers on SAM."""
    best = (None, 0, "bcd", -1e9)
    for L, enc, stride in SCORE_KINDS:
        tot = None
        nxt = None          # does the next slot behave like player 2's score? (real score tables do)
        for r in runs:
            for gi, g in enumerate(r.games):
                serves = r.idx("serve", gi)
                if not serves:
                    continue
                # from the first ball on (some games clear the scores a moment after Start)
                ii = [i for i in r.idx(None, gi) if i >= serves[0] and
                      r.ev[i]["kind"] in ("serve", "drain", "periodic")]
                if len(ii) < 4:
                    continue
                valid, val = _values(r.S[ii], L, enc)
                d = np.diff(val, axis=0)
                ok = valid.all(0) & (d >= 0).all(0) & (val[0] == 0) & (val[-1] >= 100) \
                    & (val[-1] < 1e12) & (np.mod(val[-1], 10) == 0)
                if cur is not None and g["players"] >= 2:
                    # player 1's score only changes while it's player 1's turn (a display copy of
                    # "the current player's score" changes on everyone's turn)
                    first = r.S[ii[0], cur]
                    # both snapshots in someone else's turn (bonus is added as the turn passes)
                    others = np.array([r.S[i, cur] != first and r.S[j, cur] != first
                                       for j, i in zip(ii, ii[1:])])
                    if others.any():
                        ok &= ~(d[others] != 0).any(0)
                tot = ok.astype(float) if tot is None else tot + ok
                # player 2's slot right after: grows in 2+ player games, stays 0 in 1-player games
                p2v = np.zeros_like(ok, dtype=bool)
                p2v[:-stride] = (valid.all(0) & (d >= 0).all(0) & (val[0] == 0))[stride:]
                if g["players"] >= 2:
                    p2v[:-stride] &= (val[-1] >= 100)[stride:]
                else:
                    p2v[:-stride] &= (val.max(0) == 0)[stride:]
                nxt = p2v.astype(float) if nxt is None else nxt + p2v
        if tot is None:
            continue
        tot = tot + 0.3 * nxt
        # the real score is still there after game over (shown in attract); working copies are cleared
        for r in runs:
            for gi in range(len(r.games)):
                after = [i for i, e in enumerate(r.ev) if r.game[i] is None and i > r.games[gi]["start"]
                         and e["kind"] == "periodic"][:3]
                last = [i for i in r.idx(None, gi)][-1:]
                if after and last:
                    v_end = _values(r.S[last], L, enc)[1][0]
                    v_att = _values(r.S[after], L, enc)[1]
                    tot = tot + 0.4 * ((v_att == v_end).all(0) & (v_end > 0))
        for x in exclude:
            if x is not None and x < len(tot):
                tot[x] = -1
        i = int(np.argmax(tot))
        if tot[i] > best[3] + 0.5 or (tot[i] >= best[3] and best[0] is None):
            best = (i, L, enc, tot[i])
    pos, L, enc, n = best
    if pos is None or n < 1:
        return None, 0, "bcd", "player 1 score: not found yet"
    kind = "BCD" if enc == "bcd" else ("number, low byte first" if enc == "int<" else "number")
    return pos, L, enc, f"player 1 score at {pos:#06x} ({L} bytes, {kind})"


def find_high_scores(runs) -> tuple[list[tuple[int, int]], str]:
    """A run of 4+ BCD numbers, evenly spaced, getting smaller, that don't change in attract mode.
    Looks at the start of a run, before the harvester lowers the table (with a map, it does)."""
    r = runs[0]
    early = [i for i, e in enumerate(r.ev) if e["t"] < 10000] or [0]
    S = r.S[early]
    snap = S[0]
    stable = (S == snap).all(0)
    best = ([], 0.0)
    groups = []                                  # every table-like run: (first, last byte)
    for L in (5, 4, 6):
        valid, val = _bcd_values(snap[None, :], L)
        valid, val = valid[0], val[0]
        N = len(val)
        for stride in range(L, L + 24):
            for p in range(0, N - 3 * stride):
                if not valid[p] or val[p] < 1000 or val[p] % 10 or not stable[p:p + L].all():
                    continue
                k, q, last = 1, p + stride, val[p]
                # each entry a bit lower than the one before (not a sudden drop into other data)
                while q < N and valid[q] and max(1000, last / 20) <= val[q] < last and val[q] % 10 == 0 \
                        and stable[q:q + L].all():
                    last = val[q]
                    k += 1
                    q += stride
                if k >= 4:
                    # real tables are round numbers (2,400,000,000): the right alignment has the
                    # most trailing zeros; a shifted one reads into its neighbours
                    vals = [int(val[p + i * stride]) for i in range(k)]
                    zl = [len(str(v)) - len(str(v).rstrip("0")) for v in vals]
                    zeros = np.mean(zl)
                    score = k * 10 + 3 * zeros
                    groups.append((p, p + (k - 1) * stride + L, k, val[p], min(zl)))
                    if score > best[1]:
                        best = ([(p + i * stride, L) for i in range(k)], score)
                        best_zeros = min(zl)
    if not best[0]:
        return [], "high scores: not found"
    p, L = best[0][0]
    lo, hi = p, best[0][-1][0] + best[0][-1][1]
    # another table as convincing as this one, somewhere else (scores in the millions, as long)
    # (Sharkey's also keeps its "reset to" values as a shorter look-alike in its settings)
    others = [g for g in groups if (g[1] <= lo or g[0] >= hi) and g[3] >= 1e6 and g[2] >= len(best[0])
              and g[4] >= best_zeros]          # ... every entry as round (not reading into other data)
    if others:
        # two separate tables (Theatre of Magic keeps a second, look-alike one): lowering the wrong
        # one would do nothing at best, so leave it to a checked map
        return [], (f"high scores: {1 + len({g[0] // 64 for g in others})} look-alike tables found "
                    f"(e.g. at {lo:#06x} and {others[0][0]:#06x}) - left out to be safe")
    return best[0], f"high-score table: {len(best[0])} entries from {p:#06x} ({L} bytes each, BCD)"


# Stern SAM: every checked SAM map (23 games) keeps these in the same NVRAM places
SAM_CREDITS = 0x1100C            # .nv position (0x0211100C live)
SAM_PLAYER_COUNT = 0x10900       # (0x02110900 live)
SAM_SCORE_SPOTS = (0x109E4, 0x109F0, 0x10A0C, 0x10A24, 0x10A9C)   # player 1's score, 4-byte number


def _sam_known(runs) -> tuple[dict, list[str]]:
    """Use what's the same on all SAM games, after checking the snapshots agree."""
    r0 = runs[0]
    out, rep = {}, []
    # credits: rises with coins
    rose = sum(int(r.S[r.idx("coin")[-1], SAM_CREDITS]) > int(r.S[b, SAM_CREDITS])
               for r in runs for b in r.idx("before_coins")[:1] if r.idx("coin"))
    if rose:
        out["credits"] = SAM_CREDITS
        rep.append(f"credits at {SAM_CREDITS:#06x} (where every SAM game keeps them - checked)")
    # player count: 1..4 during games
    vals = {int(r.S[i, SAM_PLAYER_COUNT]) for r in runs for i in r.idx("drain")}
    if vals and vals <= {1, 2, 3, 4}:
        out["player_count"] = SAM_PLAYER_COUNT
        rep.append(f"number of players at {SAM_PLAYER_COUNT:#06x} (the usual SAM place - checked)")
    # score: whichever usual spot grows like a score
    best, best_n = None, 0
    for p in SAM_SCORE_SPOTS:
        n = 0
        for r in runs:
            serves = r.idx("serve")
            if not serves:
                continue
            v = [int.from_bytes(r.S[i, p:p + 4].tobytes(), "little") for i in range(serves[0], len(r.ev))]
            n += sum(1 for a, b in zip(v, v[1:]) if b > a and b % 10 == 0)
        if n > best_n:
            best, best_n = p, n
    if best is not None and best_n >= 5:
        out["score"] = best
        rep.append(f"player 1 score at {best:#06x} (4-byte number, a usual SAM place - checked)")
    return out, rep


def r0_len(runs) -> int:
    return runs[0].nv_len


def find_map(runs) -> tuple[dict, list[str]]:
    rom = runs[0].rom
    rep = [f"Looked at {sum(len(r.ev) for r in runs)} memory snapshots from {len(runs)} run(s), "
           f"{sum(len(r.games) for r in runs)} game start(s)."]
    ball, note = find_ball(runs)
    rep.append("ball number: " + note)
    cur, cnt, note = find_player(runs, exclude=(ball,), ball=ball)
    rep.append(note)
    cred, cenc, note = find_credits(runs, exclude=(ball, cur, cnt))
    rep.append(note)
    sc, L, senc, note = find_score(runs, exclude=(ball, cur, cnt, cred), cur=cur)
    rep.append(note)
    sam = runs[0].cpu_offset == SAM_NVRAM
    if sam:
        known, krep = _sam_known(runs)
        rep.append("Stern SAM, using what all SAM games share:")
        rep += ["  " + x for x in krep]
        cred = known.get("credits")
        cenc = "int" if cred is not None else cenc
        cnt = known.get("player_count", cnt)
        if "score" in known:
            sc, L, senc = known["score"], 4, "int<"
        elif sc is not None and senc == "bcd":
            sc = None                       # SAM scores are numbers, not BCD
        # SAM keeps the ball number right after the player number (all checked maps)
        if cur is not None and cur >= r0_len(runs):
            ball = cur + 1
            rep.append(f"ball number at {ball:#06x} (right after the player number, as on every SAM game)")
        elif ball is not None:
            ball = None
            rep.append("ball number: left out until the player number is found (needs Mix runs)")
        hs = []
        rep.append("high scores: Stern SAM games check their high-score table, so it can't be lowered "
                   "from memory - left out")
    else:
        hs, note = find_high_scores(runs)
        rep.append(note)

    # Positions become the addresses the harvester reads while a game runs (like the SAM maps).
    r0 = runs[0]
    known = r0.cpu_offset is not None

    def at(pos):
        a = r0.cpu_address(pos)
        return f"{(a if a is not None else pos):#06x}"

    def num(enc):
        return {"encoding": "int", "endian": "little"} if enc == "int<" else \
               {"encoding": "int"} if enc == "int>" else {"encoding": enc}

    gs: dict = {}
    if ball is not None:
        gs["current_ball"] = {"label": "Ball", "start": at(ball), "encoding": "int"}
    if cur is not None:
        gs["current_player"] = {"label": "Player", "start": at(cur), "encoding": "int"}
    if cnt is not None:
        gs["player_count"] = {"label": "Player Count", "start": at(cnt), "encoding": "int"}
    if cred is not None:
        gs["credits"] = {"label": "Credits", "start": at(cred), "encoding": cenc}
    if sc is not None:
        gs["scores"] = [{"label": "Player 1", "start": at(sc), "length": L, **num(senc)}]
    labels = ["Grand Champion", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh"]
    hst = [{"label": labels[i] if i < len(labels) else f"#{i + 1}", "short_label": f"#{i + 1}",
            "score": {"start": at(p), "length": n, "encoding": "bcd"}}
           for i, (p, n) in enumerate(hs)]
    if not known:
        rep.append("! Couldn't work out where this memory sits while the game runs, so the map can't be "
                   "used yet (positions are .nv file positions).")
    m = {
        "_notes": "Found automatically by VPin Frame Harvester from memory snapshots - not checked by a "
                  "person. Positions are the game's live memory addresses.",
        "_fileformat": 0.6,
        "_metadata": {"roms": [rom], "found_by": "VPin Frame Harvester",
                      "platform": "stern-sam" if sam else None},
        # Stern SAM: tested against Family Guy's checked map and it didn't hold up yet, so a SAM map
        # is only written for reference, not used to read or change a running game
        "_harvester": {"live_addresses": known, "unverified": True},
        "game_state": gs,
        "high_scores": hst,
    }
    return m, rep


def compare(found: dict, real: dict) -> list[str]:
    """How a found map lines up with a real one (to check the finder on mapped tables)."""
    from harvester.gamestate import addrs
    out = []
    for k in ("current_ball", "current_player", "player_count", "credits"):
        f = found.get("game_state", {}).get(k)
        r = real.get("game_state", {}).get(k)
        if not r or not isinstance(r, dict) or ("start" not in r and "offsets" not in r):
            out.append(f"  {k}: (not in the real map)" + (f", found {f['start']}" if f else ""))
            continue
        ra = addrs(r)
        if not f:
            out.append(f"  {k}: MISSED (real: {ra[0]:#06x})")
        else:
            fa = addrs(f)[0]
            out.append(f"  {k}: {'RIGHT' if fa in ra else 'WRONG'} (found {fa:#06x}, real {ra[0]:#06x})")
    fs = found.get("game_state", {}).get("scores", [])
    rs = [s for s in real.get("game_state", {}).get("scores", []) if isinstance(s, dict) and "start" in s]
    if rs:
        if fs:
            fa, ra = addrs(fs[0]), addrs(rs[0])
            out.append(f"  player 1 score: {'RIGHT' if fa[-1] == ra[-1] else 'WRONG'} "
                       f"(found {fa[0]:#06x}+{len(fa)}, real {ra[0]:#06x}+{len(ra)})")
        else:
            out.append("  player 1 score: MISSED")
    rh = [h["score"] for h in real.get("high_scores", []) if "start" in h.get("score", {})]
    fh = [h["score"] for h in found.get("high_scores", [])]
    if rh:
        real_ends = {addrs(h)[-1] for h in rh}
        hit = sum(addrs(h)[-1] in real_ends for h in fh)
        out.append(f"  high scores: {hit} of {len(rh)} entries RIGHT ({len(fh)} found)")
    return out
