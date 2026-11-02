#!/usr/bin/env python3
"""vpin-frame-harvester CLI.

  harvest.py run   --rom ROM --game GAME.json --out DIR [--strategy fuzz] [--minutes N] [--seed S]
  harvest.py merge DIR [DIR ...] --out DIR --game GAME.json
      (also re-sorts an old dump with the current game file: give one folder)
  harvest.py learn-digits DIR [DIR ...] --game GAME.json
      (learns the game's number digits so live numbers don't multiply frames)
  harvest.py forget-digit N [N ...] --game GAME.json
      (removes shapes from the learned digits by their number on the picture)
  harvest.py compare DIR [DIR ...] --crom GAME.cRom [--crp GAME.cRP] --out DIR
      (checks captured frames against a finished Serum project: coverage,
       missed project frames, and captured frames Serum wouldn't recognise)
  harvest.py low-scores --rom ROM --game GAME.json
      (lowers the saved high-score table so auto-play reaches ENTER INITIALS)
"""
from __future__ import annotations

import argparse
import queue
import sys
import time

from harvester.driver import STRATEGIES, GameMap
from harvester.frames import Frame, normalize
from harvester.store import FrameStore, merge


class _Tee:
    """Print to the console and append to <out>/log.txt."""
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.con = sys.stdout

    def write(self, s):
        self.con.write(s)
        self.con.flush()                     # the app reads this live, line by line
        self.f.write(s)
        self.f.flush()

    def flush(self):
        self.con.flush()
        self.f.flush()


def cmd_run(a):
    import json
    import os
    from harvester.pinmame import PinMame   # only needed when emulating
    os.makedirs(a.out, exist_ok=True)
    sys.stdout = _Tee(os.path.join(a.out, "log.txt"))
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(sys.argv[1:])}")

    game = GameMap.load(a.game)
    if game.roms and a.rom not in game.roms and a.strategy != "attract":
        sys.exit(f"{a.game} is for {', '.join(game.roms)}, not {a.rom}. Its switch numbers would be "
                 f"wrong for this table. Pick the right game file (or use attract mode).")
    if getattr(a, "attract_first", False):  # long attract show before every game
        game.attract_first_s = 120
        game.attract_between_s = (60, 120)
    if a.missing_ball:                       # boot one ball short: "BALL MISSING" screens
        game.balls = (game.balls if game.balls is not None else len(game.trough)) - 1
    strat = STRATEGIES[a.strategy](game, a.seed)
    if a.players and hasattr(strat, "fixed_players"):
        strat.fixed_players = max(1, min(4, a.players))
    store = FrameStore(a.out, game.masks, game.levels or 4, png=not a.no_png, rules=game.rules,
                       digits=game.digit_shapes)
    pm = PinMame()

    print(f"Starting {a.rom} ({game.name}) with '{a.strategy}', seed {a.seed}, {a.minutes} min")
    nv_backup = None
    import os, shutil
    nv = os.path.join(os.environ.get("PINMAME_PATH", ""), "nvram", a.rom + ".nv")
    if (a.strategy == "service" or a.free_play) and os.path.exists(nv):
        nv_backup = nv + ".harvester-backup"
        shutil.copy2(nv, nv_backup)
        print(f"[settings] backed up {nv} - it will be restored at the end")
    if a.free_play:
        import json
        from harvester.nvram import set_bytes
        mp = os.path.join(os.path.dirname(game.path), game.nvram_map or "")
        fp = json.load(open(mp)).get("game_state", {}).get("free_play") if game.nvram_map else None
        if fp and os.path.exists(nv):
            print("[settings] free play on: " + set_bytes(nv, mp, int(str(fp["start"]), 0), b"\x01"))
        else:
            print("[settings] free play: no free_play location in the memory map (or no .nv yet)")
    pm.start(a.rom)
    for sw, closed in strat.init():
        pm.set_switch(sw, closed)

    gstate = None
    if game.nvram_map:
        import os
        from harvester.gamestate import GameState
        mp = os.path.join(os.path.dirname(game.path), game.nvram_map)
        if os.path.exists(mp):
            roms = json.load(open(mp)).get("_metadata", {}).get("roms", [])
            if roms and a.rom not in roms:
                # Memory addresses differ per ROM: using another game's map would
                # read nonsense and, worse, WRITE into the wrong places.
                print(f"[game] the memory map {game.nvram_map} is for {', '.join(roms)}, not {a.rom} - "
                      f"not reading or changing game memory")
            else:
                gstate = GameState(mp)
    lowered = gstate is None or not a.low_scores
    if gstate is not None and "credits" in gstate.fields and not a.free_play:
        strat.zero_credits_chance = 0.3
    sol_prev = {n: False for n in strat.watch_solenoids}
    fired: set[int] = set()                  # coils the game used since the last start attempt
    baseline: set[int] = set()               # ... and the ones it uses in attract mode anyway
    all_prev = [False] * 65
    last_coil_scan = 0
    end = time.monotonic() + a.minutes * 60
    last_report = last_flush = last_state = last_live = time.monotonic()
    last_frame = None
    warned_dark = False
    live_path = os.path.join(a.out, "_live.bin")
    try:
        stop_path = os.path.join(a.out, "_stop")
        if os.path.exists(stop_path):
            os.remove(stop_path)
        while time.monotonic() < end:
            if os.path.exists(stop_path):        # the app's Stop button
                os.remove(stop_path)
                print("Stop requested, saving...")
                break
            now = pm.now_ms()
            rises = pm.pop_rises()                   # every coil pulse, however short
            if pm.sol_events:
                for n in rises:
                    fired.add(n)
                    if n in sol_prev:
                        strat.on_solenoid(n, now)
            else:                                    # older libpinmame: poll the coils
                for n in sol_prev:
                    on = pm.solenoid_on(n)
                    if on and not sol_prev[n]:
                        strat.on_solenoid(n, now)
                    sol_prev[n] = on
            if getattr(strat, "request_zero_credits", False):
                strat.request_zero_credits = False
                if gstate is not None and gstate.set_credits(pm, 0):
                    print(f"  {now // 1000:6d}s  [game] credits set to 0")
            if strat.motors:                         # motor-driven toys and wheels
                strat.coils({n: pm.solenoid_on(n) for n in strat.motors}, now)
            for sw, closed in strat.due(now):
                pm.set_switch(sw, closed)

            new = 0
            try:
                while True:
                    raw = pm.frames.get(timeout=0.005)
                    levels = game.levels or (1 << raw.depth)    # depth 2 -> 4, 4 -> 16
                    store.levels = levels
                    f = normalize(raw.pixels, raw.width, raw.height, raw.t_ms, levels, raw_mode=True)
                    new += store.add(f, tag=getattr(strat, "phase", ""))
                    last_frame = f
            except queue.Empty:
                pass
            strat.reward(new)
            if last_frame is None and not warned_dark and now > 20000:
                warned_dark = True
                print(f"  {now // 1000:6d}s  No DMD picture yet. "
                      + ("PinMAME reported no DMD for this ROM - is it an alphanumeric (number/letter) "
                         "display game?" if not pm.got_display else
                         "The ROM is running but hasn't drawn anything - it may be stuck at a "
                         "start-up check. Try Create NVRAM, or attract mode for a minute."))
            if pm.lib.PinmameIsRunning() == 0:
                print(f"  {now // 1000:6d}s  PinMAME stopped by itself"
                      + (f" - it said: {'; '.join(pm.messages[-4:])}" if pm.messages else ""))
                break
            if not pm.sol_events and now - last_coil_scan >= 30:   # which coils fire tells us if a game began
                last_coil_scan = now
                for n in range(1, 65):
                    on = pm.solenoid_on(n)
                    if on and not all_prev[n]:
                        fired.add(n)
                    all_prev[n] = on
            for msg in strat.log:
                if msg.startswith("no ball served"):
                    new_coils = sorted(fired - baseline)
                    print(f"  {now // 1000:6d}s  coils used after Start that attract mode doesn't use: "
                          + (", ".join(map(str, new_coils)) if new_coils else
                             "none - the game probably didn't start (coin or Start not accepted?)"))
                if msg.startswith("start attempt"):
                    baseline, fired = set(fired), set()
                print(f"  {now // 1000:6d}s  {msg}")
            strat.log.clear()

            t = time.monotonic()
            if last_frame is not None and t - last_live > 0.1:
                # latest DMD frame for the app's live preview: w, h (uint16), levels, pixels
                try:
                    tmp = live_path + ".tmp"
                    with open(tmp, "wb") as fh:
                        import struct
                        fh.write(struct.pack("<HHB", last_frame.width, last_frame.height,
                                             min(255, store.levels)) + last_frame.pixels)
                    os.replace(tmp, live_path)
                except OSError:
                    pass
                last_live = t
            if gstate is not None and not lowered and now > game.boot_s * 1000:
                # After boot, so the game has loaded its saved table: show it,
                # then lower it in live memory (what the game really compares
                # against). The game saves the lowered table when it exits.
                lowered = True
                print(f"  {now // 1000:6d}s  [game] high-score table: {gstate.high_scores(pm)}")
                if gstate.has_checksum_over_scores():
                    print(f"  {now // 1000:6d}s  [game] high scores are checksummed - use the low-scores command instead")
                elif gstate.lower_high_scores(pm):
                    print(f"  {now // 1000:6d}s  [game] lowered to: {gstate.high_scores(pm)}")
                else:
                    print(f"  {now // 1000:6d}s  [game] could not lower the high-score table in memory")
            if gstate is not None and t - last_state > 1:      # fresh game state every second
                prev = strat.game_state
                strat.game_state = gstate.read(pm)
                last_state = t
                key = lambda st: (st.get("game_over"), st.get("current_player"), st.get("current_ball"))
                if prev and key(prev) != key(strat.game_state):      # log every change straight away
                    print(f"  {now // 1000:6d}s  [game] now {gstate.describe(strat.game_state)}")
                    if strat.game_state.get("game_over"):
                        print(f"  {now // 1000:6d}s  [game] high-score table: {gstate.high_scores(pm)}")
                    elif a.low_scores and lowered and gstate.table_full(pm) \
                            and not gstate.has_checksum_over_scores() and gstate.lower_high_scores(pm):
                        # the game put its factory table back (or it filled up) - lower it again
                        print(f"  {now // 1000:6d}s  [game] high-score table was back up - lowered again")
            if t - last_report > 10:
                extra = ""
                if gstate is not None:
                    extra = "  [game] " + gstate.describe(strat.game_state)
                print(f"  {now // 1000:6d}s  unique={len(store):5d}  dropped={pm.dropped}{extra}")
                last_report = t
            if t - last_flush > 30:
                store.flush()
                last_flush = t
    except KeyboardInterrupt:
        print("Interrupted, saving...")
    finally:
        pm.stop()
        store.close()
        if nv_backup:
            import shutil
            time.sleep(1.0)                       # let PinMAME finish writing NVRAM
            shutil.copy2(nv_backup, nv_backup[:-len(".harvester-backup")])
            print("[settings] game settings restored")
    print(f"Done: {len(store)} unique frames in {a.out}")


def cmd_merge(a):
    game = GameMap.load(a.game)
    r = merge(a.sources, a.out, game.masks, game.levels or 4, rules=game.rules,
              digits=game.digit_shapes)
    print(f"Read {r['read']} frames from {len(a.sources)} source(s): {r['unique']} unique. "
          f"Report: {r['report']}")


def cmd_learn_digits(a):
    import os
    from harvester import glyphs
    from harvester.export import read_txt
    game = GameMap.load(a.game)
    frames = []
    for src in a.sources:
        frames += read_txt(src if src.endswith(".txt") else os.path.join(src, "frames.txt"))
    before = len(game.digit_shapes)
    digits = glyphs.learn(frames, known=game.digit_shapes)
    path = game.digits_path()
    glyphs.save(digits, path)
    png = os.path.splitext(path)[0] + ".png"
    glyphs.sheet(digits, png)
    print(f"Learned {len(digits)} digit shapes ({len(digits) - before} new) from {len(frames)} frames.")
    print(f"Saved to {path}")
    print(f"Check the picture {png} - it should show ONLY numbers.")


def cmd_forget_digit(a):
    """Remove learned shapes that aren't digits (by their number on the sheet)."""
    import os
    from harvester import glyphs
    game = GameMap.load(a.game)
    gl = glyphs.ordered(game.digit_shapes)
    drop = {n - 1 for n in a.numbers if 1 <= n <= len(gl)}
    keep = {g for i, g in enumerate(gl) if i not in drop}
    path = game.digits_path()
    glyphs.save(keep, path)
    png = os.path.splitext(path)[0] + ".png"
    glyphs.sheet(keep, png)
    print(f"Removed {len(drop)} shape(s); {len(keep)} left. New picture: {png}")
    print("Re-run merge to apply it to your dumps.")


def cmd_compare(a):
    """Check captured frames against a finished Serum/ColorizingDMD project."""
    import os
    import numpy as np
    from harvester import serum
    from harvester.export import read_txt, write_frame
    proj = serum.load(a.crom, a.crp)
    frames = []
    for src in a.sources:
        frames += read_txt(src if src.endswith(".txt") else os.path.join(src, "frames.txt"))
    os.makedirs(a.out, exist_ok=True)
    seen = np.zeros(proj.n, bool)
    unknown = []
    for f in frames:
        ids = proj.identify(f)
        if ids:
            seen[ids] = True
        else:
            unknown.append(f)
    with open(os.path.join(a.out, "unrecognised.txt"), "w", encoding="ascii") as fh:
        for f in unknown:
            write_frame(fh, f)
    missed = [i for i in range(proj.n) if not seen[i]]
    pct = 100.0 * seen.sum() / max(1, proj.n)
    # Frames with the same mask, shape mode and hash are ONE screen to Serum.
    sig = {}
    for i in range(proj.n):
        sig.setdefault((int(proj.mask_id[i]), int(proj.shape[i]), int(proj.hashes[i])), []).append(i)
    sig_seen = sum(1 for v in sig.values() if seen[v].any())
    spct = 100.0 * sig_seen / max(1, len(sig))
    lines = [f"Project: {proj.name}  ({proj.n} frames)",
             f"Captured frames checked: {len(frames)}",
             f"Project frames reached by the harvester: {int(seen.sum())} of {proj.n} ({pct:.1f}%)",
             f"Distinct screens (as Serum sees them):   {sig_seen} of {len(sig)} ({spct:.1f}%)",
             f"Project frames NOT reached: {len(missed)}  -> missed.png",
             f"Captured frames not in the final project: {len(unknown)}  -> unrecognised.txt / unrecognised.png",
             "  (most are frames the author dropped on purpose; review them for real misses)"]
    open(os.path.join(a.out, "compare.txt"), "w").write("\n".join(lines) + "\n")
    with open(os.path.join(a.out, "missed.txt"), "w") as fh:
        fh.write("\n".join(str(i) for i in missed) + "\n")
    if proj.originals is not None and missed:
        # one picture per missed screen (not per duplicate project frame)
        firsts, done = [], set()
        for i in missed:
            k = (int(proj.mask_id[i]), int(proj.shape[i]), int(proj.hashes[i]))
            if k not in done:
                done.add(k)
                firsts.append(i)
        pick = firsts[:400]
        serum.sheet([proj.originals[i] for i in pick],
                    [f"#{i}" + (" (mask)" if proj.mask_id[i] != 255 else "") for i in pick],
                    os.path.join(a.out, "missed.png"),
                    masks=[proj.masks[proj.mask_id[i]] if proj.mask_id[i] != 255 else None
                           for i in pick])
    if proj.originals is not None and missed and frames:
        # For each missed screen, the closest thing the harvester DID capture:
        # a few pixels apart = reached but slightly different; far apart = never reached.
        same = [f for f in frames if (f.width, f.height) == (proj.width, proj.height)]
        cap = np.stack([np.frombuffer(f.pixels, np.uint8).reshape(f.height, f.width) for f in same])
        pick = firsts[:300]
        pm = [proj.masks[proj.mask_id[i]] if proj.mask_id[i] != 255 else None for i in pick]
        near = serum.nearest([proj.originals[i] for i in pick], pm, cap)
        imgs, labs, msk = [], [], []
        fh2 = open(os.path.join(a.out, "nearest_frames.txt"), "w", encoding="ascii")
        with open(os.path.join(a.out, "nearest.txt"), "w") as fh:
            for i, m, (j, n) in zip(pick, pm, near):
                po, cj = proj.originals[i], cap[j]
                diff = (po != cj) & ((m == 0) if m is not None else True)
                kinds = {}
                for pv, cv in zip(po[diff].tolist(), cj[diff].tolist()):
                    kinds[(pv, cv)] = kinds.get((pv, cv), 0) + 1
                top = ", ".join(f"shade {pv}->{cv}: {k}" for (pv, cv), k in
                                sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
                fh.write(f"#{i}: closest captured frame differs by {n} pixels  ({top})\n")
                if n <= 300:      # pair of frames (project, then captured) for close look
                    write_frame(fh2, Frame(i, proj.width, proj.height, po.astype(np.uint8).tobytes()))
                    write_frame(fh2, Frame(j, proj.width, proj.height, cj.astype(np.uint8).tobytes()))
                imgs += [proj.originals[i], cap[j]]
                labs += [f"#{i} project", f"closest: {n} px differ"]
                msk += [m, m]
        fh2.close()
        # Sparkle effects (text glittering between bright and dim) are random
        # per frame, so nobody captures the author's exact frames twice. Count
        # missed screens whose lit SHAPE was captured exactly (shade aside).
        all_missed = firsts
        amask = [proj.masks[proj.mask_id[i]] if proj.mask_id[i] != 255 else None for i in all_missed]
        shp = serum.nearest([(proj.originals[i] > 0).astype(np.uint8) for i in all_missed], amask,
                            (cap > 0).astype(np.uint8))
        same_shape = [i for i, (j, n) in zip(all_missed, shp) if n == 0]
        with open(os.path.join(a.out, "same_shape.txt"), "w") as fh:
            fh.write("\n".join(f"#{i}" for i in same_shape) + "\n")
        n_ss = sum(len(v) for k, v in sig.items() if v[0] in set(same_shape) or any(x in same_shape for x in v))
        lines.insert(4, f"  + same screen, only the shade/sparkle differs: {len(same_shape)} screens "
                        f"({n_ss} frames) -> same_shape.txt")
        lines.insert(5, f"  = reached allowing for sparkle: {int(seen.sum()) + n_ss} of {proj.n} "
                        f"({100.0 * (int(seen.sum()) + n_ss) / max(1, proj.n):.1f}%)")
        serum.sheet(imgs, labs, os.path.join(a.out, "nearest.png"), cols=4, masks=msk)
        lines.append("Closest captured frame for each missed screen -> nearest.png / nearest.txt")
    if unknown:
        step = max(1, len(unknown) // 400)
        pick = unknown[::step][:400]
        serum.sheet([np.frombuffer(f.pixels, np.uint8).reshape(f.height, f.width) for f in pick],
                    [f"{f.t_ms // 1000}s" for f in pick], os.path.join(a.out, "unrecognised.png"))
    open(os.path.join(a.out, "compare.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"Results in {a.out}")


def cmd_low_scores(a):
    import os
    from harvester.nvram import lower_high_scores
    game = GameMap.load(a.game)
    if not game.nvram_map:
        sys.exit("This game file has no nvram_map.")
    nv = os.path.join(os.environ.get("PINMAME_PATH", ""), "nvram", a.rom + ".nv")
    if not os.path.exists(nv):
        sys.exit(f"{nv} not found - run the game once (e.g. a 1-minute attract run) first.")
    mp = os.path.join(os.path.dirname(game.path), game.nvram_map)
    msg = lower_high_scores(nv, mp)
    print(msg)
    with open(os.path.join(os.path.dirname(nv), a.rom + ".low-scores-log.txt"), "a") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # live output for the app (packaged too)
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--rom", required=True)
    r.add_argument("--game", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--strategy", choices=sorted(STRATEGIES), default="fuzz")
    r.add_argument("--minutes", type=float, default=30)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--no-png", action="store_true")
    r.add_argument("--players", type=int, default=0,
                   help="always start games with this many players (default: a mix of 1-4)")
    r.add_argument("--free-play", action="store_true",
                   help="turn free play on for this run (settings restored afterwards)")
    r.add_argument("--attract-first", action="store_true",
                   help="watch 2 minutes of attract mode before the first game, and 1-2 minutes "
                        "between games (default: about 15 seconds)")
    r.add_argument("--missing-ball", action="store_true",
                   help="start one ball short, for the BALL MISSING screens")
    r.add_argument("--low-scores", action="store_true",
                   help="after boot, lower the high-score table in the running game's memory "
                        "so games end in ENTER INITIALS (needs an nvram_map)")
    r.set_defaults(fn=cmd_run)

    m = sub.add_parser("merge")
    m.add_argument("sources", nargs="+")
    m.add_argument("--out", required=True)
    m.add_argument("--game", required=True)
    m.set_defaults(fn=cmd_merge)

    d = sub.add_parser("learn-digits")
    d.add_argument("sources", nargs="+")
    d.add_argument("--game", required=True)
    d.set_defaults(fn=cmd_learn_digits)

    fd = sub.add_parser("forget-digit")
    fd.add_argument("numbers", nargs="+", type=int)
    fd.add_argument("--game", required=True)
    fd.set_defaults(fn=cmd_forget_digit)

    c = sub.add_parser("compare")
    c.add_argument("sources", nargs="+")
    c.add_argument("--crom", required=True)
    c.add_argument("--crp", default=None)
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_compare)

    ls = sub.add_parser("low-scores")
    ls.add_argument("--rom", required=True)
    ls.add_argument("--game", required=True)
    ls.set_defaults(fn=cmd_low_scores)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except SystemExit as e:                  # put the reason in log.txt too, not only on screen
        if isinstance(e.code, str):
            print(e.code)
            sys.stdout.flush()
            raise SystemExit(1)
        raise


if __name__ == "__main__":
    sys.exit(main())
