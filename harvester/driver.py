"""Input strategies: decide which switches to press and when.

A strategy keeps its own schedule of (time_ms, switch, closed) events.
The run loop calls `due(now_ms)` to get events to apply, and `reward(n)`
after each batch of frames so the strategy can learn what was productive.
"""
from __future__ import annotations

import heapq
import json
import random
from dataclasses import dataclass, field

from .frames import Mask, Patch, Rule
from .glyphs import load as load_digits


@dataclass
class GameMap:
    name: str
    levels: int = 0          # 0 = auto from the DMD (4 or 16 shades)
    pulse_ms: int = 60
    always_closed: list[int] = field(default_factory=list)
    coin: int | None = None               # coin switch, pulsed before start (WPC: 1)
    coins_per_credit: int = 2
    trough_solenoid: int | None = None    # coil that kicks a ball out of the trough
    balls: int | None = None              # balls in the machine, if fewer than trough switches
    serve_pulse: int | None = None        # switch the table script pulses when the trough kicks
                                          # (eject confirmation, e.g. Sharkey's switch 15)
    boot_s: int = 12                      # wait this long after power-on before playing
    serve_timeout_s: int = 30             # no ball served this long -> game over
    start: int | None = None
    trough: list[int] = field(default_factory=list)       # closed = ball present
    shooter_lane: int | None = None
    launch_button: int | None = None
    flippers: list[int] = field(default_factory=list)
    playfield: list[int] = field(default_factory=list)
    drain: list[int] = field(default_factory=list)        # outhole / drain switches
    ball_time_s: tuple[int, int] = (20, 90)
    masks: list[Mask] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    single_player_chance: float = 0.7    # otherwise 2-4 players
    buttons: list[int] = field(default_factory=list)   # extra cabinet buttons to tap sometimes
    drop_banks: list[dict] = field(default_factory=list)  # [{"switches": [...], "reset_coil": n}]
    holes: list[dict] = field(default_factory=list)  # locks/VUKs/saucers: [{"switch": n, "coil": n}]
                                                     # a ball stays in until the game fires the coil
    nvram_map: str | None = None         # pinball-memory-map, for low-scores (relative to games/)
    tilt: int | None = None              # plumb-bob tilt switch: pulsed now and then for DANGER/TILT
    slam_tilt: int | None = None         # ends a game that runs too long
    animation_hold_ms: int = 4000        # wait up to this long for a new animation to finish
    max_game_s: int = 600                # longer than this -> slam tilt (game over, attract, initials...)
    service_buttons: list[int] = field(default_factory=list)  # Sega: [0, -1, -2]; WPC: [5, 6, 7, 8]
    system: str | None = None            # machine type (wpc, de, sega, sam, gts3, capcom, alving ...)
    balls_per_game: int = 3              # only used when PinMAME can't report the coils (timed play)
    game_on_coil: int | None = None      # the "game on" relay: on while a game is being played
    cabinet_pulse_ms: int = 0            # coin/Start press length; 0 = 120/150 ms (some boards scan slowly)
    attract_first_s: int = 15            # watch attract mode this long before the first game
    attract_between_s: tuple[int, int] = (10, 30)  # idle after each game over (game over, match screens)
    wheels: list[dict] = field(default_factory=list)  # motor-driven position encoders, e.g. a mystery
    # wheel: {"motor": 20, "step_ms": 250, "positions": [[33], [33, 34], ...]} - while the motor coil
    # is on, the wheel turns one position per step_ms and the listed switches are closed
    mechs: list[dict] = field(default_factory=list)  # toys moved by motor coils, see Mech below
    eject: dict | None = None            # two-stage trough: {"switch": 15, "load_coil": 16}; the load
    # coil moves a ball from the trough into the eject lane, trough_solenoid then kicks it out
    outhole: dict | None = None          # {"switch": 15, "coil": 9}: a drained ball waits in the
    # outhole until the game fires the coil, which kicks it into the trough (WPC, physical troughs)
    feeds: dict = field(default_factory=dict)   # {"37": 61}: a ball through switch 37 drops into
    # hole 61 shortly after (scoops and subways that lead to a VUK)
    spinners: list[int] = field(default_factory=list)  # spinner switches: now and then ripped 10-40 times
    clean_drain_chance: float = 0.2      # ball that touches nothing and drains (ball-save screens)
    launch_path: list[int] = field(default_factory=list)  # switches a plunged ball rolls over (one
    # is hit by an "untouched" ball, so the game knows it left the shooter lane)
    outlanes: list[int] = field(default_factory=list)     # an untouched ball may drain through one
    recipes: list[dict] = field(default_factory=list)  # shot sequences that progress the game, e.g.
    # {"name": "8-ball", "steps": [17, 18, ..., 44], "gap_ms": [700, 1600], "chance": 0.04}
    # a step is a switch, a list of switches hit together (one shot), or {"switch": n, "times": k}
    roms: list[str] = field(default_factory=list)   # ROM names this game file is for
    digits: str | None = None            # learned digit file (see learn-digits)
    digit_shapes: set = field(default_factory=set)
    path: str = ""

    @staticmethod
    def load(path: str) -> "GameMap":
        d = json.load(open(path))
        if not isinstance(d, dict) or "name" not in d:
            raise SystemExit(f"{path} is not a game file (it should describe the table's switches). "
                             "Pick the game's .json, not its .digits.json.")
        import dataclasses
        known = {f.name for f in dataclasses.fields(GameMap)}
        d = {k: v for k, v in d.items() if k in known}      # notes (_comment...) and unknown keys ignored
        d["masks"] = [Mask(**m) for m in d.get("masks", [])]
        d["rules"] = [Rule(r["name"],
                           tuple(Patch(p["x"], p["y"], tuple(p["rows"])) for p in r["trigger"]),
                           tuple(Mask(**m) for m in r.get("masks", [])),
                           tuple(r["layout_cell"]) if r.get("layout_cell") else None)
                      for r in d.get("rules", [])]
        for k in ("ball_time_s", "attract_between_s"):
            if k in d:
                d[k] = tuple(d[k])
        d["path"] = path
        g = GameMap(**d)
        g.digit_shapes = load_digits(g.digits_path())
        return g

    def digits_path(self) -> str:
        import os
        name = self.digits or os.path.splitext(os.path.basename(self.path))[0] + ".digits.json"
        return os.path.join(os.path.dirname(self.path), name)


class Strategy:
    def __init__(self, game: GameMap, seed: int):
        self.g = game
        self.rng = random.Random(seed)
        self._q: list[tuple[int, int, int, bool]] = []
        self._seq = 0
        self.log: list[str] = []          # messages for the console
        self.events: list[tuple[int, str, dict]] = []   # (when, what, details) for the memory-map finder
        self.game_state: dict = {}        # filled by the run loop from game memory, if a map exists
        self._wheel: dict = {}
        self._mech: dict = {}
        self._closed: dict = {}           # last state we set for each switch

    watch_solenoids: list[int] = []       # coils whose rising edge we want

    @property
    def motors(self) -> list[int]:        # coils whose on/off level we want every loop
        out = [w["motor"] for w in self.g.wheels]
        for m in self.g.mechs:
            out += [c for c in (m.get("motor"), m.get("dir_coil")) if c is not None]
        return sorted(set(out))

    def coils(self, states: dict, now_ms: int):
        """Called every loop with {coil: on} for self.motors."""
        for n in {w["motor"] for w in self.g.wheels}:
            self.motor(n, states.get(n, False), now_ms)
        for i, m in enumerate(self.g.mechs):
            self._mech_step(i, m, states, now_ms)

    def _mech_step(self, i: int, m: dict, states: dict, now_ms: int):
        """A toy with one position value, moved by a motor coil.
        kind "linear": moves toward max, or toward min while dir_coil is on;
                       only if every switch in "needs" is closed.
        kind "oscillate": while the motor is on, travels min->max->min...
        "switches": [{"switch": n, "min": a, "max": b}] closed while a <= value <= b.
        "carries": hole switch whose ball the toy picks up at "pick" and
                   releases (live ball) when it comes back to "drop"."""
        st = self._mech.setdefault(i, {"v": float(m.get("start", m["min"])), "last": now_ms,
                                       "dir": 1, "sw": {}, "loaded": False})
        dt = (now_ms - st["last"]) / 1000.0
        st["last"] = now_ms
        if states.get(m["motor"]) and all(self._closed.get(x) for x in m.get("needs", [])):
            if m.get("kind", "linear") == "linear":
                d = -1 if states.get(m.get("dir_coil")) else 1
                if m.get("dir_flip"):
                    d = -d
                st["v"] = min(m["max"], max(m["min"], st["v"] + d * m["speed"] * dt))
            else:
                st["v"] += st["dir"] * m["speed"] * dt
                if st["v"] >= m["max"]:
                    st["v"], st["dir"] = m["max"], -1
                if st["v"] <= m["min"]:
                    st["v"], st["dir"] = m["min"], 1
        for sp in m.get("switches", []):
            on = sp.get("min", -1e9) <= st["v"] <= sp.get("max", 1e9)
            if st["sw"].get(sp["switch"]) != on:
                st["sw"][sp["switch"]] = on
                self.at(now_ms, sp["switch"], on)
        hole = m.get("carries")
        if hole is not None:
            if hole in getattr(self, "held", ()) and abs(st["v"] - m["pick"]) < 1.0:
                st["loaded"] = True
            if st["loaded"] and abs(st["v"] - m["drop"]) < 1.0:
                st["loaded"] = False
                self._release_hole(hole, now_ms, why="toy")

    def _release_hole(self, sw: int, now_ms: int, why: str = "coil"):
        pass

    def motor(self, number: int, on: bool, now_ms: int):
        """Turn the wheels driven by this coil while it's on."""
        for i, w in enumerate(self.g.wheels):
            if w["motor"] != number:
                continue
            st = self._wheel.setdefault(i, {"pos": w.get("start", 0), "acc": 0, "last": now_ms})
            if on:
                st["acc"] += now_ms - st["last"]
                while st["acc"] >= w.get("step_ms", 250):
                    st["acc"] -= w.get("step_ms", 250)
                    old = set(w["positions"][st["pos"]])
                    st["pos"] = (st["pos"] + 1) % len(w["positions"])
                    new = set(w["positions"][st["pos"]])
                    for sw in old - new:
                        self.at(now_ms, sw, False)
                    for sw in new - old:
                        self.at(now_ms, sw, True)
            st["last"] = now_ms

    def on_solenoid(self, number: int, now_ms: int):
        pass

    def at(self, t_ms: int, switch: int | None, closed: bool):
        if switch is None:
            return
        heapq.heappush(self._q, (t_ms, self._seq, switch, closed))
        self._seq += 1

    def pulse(self, t_ms: int, switch: int | None, ms: int | None = None):
        self.at(t_ms, switch, True)
        self.at(t_ms + (ms or self.g.pulse_ms), switch, False)

    def due(self, now_ms: int) -> list[tuple[int, bool]]:
        out = []
        while self._q and self._q[0][0] <= now_ms:
            _, _, sw, closed = heapq.heappop(self._q)
            out.append((sw, closed))
            self._closed[sw] = closed
        self.plan(now_ms)
        return out

    def init(self) -> list[tuple[int, bool]]:
        """Switch states to apply once at boot."""
        n = self.g.balls if self.g.balls is not None else len(self.g.trough)
        wheel = [(sw, True) for w in self.g.wheels for sw in w["positions"][w.get("start", 0)]]
        mech = []
        for m in self.g.mechs:
            v = m.get("start", m["min"])
            mech += [(sp["switch"], sp.get("min", -1e9) <= v <= sp.get("max", 1e9))
                     for sp in m.get("switches", [])]
        out = ([(s, True) for s in self.g.always_closed] + [(s, True) for s in self.g.trough[:n]]
               + wheel + mech)
        for sw, c in out:
            self._closed[sw] = c
        return out

    def plan(self, now_ms: int):
        pass

    def reward(self, new_frames: int):
        pass


class AttractStrategy(Strategy):
    """Touch nothing: captures attract mode, high scores, rules screens."""


class FuzzStrategy(Strategy):
    """Start games and play them by reacting to the machine, like a VPX script.

    - Boot: wait until the ROM has finished starting up.
    - Start: insert coins, press Start for 1-4 players.
    - When the game fires the TROUGH COIL, a ball moves from the trough to the
      shooter lane and gets plunged (also handles multiball and ball save).
    - Play: pulse random playfield switches, weighted toward switches that
      recently led to new frames; tap flippers.
    - Drain: put the ball back in the trough, tap buttons through bonus,
      initials and match, and wait for the next ball.
    - If no ball is served for a while (game over, or start failed), go back
      to inserting coins and pressing Start.
    """

    def __init__(self, game: GameMap, seed: int):
        super().__init__(game, seed)
        self.weights = {s: 1.0 for s in game.playfield}
        self.recent: list[int] = []
        self.phase = "boot"
        self.next_action_ms = (game.boot_s + game.attract_first_s) * 1000
        self.log.append(f"attract mode for {game.attract_first_s}s; the first game starts at about "
                        f"{game.boot_s + game.attract_first_s}s")
        self.await_started = 0
        self.ball_end_ms = 0
        self.await_until = 0
        # Balls in the trough, stacked from trough[0] up. A trough can hold more balls than it
        # has switches (Pistol Poker: 3 balls, one switch) - the extra ones just aren't seen.
        self.total_balls = game.balls if game.balls is not None else len(game.trough)
        self.in_trough = self.total_balls if game.trough else 0
        self.in_play = 0
        self.games = 0
        self.served_since_start = True
        self.failed_starts = 0                # starts that never got a ball out -> more coins next time
        self.extra_coins = 0                  # extra coins (per player) used on the last start attempt
        self.learned_extra = 0                # ... and what a start that worked needed: keep using it
        self.game_on: bool | None = None      # from the game-on relay, when it's known to work
        self.blind = False                    # PinMAME reports none of this machine's coils: play on timers
        self.blind_balls_left = 0             # ... and count balls to know when the game is over
        self._blind_q: list[tuple[int, int]] = []
        self.stuck_games = 0
        self.serves_in_game = 0               # balls served since Start (more than one per ball with saves)
        self.game_started_ms = None
        self.players = 1
        self._last_pos, self._pos_since = None, None
        self._t, self._last_new_ms = 0, -10 ** 9      # when the display last showed a new frame
        self._hold_from, self._no_hold_until = None, 0
        self.clean = False
        self.eject_full = False
        self.outhole_n = 0                    # balls waiting in the outhole
        self.fixed_players = 0                # 0 = mix of 1-4; set by --players
        self.zero_credits_chance = 0.0       # set by the run loop when game memory is writable
        self.request_zero_credits = False
        self.drop_down: set[int] = set()
        self.drop_switch = {sw: i for i, b in enumerate(game.drop_banks) for sw in b["switches"]}
        self.reset_coil = {b["reset_coil"]: i for i, b in enumerate(game.drop_banks)
                           if b.get("reset_coil") is not None}
        for sw in self.drop_switch:              # drop targets are hit, not pulsed
            self.weights.setdefault(sw, 1.0)
        self.hole_coil = {h["switch"]: h["coil"] for h in game.holes}
        self.coil_holes: dict[int, list[int]] = {}
        for h in game.holes:
            self.coil_holes.setdefault(h["coil"], []).append(h["switch"])
        self.held: set[int] = set()              # hole switches currently holding a ball
        for sw in self.hole_coil:
            self.weights.setdefault(sw, 1.0)

    @property
    def watch_solenoids(self) -> list[int]:
        coils = [self.g.trough_solenoid] if self.g.trough_solenoid is not None else []
        coils += [c for c in self.reset_coil if c not in coils]
        if self.g.eject and self.g.eject.get("load_coil") is not None:
            coils.append(self.g.eject["load_coil"])
        if self.g.outhole and self.g.outhole.get("coil") is not None:
            coils.append(self.g.outhole["coil"])
        return coils + [c for c in self.coil_holes if c not in coils]

    def _trough_add(self, t: int):
        """One more ball rolls into the trough."""
        if self.in_trough < self.total_balls:
            if self.in_trough < len(self.g.trough):
                self.at(t, self.g.trough[self.in_trough], True)
            self.in_trough += 1

    def _trough_take(self, t: int) -> bool:
        """One ball leaves the trough; False if it's empty."""
        if self.in_trough <= 0:
            return False
        self.in_trough -= 1
        if self.in_trough < len(self.g.trough):
            self.at(t, self.g.trough[self.in_trough], False)
        return True

    def on_solenoid(self, number: int, now_ms: int):
        oh = self.g.outhole
        if oh and number == oh.get("coil") and self.outhole_n > 0:
            self.outhole_n -= 1                      # outhole kicks one ball into the trough
            if self.outhole_n == 0:
                self.at(now_ms + 50, oh["switch"], False)
            self._trough_add(now_ms + 400)
        ej = self.g.eject
        if ej and number == ej.get("load_coil"):
            if not self.eject_full and self._trough_take(now_ms + 50):
                self.at(now_ms + 300, ej["switch"], True)
                self.eject_full = True
        if number == self.g.trough_solenoid:
            self._serve(now_ms)
        if number in self.coil_holes:            # a hole kicks its ball back out
            for sw in self.coil_holes[number]:
                self._release_hole(sw, now_ms)
        if number in self.reset_coil:            # bank reset: targets pop back up
            bank = self.g.drop_banks[self.reset_coil[number]]["switches"]
            for sw in bank:
                if sw in self.drop_down:
                    self.at(now_ms + 80, sw, False)
                    self.drop_down.discard(sw)
            self.log.append("drop targets reset")

    def go_blind(self, now_ms: int):
        """Coils can't be seen: the game that just 'failed' is really waiting for its first ball."""
        self.blind = True
        self.blind_balls_left = self.g.balls_per_game * max(1, self.players)
        self.phase = "await"
        self._serve(now_ms + 1000)

    def _release_hole(self, sw: int, now_ms: int, why: str = "coil"):
        if sw not in self.held:
            return
        self.held.discard(sw)
        self.at(now_ms + 50, sw, False)
        self.in_play += 1
        self.log.append(f"hole {sw} {'kicked out' if why == 'coil' else 'emptied by the ' + why} "
                        f"({self.in_play} in play)")
        if self.phase != "play":             # after a drain: it's a live ball again
            self.phase = "play"
            self.ball_end_ms = now_ms + self.rng.randint(5, 20) * 1000
            self.next_action_ms = now_ms + 500

    def _hit(self, t: int, sw: int):
        """Hit a playfield switch: drop targets stay down, a ball entering a
        hole stays there until its coil fires, the rest are pulsed."""
        if sw in self.hole_coil:
            if sw not in self.held and self.in_play > 0:
                self.held.add(sw)
                if self.blind:                       # no coil to wait for: kick it out after a moment
                    self._blind_q.append((t + self.rng.randint(1500, 4000), sw))
                self.in_play -= 1
                self.at(t, sw, True)
                self.log.append(f"ball into hole {sw} ({self.in_play} in play)")
            return
        dest = self.g.feeds.get(str(sw))
        if dest is not None:                     # through a scoop/subway into a hole
            self.pulse(t, sw)
            if dest not in self.held and self.in_play > 0:
                self._hit(t + 700, dest)
            return
        if sw in self.drop_switch:
            if sw not in self.drop_down:
                self.drop_down.add(sw)
                self.at(t, sw, True)
        else:
            self.pulse(t, sw)

    def _serve(self, t: int):
        g = self.g
        self.served_since_start = True
        self.serves_in_game += 1
        self.events.append((t + 300, "serve", {"in_play": self.in_play}))
        if self.failed_starts:
            self.learned_extra = max(self.learned_extra, self.extra_coins)
        self.failed_starts = 0
        if g.serve_pulse is not None:            # tell the game the ball really left
            self.pulse(t + 20, g.serve_pulse, 100)
        if g.eject:
            if not self.eject_full:
                return
            self.eject_full = False
            self.in_play += 1
            self.at(t + 50, g.eject["switch"], False)
        else:
            if not self._trough_take(t + 50):
                return
            self.in_play += 1
        self.at(t + 400, g.shooter_lane, True)
        if g.launch_button is not None:
            self.pulse(t + 1500, g.launch_button, 150)
        self.at(t + self.rng.randint(1500, 3000), g.shooter_lane, False)   # plunge
        self.log.append(f"ball served ({self.in_play} in play)")
        if self.phase != "play":
            self.phase = "play"
            self.next_action_ms = t + 3200
            lo, hi = g.ball_time_s
            self.clean = False
            if self.rng.random() < g.clean_drain_chance and not self.blind:   # (timed play can't see ball saves)  # touch nothing, drain: ball save screens
                self.ball_end_ms = t + 3000 + self.rng.randint(1, 5) * 1000
                self.clean = True
                # Which switches a "touch nothing" ball crosses differs per table, so
                # mix it: nothing at all / top lane only / outlane only / both.
                way = self.rng.choice(["nothing", "lane", "outlane", "lane+outlane"])
                if "lane" in way.split("+") and g.launch_path:
                    self.pulse(t + 3000 + self.rng.randint(300, 900), self.rng.choice(g.launch_path))
                if "outlane" in way and g.outlanes:
                    self.pulse(self.ball_end_ms - 400, self.rng.choice(g.outlanes), 150)
                self.log.append(f"this ball drains untouched (ball save) - via {way}")
            else:
                self.ball_end_ms = t + 3000 + self.rng.randint(lo, hi) * 1000
            if g.tilt is not None and self.rng.random() < 0.12:   # nudge: DANGER, maybe TILT
                at = t + 4000 + self.rng.randint(0, 10) * 1000
                for i in range(self.rng.choice([1, 1, 2, 3])):
                    self.pulse(at + i * 2500, g.tilt, 80)
                self.log.append("will nudge this ball (tilt warning)")

    def plan(self, now_ms: int):
        self._t = now_ms
        if self._blind_q:
            due = [x for x in self._blind_q if x[0] <= now_ms]
            self._blind_q = [x for x in self._blind_q if x[0] > now_ms]
            for _, sw in due:
                self._release_hole(sw, now_ms, why="timer")
        if now_ms < self.next_action_ms:
            return
        g, t = self.g, now_ms

        if self.phase == "boot":
            self.phase = "start"

        elif self.phase == "start":
            # Stray-ball check: everything must be home before a new game, or
            # the machine sits in ball search instead of starting.
            total = self.total_balls if g.trough else 0
            stray = (total - self.in_trough - (1 if self.eject_full else 0) - self.outhole_n)
            if stray > 0 or self.held:
                for sw in list(self.held):
                    self.at(t, sw, False)
                self.held.clear()
                home = total - (1 if self.eject_full else 0) - self.outhole_n
                for k in range(self.in_trough, home):
                    self._trough_add(t + 100 * (k + 1))
                self.in_play = 0
                self.log.append(f"recovered {stray} stray ball(s) into the trough before the next game")
                self.next_action_ms = t + 3000
                return
            if self.game_on and self.blind:
                # timed play miscounted (ball save, extra ball): the game still wants a ball
                self.blind_balls_left = 1
                self.log.append("timed play: the game is still on - serving another ball")
                self._serve(t + 1000)
                return
            if self.game_state.get("game_over") is False and not self.blind:
                self.game_on = True          # the game's memory says a game is running: same as the relay
            if self.game_on:
                # The game is still running but no ball came out. Coins and Start now would only
                # add players, so wait - and end the game if it stays stuck.
                self.stuck_games += 1
                if self.stuck_games >= 2 and g.slam_tilt is not None:
                    self.pulse(t, g.slam_tilt, 150)
                    self.log.append("the game is stuck without a ball - slam tilt to end it")
                    self.stuck_games = 0
                else:
                    self.log.append("the game is still running (game-on relay) but no ball came out - "
                                    "not pressing Start (that would add players)")
                self.next_action_ms = t + 20000
                return
            self.stuck_games = 0
            players = 1 if self.rng.random() < g.single_player_chance else self.rng.choice([2, 2, 2, 3, 4])
            if self.fixed_players:
                players = self.fixed_players
            self.players = players
            coins = (players * g.coins_per_credit + self.rng.randint(0, 3)) if g.coin is not None else 0
            # A start that didn't take probably needed more coins (2+ coins per credit, or a game that
            # costs several credits, like World Poker Tour). Once a start works, keep that amount.
            self.extra_coins = max(self.learned_extra, min(8, 3 * self.failed_starts)) if coins else 0
            coins += self.extra_coins * players
            if self.zero_credits_chance and g.coin is not None and self.rng.random() < self.zero_credits_chance:
                # empty the credits first (the run loop writes 0 into game memory), then one coin
                # alone: the "CREDITS 1/2" / half-credit screens only show from zero
                self.request_zero_credits = True
                self.pulse(t + 1500, g.coin, g.cabinet_pulse_ms or 120)
                self.log.append("credits emptied; one coin, then a pause")
                t += 6000
            if g.coin is not None and self.rng.random() < 0.25:
                coins += self.rng.randint(10, 30)       # double-digit credit screens
            gap = max(400, (g.cabinet_pulse_ms or 120) + 300)   # the switch must open between coins
            self.events.append((t - 300, "before_coins", {}))
            for i in range(coins):
                self.pulse(t + i * gap, g.coin, g.cabinet_pulse_ms or 120)
                self.events.append((t + i * gap + gap - 50, "coin", {"n": i + 1, "of": coins}))
            t2 = t + coins * gap + 800
            for i in range(players):
                # well apart: some boards (Data East) ignore Start for a moment after a game begins
                self.pulse(t2 + i * 3000, g.start, g.cabinet_pulse_ms or 150)
            self.games += 1
            self.game_started_ms = t2
            self._last_pos, self._pos_since = None, None    # a new game: its ball 1 starts now
            self.serves_in_game = 0
            self.served_since_start = False
            self.events.append((t2 + 500, "started", {"players": players}))   # credits are taken at the first press
            self.log.append(f"start attempt {self.games}: {coins} coins, {players} player(s)")
            self.phase = "await"
            self.await_started = t2 + players * 3000 + 60000   # no initials right after start
            self.await_until = t2 + players * 3000 + g.serve_timeout_s * 1000
            self.next_action_ms = t2 + players * 3000 + 1000
            if not self.watch_solenoids or self.blind:   # no coil info: serve on a timer
                self.blind_balls_left = g.balls_per_game * players
                self._serve(t2 + players * 3000 + 3000)

        elif self.phase == "await":
            # Between balls / after the game: bonus count, match, initials entry,
            # game over. Stay hands-off for a while so those animations play out,
            # then work through initials: a run of flipper taps (moves the
            # letter), then Start (accepts it).
            if self.game_state.get("game_over") and t - self.await_started > 20000:
                self.await_until = min(self.await_until, t)      # the game says it's over
            if t >= self.await_until:
                lo, hi = g.attract_between_s
                idle = self.rng.randint(lo, hi)
                if not self.served_since_start:
                    self.failed_starts += 1
                self.log.append(f"no ball served - game over; watching attract for {idle}s")
                self.phase, self.next_action_ms = "start", t + idle * 1000
                return
            if t - self.await_started < 7000:
                self.next_action_ms = min(self.await_started + 7000, self.await_until)
                return
            if g.flippers:
                side = self.rng.choice(g.flippers)
                taps = self.rng.randint(1, 26)
                for i in range(taps):
                    self.pulse(t + i * 220, side, 90)
                t_end = t + taps * 220 + 300
                # Start accepts an initial, but pressed between balls it adds a
                # player, and pressed a moment after game over it starts a NEW
                # game and skips the end-of-game screens. When the game's memory
                # is readable, Start is left alone: ENTER INITIALS comes up by
                # itself, flipper taps change the letter, and the entry times
                # out on its own.
                known = self.game_state.get("current_ball") is not None
                if g.start is not None and not known:
                    self.pulse(t_end, g.start, 120)
                self.next_action_ms = t_end + self.rng.randint(800, 2000)
            else:
                self.next_action_ms = t + 2000

        elif self.phase == "play":
            gs = self.game_state
            pos = (gs.get("current_player"), gs.get("current_ball"))
            if pos[1] is not None:
                # The game tells us which ball it's on: only a ball that never
                # ends (stuck game) gets slammed, however many players there are.
                if pos != self._last_pos:
                    self._last_pos, self._pos_since = pos, t
                limit_s, since = g.max_game_s, self._pos_since
            else:
                limit_s = g.max_game_s * max(1, self.players)   # multi-player games run longer
                since = self.game_started_ms
            if (g.slam_tilt is not None and self.game_started_ms is not None and since is not None
                    and t - since > limit_s * 1000):
                self.pulse(t, g.slam_tilt, 150)
                self.log.append(f"game ran {limit_s // 60} min - slam tilt to end it")
                self.game_started_ms = None
                self._last_pos, self._pos_since = None, None
                # the machine resets its game; put every ball back in the trough
                while self.in_trough < self.total_balls - (1 if self.eject_full else 0):
                    self._trough_add(t + 1000)
                if g.outhole and self.outhole_n:
                    self.outhole_n = 0
                    self.at(t + 1000, g.outhole["switch"], False)
                self.in_play = 0
                for sw in list(self.drop_down):
                    self.at(t + 1000, sw, False)
                self.drop_down.clear()
                self.phase = "await"
                self.await_started = t
                self.await_until = t + g.serve_timeout_s * 1000
                self.next_action_ms = t + 2000
                return
            if getattr(self, "clean", False) and t < self.ball_end_ms:
                self.next_action_ms = self.ball_end_ms        # hands off until it drains
                return
            if t >= self.ball_end_ms and self.in_play == 0:
                # every ball is sitting in a hole - wait for the game to kick one out
                self.ball_end_ms = t + 3000
                self.next_action_ms = t + 1000
                return
            if t >= self.ball_end_ms:
                # Drain one ball back into the trough.
                for d in g.drain:
                    self.pulse(t, d, 300)
                if g.outhole:                         # waits in the outhole for the kicker coil
                    self.outhole_n += 1
                    self.at(t + 300, g.outhole["switch"], True)
                else:
                    self._trough_add(t + 500)
                self.in_play = max(0, self.in_play - 1)
                self.events.append((t + 200, "drain", {"in_play": self.in_play}))
                self.log.append(f"ball drained ({self.in_play} still in play)")
                if self.in_play > 0:              # multiball: keep playing
                    self.ball_end_ms = t + self.rng.randint(5, 20) * 1000
                    self.next_action_ms = t + 300
                else:
                    self.clean = False
                    self.phase = "await"
                    self.await_started = t
                    self.await_until = t + g.serve_timeout_s * 1000
                    if self.game_state.get("current_ball") is not None and not self._mid_game():
                        # last ball of the game: allow time for bonus, initials, match
                        self.await_until = t + 150_000
                    elif not self.game_state and self.serves_in_game < 3 * max(1, self.players):
                        # can't read the game: fewer balls than a 3-ball game has gone by, so more
                        # are surely coming - allow for long end-of-ball bonuses (World Poker Tour)
                        self.await_until = t + max(g.serve_timeout_s, 90) * 1000
                    self.next_action_ms = t + 2500
                    if self.blind:
                        if g.outhole and self.outhole_n:
                            self.on_solenoid(g.outhole["coil"], t + 1200)   # as if the game kicked it
                        self.blind_balls_left -= 1
                        if self.blind_balls_left > 0:
                            self._serve(t + self.rng.randint(4000, 9000))
                        else:
                            self.await_until = t + 40_000        # bonus, match, game over screens
                            self.log.append("timed play: that was the last ball of the game")
                return
            # Let new animations play out: while the display keeps showing frames we haven't
            # captured before, hold off the next hit (up to a few seconds - sparkling score
            # screens never stop being "new"). A real player doesn't hit a target every half
            # second, and a hit during a show often cuts it short (Hook, Family Guy).
            busy = t - self._last_new_ms < 350
            if busy and t >= self._no_hold_until:
                if self._hold_from is None:
                    self._hold_from = t
                if t - self._hold_from < g.animation_hold_ms:
                    self.next_action_ms = t + 120
                    return
                self._no_hold_until = t + 2500     # held long enough: play on for a bit
                self._hold_from = None
            elif not busy:
                self._hold_from = None
            if g.recipes and self.rng.random() < sum(r.get("chance", 0.03) for r in g.recipes):
                r = self.rng.choices(g.recipes, weights=[x.get("chance", 0.03) for x in g.recipes])[0]
                lo, hi = r.get("gap_ms", (700, 1600))
                tt = t
                for step in r["steps"]:
                    if isinstance(step, dict):
                        for i in range(int(step.get("times", 1))):
                            self._hit(tt + i * 70, step["switch"])
                        tt += int(step.get("times", 1)) * 70
                    else:
                        for k, sw in enumerate(step if isinstance(step, list) else [step]):
                            self._hit(tt + k * 120, sw)
                    tt += self.rng.randint(lo, hi)
                self.log.append(f"recipe: {r.get('name', '?')}")
                self.next_action_ms = tt + 300
                return
            if g.spinners and self.rng.random() < 0.02:
                sw, n = self.rng.choice(g.spinners), self.rng.randint(10, 40)
                for i in range(n):
                    self.pulse(t + i * 70, sw, 30)
                self.log.append(f"spinner {sw} x{n}")
                self.next_action_ms = t + n * 70 + 300
                return
            if self.rng.random() < 0.06 and self.weights:
                # "Author way": a deliberate burst instead of a single random hit.
                pf = sorted(set(g.playfield) | set(self.drop_switch))
                if self.rng.random() < 0.5 or not pf:
                    sw = self._pick()
                    n = self.rng.randint(3, 7)
                    gap = self.rng.randint(1000, 2500)      # room for each hit's animation
                    for i in range(n):
                        self._hit(t + i * gap, sw)
                    self.log.append(f"focus: switch {sw} x{n}")
                else:
                    start = self.rng.choice(pf)
                    group = [x for x in pf if x // 10 == start // 10]  # same bank of numbers
                    gap = self.rng.randint(700, 1600)
                    for i, sw in enumerate(group):
                        self._hit(t + i * gap, sw)
                    n = len(group)
                    self.log.append(f"sweep: switches {group[0]}-{group[-1]}")
                self.recent = (self.recent + [sw])[-8:]
                self.next_action_ms = t + n * gap + 500
                return
            r = self.rng.random()
            # Hands off the flippers for the first seconds of a game: some games ask "hold right
            # button for French" (Family Guy) or similar, and a held flipper would answer it
            early = self.game_started_ms is not None and \
                t < self.game_started_ms + 3000 * max(1, self.players) + 12000
            if r < 0.15 and g.flippers and not early:
                self.pulse(t, self.rng.choice(g.flippers), self.rng.choice([80, 250, 1500, 5000]))
            elif r < 0.18 and g.buttons:
                self.pulse(t, self.rng.choice(g.buttons), 150)
            elif self.weights:
                sw = self._pick()
                self._hit(t, sw)
                self.recent.append(sw)
                self.recent = self.recent[-8:]
            self.next_action_ms = t + self.rng.randint(80, 900)

    def _mid_game(self) -> bool:
        """True when the game's memory says more balls are still to come.
        Pressing Start then would add a player instead of entering initials."""
        gs = self.game_state
        try:
            if gs.get("game_over") or gs.get("current_ball") is None:
                return False
            return (gs["current_ball"] < gs["ball_count"]
                    or gs["current_player"] < gs["player_count"])
        except (KeyError, TypeError):
            return False

    def _pick(self) -> int:
        sw = list(self.weights)
        return self.rng.choices(sw, weights=[self.weights[s] for s in sw])[0]

    def reward(self, new_frames: int):
        if new_frames:
            self._last_new_ms = getattr(self, "_t", 0)
        # Decay everything slightly; boost switches hit just before novelty.
        for s in self.weights:
            self.weights[s] = max(0.2, self.weights[s] * 0.995)
        if new_frames:
            for s in self.recent:
                self.weights[s] = min(20.0, self.weights[s] + 0.5 * new_frames)
            self.recent.clear()


# How each machine's service buttons work (switch numbers from VPX's system scripts).
# Menus with four buttons: enter / up / down / back ("start" = the Start button).
# Data East, System 11: Black advances, Green sets the direction. GTS3: one diagnostic button.
# Capcom: a diagnostic switch that is turned on and stays on; flippers and Start move around.
SERVICE_KEYS = {
    "wpc":    dict(enter=8, up=7, down=6, back=5),
    "sam":    dict(enter=0, up=-1, down=-2, back=-3),
    "sega":   dict(enter=0, up=-1, down=-2, back="start"),     # Black / Green / Red
    "sega2":  dict(enter=0, up=-1, down=-2, back="start"),
    "alving": dict(enter=-7, back=-6, up=15, down=16),
    "de":     dict(advance=-7, direction=-6),
    "de2":    dict(advance=-7, direction=-6),
    "s11":    dict(advance=-7, direction=-6),
    "s7":     dict(advance=-7, direction=-6),
    "gts3":   dict(advance=-8),
    "capcom": dict(toggle=8),
}


def system_of_game(g: "GameMap") -> str | None:
    """The machine type: from the game file, or guessed from its service buttons (older files)."""
    if g.system in SERVICE_KEYS:
        return g.system
    sb = list(g.service_buttons)
    if sb == [5, 6, 7, 8]:
        return "wpc"
    if sb == [-3, -2, -1, 0]:
        return "sam"
    if sb == [0, -1, -2]:
        return "sega"
    if sb == [-7, -6]:
        return "alving" if g.start == 9 else "de"
    if sb == [-8]:
        return "gts3"
    if sb == [8]:
        return "capcom"
    return None


class ServiceStrategy(Strategy):
    """Explore the service/test menus. When the machine type is known it first takes a
    tour the way a person would (into each main-menu item, through its pages, into some
    tests and out again); then, and for unknown machines, it presses service buttons,
    flippers and Start at random, favouring buttons that recently led to new screens.
    No game is played. The run loop backs up the game's NVRAM first and restores it
    afterwards, because menus can change settings."""

    def __init__(self, game: GameMap, seed: int):
        super().__init__(game, seed)
        self.keys = list(game.service_buttons) + list(game.flippers)
        if game.start is not None:
            self.keys.append(game.start)
        self.weights = {k: 1.0 for k in self.keys}
        self.recent: list[int] = []
        self.next_action_ms = (game.boot_s + 10) * 1000
        self.phase = "service"
        self.system = system_of_game(game)
        self.sk = SERVICE_KEYS.get(self.system or "", {})
        self.tour: list[tuple[str, int]] = self._make_tour() if self.sk else []
        self.random_until = 0
        if not game.service_buttons and not self.sk:
            self.log.append("no service_buttons in the game file - nothing to explore")
        elif self.sk:
            self.log.append(f"service menu tour for {self.system} ({len(self.tour)} presses), then exploring")

    def _make_tour(self) -> list[tuple[str, int]]:
        r, sk, steps = self.rng, self.sk, []
        if "enter" in sk:                                   # WPC, SAM, Whitestar, Alvin G.
            steps.append(("enter", 3000))                   # into the menu
            for _ in range(r.randint(6, 9)):                # the main-menu items
                steps.append(("enter", 3000))               # open one
                for _ in range(r.randint(5, 14)):           # its pages
                    steps.append(("up", r.randint(1500, 2600)))
                    if r.random() < 0.2:                    # look inside (a test, an audit page) and leave;
                        steps += [("enter", r.randint(3000, 6000)), ("back", 2000)]   # back cancels any edit
                steps.append(("back", 2500))
                steps.append(("up", 2000))                  # next main-menu item
            steps += [("back", 2000)] * 3                   # and out again
        elif "advance" in sk:                               # Data East, System 11, GTS3
            steps.append(("advance", 3000))
            for _ in range(r.randint(40, 80)):
                x = r.random()
                if x < 0.08 and self.g.start is not None:
                    steps.append(("start", r.randint(3000, 6000)))      # run this test
                elif x < 0.12 and "direction" in sk:
                    steps.append(("direction", 800))                    # back the other way for a bit
                else:
                    steps.append(("advance", r.randint(1500, 3000)))
        elif "toggle" in sk:                                # Capcom: switch test mode on, look around
            steps.append(("toggle", 4000))
            for _ in range(r.randint(30, 60)):
                k = r.choice(["start"] + ["flip"] * 3)
                steps.append((k, r.randint(1500, 3000)))
            steps.append(("toggle", 3000))
        return steps

    def _press(self, now_ms: int, role: str):
        sk, g = self.sk, self.g
        if role in ("direction", "toggle"):                 # switches that stay where they're put
            sw = sk[role]
            self.at(now_ms, sw, not self._closed.get(sw, False))
            return
        if role == "flip":
            if g.flippers:
                self.pulse(now_ms, self.rng.choice(g.flippers), 150)
            return
        sw = sk.get(role, role)
        if sw == "start" or role == "start":
            sw = g.start
        if isinstance(sw, int):
            self.pulse(now_ms, sw, 150)

    def plan(self, now_ms: int):
        if now_ms < self.next_action_ms:
            return
        if self.tour:
            role, wait = self.tour.pop(0)
            self._press(now_ms, role)
            self.next_action_ms = now_ms + wait
            if not self.tour:
                self.log.append("service menu tour done - exploring at random")
                self.random_until = now_ms + 120_000
            return
        if self.sk and self.random_until and now_ms > self.random_until:
            self.tour = self._make_tour()                    # another tour, different pages
            self.random_until = 0
            return
        if not self.g.service_buttons:
            return
        ks = list(self.weights)
        k = self.rng.choices(ks, weights=[self.weights[x] for x in ks])[0]
        hold = 1200 if self.rng.random() < 0.1 else 150      # some menus need a held button
        self.pulse(now_ms, k, hold)
        self.recent = (self.recent + [k])[-3:]
        self.next_action_ms = now_ms + hold + self.rng.randint(500, 1600)

    def reward(self, new_frames: int):
        for k in self.weights:
            self.weights[k] = max(0.3, self.weights[k] * 0.997)
        if new_frames:
            for k in self.recent:
                self.weights[k] = min(10.0, self.weights[k] + 0.4 * new_frames)
            self.recent.clear()


STRATEGIES = {"attract": AttractStrategy, "fuzz": FuzzStrategy, "service": ServiceStrategy}
