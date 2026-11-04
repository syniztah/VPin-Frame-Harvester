"""Offline tests: dedupe, masking, export round-trip, merge, and driver timing.
No emulator or ROM needed.  Run:  python tests/test_offline.py
"""
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.driver import FuzzStrategy, GameMap
from harvester.export import frame_to_txt, read_txt
from harvester.frames import Frame, Mask, Patch, Rule, frame_key, normalize
from harvester.store import FrameStore, merge

W, H = 128, 32
SCORE = Mask(0, 0, 128, 8, "score")


def make(seed, score=0, t=0):
    rng = random.Random(seed)
    px = bytearray(rng.randrange(4) for _ in range(W * H))
    for x in range(min(W, score % 97)):        # vary the score band only
        px[2 * W + x] = 3
    return Frame(t, W, H, bytes(px))


def test_quantize():
    f = normalize(bytes([0, 63, 64, 127, 128, 255]), 6, 1, 0, 4)
    assert f.pixels == bytes([0, 0, 1, 1, 2, 3]), f.pixels


def test_mask_collapses_scores():
    a, b = make(1, score=10), make(1, score=50)
    assert frame_key(a, []) != frame_key(b, [])
    assert frame_key(a, [SCORE]) == frame_key(b, [SCORE])


def test_rule_collapses_only_matching_screens():
    base = bytearray(W * H)
    for x in range(10, 20):                      # the "BALL" word
        base[30 * W + x] = 3
    word = Frame(0, W, H, bytes(base))
    rule = Rule("score", (Patch.cut(word, 10, 30, 10, 1),), (Mask(0, 0, 128, 20),), (32, 10))

    def screen(score_px, extra=None, with_word=True):
        px = bytearray(base if with_word else bytes(W * H))
        px[5 * W + score_px] = 3                 # "score digit" moves around
        if extra is not None:
            px[25 * W + extra] = 2               # something outside the mask
        return Frame(0, W, H, bytes(px))

    k = lambda f: frame_key(f, [], [rule])
    assert k(screen(3)) == k(screen(7))           # different score, same layout
    assert k(screen(3)) != k(screen(70))          # different layout cell
    assert k(screen(3)) != k(screen(3, extra=50)) # change outside mask is kept
    assert k(screen(3, with_word=False)) != k(screen(7, with_word=False))  # no trigger


def test_learn_digits():
    from harvester import glyphs
    font = {  # 3x5 "digits"
        0: ["###", "#.#", "#.#", "#.#", "###"], 1: [".#.", "##.", ".#.", ".#.", "###"],
        2: ["###", "..#", "###", "#..", "###"], 3: ["###", "..#", ".##", "..#", "###"],
        4: ["#.#", "#.#", "###", "..#", "..#"], 5: ["###", "#..", "###", "..#", "###"]}
    letter = ["#.#", "#.#", "###", "#.#", "#.#"]           # blinks, never swaps
    sign = [["##.", "#.#", "##.", "#.#", "##."], [".##", "#..", "#..", "#..", ".##"]]  # flips at ONE spot

    def draw(px, x, y, g, v=3):
        for r, row in enumerate(g):
            for c, ch in enumerate(row):
                if ch == "#":
                    px[(y + r) * W + x + c] = v

    frames = []
    for i in range(120):
        px = bytearray(W * H)
        for k in range(20 + (i % 7)):                       # moving "animation" blob
            px[20 * W + 40 + k] = 2
        draw(px, 100, 2, font[i % 6])                       # score: ones digit ticks
        draw(px, 96, 2, font[(i // 6) % 6])                 # tens digit, slower
        if i % 10 >= 5:
            draw(px, 5, 2, letter)                          # blinking letter
        draw(px, 60, 10, sign[(i // 3) % 2])                # a sign flipping in one place
        frames.append(Frame(i, W, H, bytes(px)))
    learned = glyphs.learn(frames)
    shapes = {(3, 5, bytes(1 if c == "#" else 0 for row in g for c in row)) for g in font.values()}
    as_shape = lambda g: (3, 5, bytes(1 if c == "#" else 0 for row in g for c in row))
    assert shapes <= learned, len(learned & shapes)
    assert as_shape(letter) not in learned
    assert not ({as_shape(g) for g in sign} & learned)       # one spot only: not a digit
    # digits erased -> frames differing only by digits collapse, animation still counts
    k = lambda f: frame_key(f, [], [], learned)
    assert k(frames[0]) == k(frames[42])                    # same blob, letter and sign; other digits
    assert k(frames[0]) != k(frames[1])                     # blob differs


def test_drop_targets_stay_down_until_reset():
    g = GameMap("t", start=13, trough=[32], shooter_lane=27, trough_solenoid=1, boot_s=0,
                playfield=[], drop_banks=[{"switches": [17, 18], "reset_coil": 6}],
                ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=1)
    assert 6 in s.watch_solenoids and 1 in s.watch_solenoids
    state, now = {}, 0
    s.on_solenoid(1, 0)                                  # a ball gets served
    for now in range(0, 30_000, 20):
        for sw, closed in s.due(now):
            state[sw] = closed
    assert state.get(17) is True and state.get(18) is True   # both down, stayed down
    s.on_solenoid(6, now)
    popped = set()
    for t in range(now, now + 500, 20):
        for sw, closed in s.due(t):
            if not closed:
                popped.add(sw)
    assert {17, 18} <= popped                                # reset popped them up


def test_ball_count():
    g = GameMap("t", trough=[14, 13, 12, 11, 10], balls=4, always_closed=[9])
    s = FuzzStrategy(g, seed=1)
    assert s.in_trough == 4
    assert sorted(sw for sw, c in s.init() if c) == [9, 11, 12, 13, 14]


def test_service_strategy():
    from harvester.driver import ServiceStrategy
    g = GameMap("t", start=54, flippers=[82, 84], service_buttons=[0, -1, -2], boot_s=0)
    s = ServiceStrategy(g, seed=2)
    assert s.system == "sega" and s.tour and s.tour[0][0] == "enter"   # Whitestar: a menu tour first
    pressed, first = set(), None
    for now in range(0, 900_000, 20):
        for sw, closed in s.due(now):
            if closed:
                pressed.add(sw)
                first = sw if first is None else first
        s.reward(0)
    assert first == 0, first                                   # Black button opens the menu
    assert {0, -1, -2} <= pressed and 54 in pressed, pressed   # Green/Red move, Start backs out


def test_rom_only_game_file():
    from harvester.driver import ServiceStrategy, system_of_game
    from harvester import scriptgen
    import json
    import tempfile
    g, rep = scriptgen.make_rom_game("xyz_10", "wpc")
    assert g["system"] == "wpc" and g["service_buttons"] == [5, 6, 7, 8] and g["_rom_only"]
    p = os.path.join(tempfile.mkdtemp(), "xyz_10.json")
    json.dump(g, open(p, "w"))
    gm = GameMap.load(p)
    assert system_of_game(gm) == "wpc"
    s = ServiceStrategy(gm, seed=1)
    assert s.tour[0] == ("enter", 3000)


def test_focus_bursts_happen():
    g = GameMap("t", start=13, trough=[32, 33], shooter_lane=27, trough_solenoid=1, boot_s=0,
                attract_first_s=0, playfield=[21, 22, 23, 24, 41, 42], ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=5)
    s.on_solenoid(1, 0)
    for now in range(0, 600_000, 20):
        s.due(now)
    msgs = " ".join(s.log)
    assert "focus:" in msgs and "sweep:" in msgs, msgs[:300]


def test_slam_tilt_ends_long_games():
    g = GameMap("t", coin=1, start=13, trough=[32, 33], shooter_lane=27, trough_solenoid=1,
                boot_s=0, attract_first_s=0, playfield=[41], ball_time_s=(1000, 1000),
                slam_tilt=21, max_game_s=60)
    s = FuzzStrategy(g, seed=1)
    slammed = False
    for now in range(0, 200_000, 20):
        for sw, closed in s.due(now):
            if sw == 21 and closed:
                slammed = True
        if s.phase == "await" and s.in_play == 0 and now == 5000:
            s.on_solenoid(1, now)                       # game started, ball served
    assert slammed and s.in_trough == 2


def test_low_scores_keeps_checksums_valid(tmp):
    import json
    from harvester.nvram import lower_high_scores, _calc, _checksum_regions
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mp = os.path.join(root, "games", "nvram_maps", "tom_13.map.json")
    regs = list(_checksum_regions(json.load(open(mp))))
    buf = bytearray(random.Random(9).randbytes(0x2000))
    for st, sp, ad, bits, comp in regs:
        buf[ad:ad + bits // 8] = _calc(buf, st, sp, bits, comp).to_bytes(bits // 8, "big")
    nv = os.path.join(tmp, "tom_13.nv")
    open(nv, "wb").write(buf)
    assert "set 5 high scores" in lower_high_scores(nv, mp)
    nb = open(nv, "rb").read()
    assert all(_calc(nb, st, sp, bits, comp) == int.from_bytes(nb[ad:ad + bits // 8], "big")
               for st, sp, ad, bits, comp in regs)
    bad = bytearray(nb)
    bad[0x1C83] ^= 0x11                                     # break the GC checksum
    open(nv, "wb").write(bad)
    assert "NOT changed" in lower_high_scores(nv, mp)
    assert open(nv, "rb").read() == bytes(bad)


def test_serve_pulse():
    g = GameMap("t", trough=[14, 13], shooter_lane=16, trough_solenoid=1, serve_pulse=15,
                boot_s=0, attract_first_s=0, ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=1)
    s.on_solenoid(1, 0)
    got = [(sw, c) for now in range(0, 1000, 20) for sw, c in s.due(now)]
    assert (15, True) in got and (15, False) in got


def test_holes_hold_balls_until_coil():
    g = GameMap("t", trough=[14, 13, 12], shooter_lane=16, trough_solenoid=1, boot_s=0,
                attract_first_s=0, playfield=[], holes=[{"switch": 44, "coil": 3}],
                ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=1)
    assert 3 in s.watch_solenoids
    s.on_solenoid(1, 0)                                  # one ball in play
    s._hit(5000, 44)
    assert s.in_play == 0 and 44 in s.held
    ev = [(sw, c) for now in range(5000, 6000, 20) for sw, c in s.due(now)]
    assert (44, True) in ev and (44, False) not in ev    # stays closed
    s.on_solenoid(3, 6000)                               # game kicks it out
    assert s.in_play == 1 and not s.held
    ev = [(sw, c) for now in range(6000, 6200, 20) for sw, c in s.due(now)]
    assert (44, False) in ev


def test_stray_balls_recovered_before_new_game():
    g = GameMap("t", coin=1, start=13, trough=[14, 13, 12], shooter_lane=16, trough_solenoid=1,
                boot_s=0, attract_first_s=0, holes=[{"switch": 44, "coil": 3}],
                ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=1)
    s.in_trough, s.in_play = 1, 0                        # two balls lost track of
    s.held.add(44)
    s.phase, s.next_action_ms = "start", 0
    ev = [(sw, c) for now in range(0, 2000, 20) for sw, c in s.due(now)]
    assert (13, True) in ev and (12, True) in ev and (44, False) in ev
    assert s.in_trough == 3 and not s.held
    # a hole kicking out after the ball drained makes it a live ball again
    s.phase = "await"
    s.held.add(44)
    s.on_solenoid(3, 5000)
    assert s.phase == "play" and s.in_play == 1


def test_roundtrip(tmp):
    f = make(2, t=0x1234)
    p = os.path.join(tmp, "one.txt")
    open(p, "w").write(frame_to_txt(f))
    g = read_txt(p)[0]
    assert (g.t_ms, g.width, g.height, g.pixels) == (f.t_ms, f.width, f.height, f.pixels)


def test_store_and_merge(tmp):
    w1, w2, out = (os.path.join(tmp, d) for d in ("w1", "w2", "merged"))
    s1 = FrameStore(w1, [SCORE], 4, png=False)
    for i in range(50):                     # 5 distinct screens, score changing
        s1.add(make(i % 5, score=i, t=i))
    s1.close()
    assert len(s1) == 5, len(s1)

    s2 = FrameStore(w2, [SCORE], 4, png=False)
    for i in range(3, 8):                   # overlaps on 3,4; adds 5,6,7
        s2.add(make(i, t=i))
    s2.close()

    r = merge([w1, w2], out, [SCORE], 4)
    assert r["unique"] == 8, r
    assert os.path.exists(r["report"])


def test_fuzz_schedule():
    """Simulated machine: the 'ROM' fires the trough coil ~2s after each
    Start press while a game is running, and after each drain."""
    g = GameMap("t", coin=1, start=13, trough=[32, 33, 34], shooter_lane=27, drain=[31],
                always_closed=[22], playfield=list(range(41, 50)), ball_time_s=(1, 2),
                trough_solenoid=1, boot_s=2, serve_timeout_s=5,
                attract_first_s=0, attract_between_s=(1, 2))
    s = FuzzStrategy(g, seed=3)
    seen, fire_at, served = set(), None, 0
    for now in range(0, 120_000, 20):
        for sw, closed in s.due(now):
            seen.add(sw)
            if sw == 13 and closed and s.in_play == 0 and fire_at is None:
                fire_at = now + 2000                   # game starts, kicks a ball
            if sw in g.trough and closed and served < 6:
                fire_at = now + 2000                   # ball drained -> next ball
        if fire_at is not None and now >= fire_at:
            s.on_solenoid(1, now)
            served += 1
            fire_at = None
        s.reward(1 if now % 1000 == 0 else 0)
    assert {1, 13, 27, 34}.issubset(seen), seen
    assert seen & set(g.playfield)
    assert g.always_closed[0] not in seen       # never toggled after init
    assert served >= 3 and s.games >= 2, (served, s.games)   # played, then restarted
    assert 0 <= s.in_trough <= len(g.trough)


def test_no_start_between_balls():
    g = GameMap("t", start=54, flippers=[82, 84], trough=[14], boot_s=0, attract_first_s=0,
                attract_between_s=(120, 120))     # only Start presses inside "await" count
    def starts(state):
        s = FuzzStrategy(g, seed=3)
        s.phase, s.await_started, s.await_until, s.next_action_ms = "await", 0, 10**9, 8000
        s.game_state = state
        return sum(1 for now in range(8000, 90_000, 20) for sw, c in s.due(now) if sw == 54 and c)
    mid = {"current_player": 1, "player_count": 3, "current_ball": 1, "ball_count": 3}
    last = {"current_player": 3, "player_count": 3, "current_ball": 3, "ball_count": 3}
    assert starts(mid) == 0            # would add a player
    assert starts(last) == 0           # might start a new game; initials time out by themselves
    assert starts(dict(last, game_over=True)) == 0   # would start a new game
    assert starts({}) > 0              # no memory map: old behaviour


def test_wheel_turns_while_motor_on():
    pos = [[33], [33, 34], [34], [34, 35]]
    g = GameMap("t", wheels=[{"motor": 20, "step_ms": 250, "positions": pos}])
    s = FuzzStrategy(g, seed=1)
    assert (33, True) in s.init()
    state = {33: True}
    for now in range(0, 1000, 10):
        s.motor(20, now < 600, now)          # on for 600 ms = 2 steps
        for sw, c in s.due(now):
            state[sw] = c
    assert {k for k, v in state.items() if v} == {34}, state


def test_clean_drain_touches_nothing():
    g = GameMap("t", trough=[14, 13], shooter_lane=16, trough_solenoid=1, boot_s=0,
                attract_first_s=0, playfield=[25, 26, 27], flippers=[82], clean_drain_chance=1.0,
                ball_time_s=(1000, 1000))
    s = FuzzStrategy(g, seed=1)
    s.on_solenoid(1, 0)
    hits = {sw for now in range(0, 9000, 20) for sw, c in s.due(now) if c and sw in (25, 26, 27, 82)}
    g.launch_path = [46]
    seen = set()
    for seed in range(12):
        s = FuzzStrategy(g, seed=seed)
        s.on_solenoid(1, 0)
        seen |= {sw for now in range(0, 9000, 20) for sw, c in s.due(now) if c}
    assert 46 in seen
    assert not hits and s.in_play == 0, (hits, s.in_play)


def test_recipes_run():
    g = GameMap("t", trough=[14], shooter_lane=16, trough_solenoid=1, boot_s=0, attract_first_s=0,
                playfield=[25], ball_time_s=(1000, 1000),
                drop_banks=[{"switches": [17, 18], "reset_coil": 5}], holes=[{"switch": 44, "coil": 3}],
                recipes=[{"name": "rack", "steps": [17, 18, {"switch": 38, "times": 5}, 44], "chance": 0.5}],
                clean_drain_chance=0.0)
    s = FuzzStrategy(g, seed=2)
    s.on_solenoid(1, 0)
    got = [sw for now in range(0, 60_000, 20) for sw, c in s.due(now) if c]
    assert any("recipe: rack" in m for m in s.log)
    assert got.count(38) >= 5 and 17 in got and 44 in got


def test_two_stage_trough_and_toy():
    g = GameMap("t", trough=[14, 13], balls=2, trough_solenoid=2, eject={"switch": 15, "load_coil": 16},
                shooter_lane=16, boot_s=0, attract_first_s=0, playfield=[], ball_time_s=(1000, 1000),
                holes=[{"switch": 55, "coil": 7}],
                mechs=[{"kind": "oscillate", "motor": 14, "min": -80, "max": 0, "start": 0, "speed": 25,
                        "switches": [{"switch": 57, "min": -1.5}], "carries": 55, "pick": -80, "drop": 0}])
    s = FuzzStrategy(g, seed=1)
    s.init()
    s.on_solenoid(2, 0)
    assert s.in_play == 0                      # nothing in the eject lane yet
    s.on_solenoid(16, 0)
    assert s.eject_full and s.in_trough == 1
    s.on_solenoid(2, 500)
    assert s.in_play == 1 and not s.eject_full
    s._hit(1000, 55)
    assert 55 in s.held
    for now in range(1000, 10000, 10):
        s.coils({14: True}, now)
        s.due(now)
    assert 55 not in s.held and s.in_play == 1
    from harvester.driver import ServiceStrategy, AttractStrategy
    for cls in (ServiceStrategy, AttractStrategy):           # toys move in every mode
        o = cls(g, seed=1)
        o.init()
        for now in range(0, 5000, 10):
            o.coils({14: True}, now)
            o.due(now)


def test_game_file_from_script(tmp):
    """A game file made from a table script loads and plays (offline)."""
    import json
    from harvester import scriptgen
    script = """
LoadVPM "01560000", "sega.VBS", 3.36
Const cGameName = "test_100"
Sub Table1_Init
    Controller.Switch(9) = 1
    Set bsTrough = New cvpmBallStack
    With bsTrough
        .InitSw 0, 14, 13, 12, 11, 0, 0, 0
        .Balls = 3
        .IsTrough = 1
    End With
    Set bsVUK = New cvpmBallStack
    bsVUK.InitSw 0, 44, 0, 0, 0, 0, 0, 0
    Set dtBank = New cvpmDropTarget
    dtBank.InitDrop Array(sw17, sw18), Array(17, 18)
End Sub
SolCallback(1) = "bsTrough.SolOut"
SolCallback(3) = "bsVUK.SolOut"
SolCallback(5) = "dtBank.SolDropUp"
Sub sw16_Hit:Controller.Switch(16) = 1:End Sub  ' shooter lane
Sub sw25_Hit
    vpmTimer.PulseSw 25
End Sub
Sub Spinner_Spin:vpmTimer.PulseSw 38:End Sub
Sub sw57_Hit:Controller.Switch(57) = 1:End Sub  ' left outlane
"""
    g, rep = scriptgen.make_game(script, table="Test")
    assert g["roms"] == ["test_100"] and g["start"] == 54 and g["trough"] == [14, 13, 12, 11]
    assert g["balls"] == 3 and g["trough_solenoid"] == 1 and g["shooter_lane"] == 16
    assert g["holes"] == [{"switch": 44, "coil": 3}]
    assert g["drop_banks"] == [{"switches": [17, 18], "reset_coil": 5}]
    assert 25 in g["playfield"] and 38 in g["spinners"] and 57 in g["outlanes"]
    assert g["always_closed"] == [9]
    p = os.path.join(tmp, "test.json")
    json.dump(g, open(p, "w"))
    gm = GameMap.load(p)
    s = FuzzStrategy(gm, seed=1)
    s.init()
    for now in range(0, 200_000, 20):
        s.due(now)
    assert s.games >= 1


def test_outhole_holds_ball_until_coil():
    g = GameMap("t", trough=[16, 17, 18], trough_solenoid=10, outhole={"switch": 15, "coil": 9},
                shooter_lane=None, boot_s=0, attract_first_s=0, playfield=[25], ball_time_s=(2, 2),
                clean_drain_chance=0.0)
    s = FuzzStrategy(g, seed=1)
    s.init()
    s.on_solenoid(10, 0)                       # ball out of the trough
    assert s.in_trough == 2 and s.in_play == 1
    events = [(sw, c) for now in range(0, 10_000, 20) for sw, c in s.due(now)]
    assert (15, True) in events and s.outhole_n == 1 and s.in_trough == 2   # drained into the outhole
    s.on_solenoid(9, 10_000)                   # outhole kicker
    events = [(sw, c) for now in range(10_000, 11_000, 20) for sw, c in s.due(now)]
    assert (15, False) in events and s.outhole_n == 0 and s.in_trough == 3


def test_scriptgen_kicker_trough_and_ballstack_outhole():
    from harvester.scriptgen import make_game
    tmnt = """LoadVPM "01560000", "DE.VBS", 3.26
Const cGameName = "tmnt_104"
SolCallback(1) = "kisort"
SolCallback(2) = "KickBallToLane"
Sub tmnt_Init
 Controller.Run
 CreatBalls
End Sub
Sub CreatBalls()
 Set cBall1 = Kicker1.CreateSizedballWithMass(25,1)
 Set cBall2 = Kicker2.CreateSizedballWithMass(25,1)
 Set cBall3 = Kicker3.CreateSizedballWithMass(25,1)
 Set cBall4 = Kicker5.CreateSizedballWithMass(25,1)
 Kicker5.Kick 0,1
 Kicker5.enabled = false
End Sub
Sub Kicker3_Hit():Controller.Switch(11) = 1:End Sub
Sub Kicker2_Hit():Controller.Switch(12) = 1:End Sub
Sub Kicker1_Hit():Controller.Switch(13) = 1:End Sub
Sub Drain_Hit():Controller.Switch(10) = 1:End Sub
Sub kisort(enabled):If enabled then Drain.Kick 70,20:End Sub
Sub KickBallToLane(Enabled):if enabled then Kicker1.Kick 70,5:End Sub
Sub sw30_Hit:vpmTimer.PulseSw 30:End Sub
"""
    g, rep = make_game(tmnt)
    assert g["trough"] == [13, 12, 11] and g["trough_solenoid"] == 2, (g, rep)
    assert g["outhole"] == {"switch": 10, "coil": 1}, rep
    pp = """LoadVPM "01300000","alvinG.VBS",3.10
Const cGameName="pstlpkr"
SolCallback(13)="bsTrough.SolIn"
SolCallback(30)="bsTrough.SolOut"
Sub Table1_Init
 vpmNudge.TiltSwitch=10
 Set bsTrough=New cvpmBallStack
 bsTrough.InitSw 17,18,0,0,0,0,0,0
 bsTrough.Balls=3
End Sub
Sub sw21_Hit:Controller.Switch(21)=1 : BIPL = 1: End Sub
Sub sw30_Hit:vpmTimer.PulseSw 30:End Sub
"""
    g, rep = make_game(pp)
    assert g["start"] == 9 and g["coin"] == 1 and g["tilt"] == 10, g
    assert g["outhole"] == {"switch": 17, "coil": 13} and g["trough_solenoid"] == 30, (g, rep)
    assert g["shooter_lane"] == 21


def test_one_switch_trough_holds_several_balls():
    """Pistol Poker: 3 balls behind one trough switch - a second ball must still come out."""
    g = GameMap("t", start=9, trough=[18], balls=3, shooter_lane=21, trough_solenoid=30,
                outhole={"switch": 17, "coil": 13}, boot_s=0, attract_first_s=0)
    s = FuzzStrategy(g, seed=1)
    assert (18, True) in s.init()
    s.on_solenoid(30, 1000)
    s.on_solenoid(30, 2000)                     # e.g. a ball got locked: the game serves another
    assert s.in_play == 2 and s.in_trough == 1
    closed = dict(s.due(5000))
    assert closed.get(18) is not True          # still one ball behind the switch: not opened...
    s.on_solenoid(30, 6000)
    closed = dict(s.due(7000))
    assert closed.get(18) is False and s.in_play == 3   # ...until the last ball leaves


# ---- memory-map finder on a simulated machine with known positions
import random as _random
import numpy as _np
from harvester import memfind

def _mfbcd(n, L):
    s = f"{n:0{L*2}d}"[-L*2:]
    return [int(s[k]) << 4 | int(s[k+1]) for k in range(0, L*2, 2)]

class _FakeMachine:
    BALL, PLAYER, PCOUNT, CRED, SCORE, HS, AUD = 0x25E, 0x25D, 0x25F, 0x1546, 0x154C, 0x1490, 0x1800
    def __init__(self, rng):
        self.rng = rng
        self.m = _np.array([rng.randrange(256) for _ in range(8192)], dtype=_np.uint8)
        self.m[0x200:0x300] = 0
        self.m[self.BALL] = 0; self.m[self.PLAYER] = 0; self.m[self.PCOUNT] = 0
        self.credits = 0; self.coins = 0; self.scores = [0]*4
        for i, v in enumerate([2400000000, 2100000000, 1950000000, 1800000000, 1650000000]):
            self.m[self.HS + i*5: self.HS + i*5 + 5] = _mfbcd(v, 5)
        self.cpc = 2
        self.write()
    def write(self):
        self.m[self.CRED] = _mfbcd(min(self.credits, 99), 1)[0]
        self.m[self.AUD] = self.coins % 256
        for p in range(4):
            self.m[self.SCORE + p*5: self.SCORE + p*5 + 5] = _mfbcd(self.scores[p], 5)
        for k in range(6):                       # noisy bytes: timers etc
            self.m[0x100 + k] = self.rng.randrange(256)
    def read_nvram(self): return self.m.tobytes()
    def cpu_region(self, start=0, size=None):
        mem = bytes(0x4000) + self.m.tobytes()
        return mem[start:] if size is None else mem[start:start + size]

def _mf_run(out, seed=1, games=6, mix=True):
    rng = _random.Random(seed)
    M = _FakeMachine(rng)
    rec = memfind.Recorder(M, out, 'fake')
    t = 0
    def tick(phase, game_on, dt=500):
        nonlocal t
        for _ in range(max(1, dt // 500)):
            t += 500; M.write(); rec.tick(t, phase, game_on)
    for _ in range(10): tick("boot", False)
    for g in range(games):
        players = rng.choice([1, 2, 3]) if mix else 1
        rec.add_events([(t + 100, "before_coins", {})]); tick("start", False)
        n = players * M.cpc + rng.randint(0, 3)
        for i in range(n):
            M.coins += 1
            if M.coins % M.cpc == 0: M.credits += 1
            rec.add_events([(t + 100, "coin", {"n": i+1, "of": n})]); tick("start", False)
        M.credits -= players
        M.scores = [0]*4
        M.m[M.PCOUNT] = players; M.m[M.PLAYER] = 1; M.m[M.BALL] = 1
        rec.add_events([(t + 100, "started", {"players": players})]); tick("await", True)
        for ball in range(1, 4):
            for p in range(1, players + 1):
                M.m[M.BALL] = ball; M.m[M.PLAYER] = p
                for save in range(2 if rng.random() < 0.2 else 1):
                    rec.add_events([(t + 100, "serve", {})]); tick("play", True)
                    for _ in range(rng.randint(3, 8)):
                        M.scores[p-1] += rng.choice([10, 100, 1000, 25000]); tick("play", True, 2000)
                    rec.add_events([(t + 100, "drain", {})]); tick("await", True)
        M.m[M.PLAYER] = 0
        for _ in range(10): tick("start", False)
    return rec.save()



def test_memfind_on_simulated_machine():
    out = tempfile.mkdtemp()
    p = _mf_run(out, seed=2, games=5, mix=True)
    m, rep = memfind.find_map(memfind.load_runs([p]))
    gs = m["game_state"]
    # the fake machine keeps its .nv memory at 0x4000 in CPU memory: the map uses live addresses
    assert gs["current_ball"]["start"] == "0x425e", rep
    assert gs["current_player"]["start"] == "0x425d", rep
    assert gs["player_count"]["start"] == "0x425f", rep
    assert gs["credits"]["start"] == "0x5546", rep
    assert gs["scores"][0]["start"] == "0x554c", rep
    assert m["high_scores"][0]["score"]["start"] == "0x5490" and len(m["high_scores"]) == 5, rep
    assert m["_harvester"]["live_addresses"] is True


if __name__ == "__main__":
    test_memfind_on_simulated_machine()
    test_one_switch_trough_holds_several_balls()
    test_scriptgen_kicker_trough_and_ballstack_outhole()
    tmp = tempfile.mkdtemp()
    try:
        test_quantize()
        test_mask_collapses_scores()
        test_rule_collapses_only_matching_screens()
        test_learn_digits()
        test_drop_targets_stay_down_until_reset()
        test_ball_count()
        test_service_strategy()
        test_rom_only_game_file()
        test_focus_bursts_happen()
        test_slam_tilt_ends_long_games()
        test_serve_pulse()
        test_holes_hold_balls_until_coil()
        test_stray_balls_recovered_before_new_game()
        test_low_scores_keeps_checksums_valid(tmp)
        test_roundtrip(tmp)
        test_store_and_merge(tmp)
        test_fuzz_schedule()
        test_no_start_between_balls()
        test_wheel_turns_while_motor_on()
        test_clean_drain_touches_nothing()
        test_recipes_run()
        test_two_stage_trough_and_toy()
        test_game_file_from_script(tmp)
        test_outhole_holds_ball_until_coil()
        print("All offline tests passed.")
    finally:
        shutil.rmtree(tmp)
