# vpin-frame-harvester

by Syniztah

Automatically play a pinball ROM in PinMAME, capture every **unique** DMD frame,
and export them in a dump format your colorizing tool can import, so you're not
relying on hours of manual play.

> Status: skeleton. The offline parts (dedupe, masking, export, merge) are
> working and tested. The emulator binding (`harvester/pinmame.py`) and the game
> switch maps need checking against your libpinmame build and each table.

---

## Architecture

```
            +-------------------- one process per worker ----------------------+
            |                                                                   |
 game.json  |  Driver (strategy)  --switch events-->  libpinmame (emulation)     |
 (switches, |     ^                                        |                     |
  masks)    |     | novelty feedback                       | OnDisplayUpdated    |
            |     |                                        v                     |
            |  FrameStore  <-- dedupe (masked hash) <-- frame queue (copy)       |
            |     |                                                             |
            +-----|-------------------------------------------------------------+
                  v
        out/worker-N/  index.json, frames.txt, png/      --merge-->  out/merged/
                                                                     frames.txt
                                                                     report.txt
```

**1. Emulator layer** (`harvester/pinmame.py`)
Thin ctypes wrapper around libpinmame. It starts a ROM, sets switches, and
copies each DMD frame from the display callback into a thread-safe queue.
libpinmame is a single emulator per process, so parallelism comes from running
several worker processes with different random seeds.

**2. Driver** (`harvester/driver.py`)
Decides which switches to hit and when. Strategies:
- `attract`: start nothing; record attract mode, high scores and similar.
- `service`: explore the service/test menus. With a known machine type (`system` in
  the game file, or guessed from `service_buttons`) it first tours the menus with that
  system's buttons (WPC/SAM/Whitestar/Alvin G.: enter, up, down, back; Data East and
  System 11: advance and direction; GTS3: diagnostic; Capcom: the diagnostic switch),
  then explores with the service buttons
  (`service_buttons` in the game file), flippers and Start. No game is
  played. The game's NVRAM is backed up first and restored afterwards,
  because menus can change settings.
- `fuzz`: start a game, keep the trough and coin door in a sane state, pulse
  playfield switches at random, flip, drain, start new games.
- **Author-style bursts**: now and then, hit one shot 3-10 times in a row, or
  sweep a bank of consecutively numbered switches in order. Random single
  hits rarely complete the sequences deeper modes need.
- **Realism extras**: occasional tilt nudges (DANGER/TILT screens), quick
  drains (ball save screens), long flipper holds (instant info), varied coin
  counts (half-credit screens).
- **Novelty weighting**: switches that recently led to a *new* frame get picked
  more often. That's a cheap version of coverage-guided fuzzing, and it tends
  to push the game deeper into modes.

**3. Dedupe** (`harvester/frames.py`, `harvester/store.py`)
Each frame is quantized (e.g. 4 or 16 shades), the **mask rectangles** from the
game config are blanked (score area, ball number, timers), and the result is
hashed. Frames that differ only inside a mask count as the same frame, so you
don't get 40,000 copies of the base score screen. The store keeps the first
copy plus a hit count.

**Screen rules** (`rules` in the game file)
Some screens, like the score screen, show up hundreds of times with only the
numbers changed. A rule names a few exact picture fragments (e.g. the words
BALL and CREDITS) and the areas to ignore when all of them are on screen,
like a Serum mask. `layout_cell` keeps one example per rough layout (1
player, 2 players with player 1 up, ...). Animations drawn over the score
usually cover the trigger words or change outside the masks, so they're kept.

**Learned digits** (`harvest.py learn-digits`)
Animations often carry a live number (score, timer, value), and every
different number used to make a "new" frame. `learn-digits` looks through
captured frames for small shapes that get swapped in place, the way digits
tick over, and saves them to `games/<game>.digits.json` with a picture
(`.digits.png`) to check: it should show only numbers. After that, those
shapes are ignored when deciding what's new. The learner is strict on
purpose: a shape must be swapped at 2+ spots on screen, with 2+ same-height
partners, in a family of 5+ same-height shapes. On real dumps that keeps all
of Harley-Davidson's digits and rejects every shape from Rescue 911's attract
animations (which a looser version mistook for 38 "digits").

**4. Export** (`harvester/export.py`)
Writes unique frames as a text dump: a hex timestamp line, then one line per
row with one hex digit per pixel. **Check this against a dump your tool already
accepts** before relying on it; it's a small function to adjust.

**5. Merge and re-sort** (`harvest.py merge`)
Combines worker outputs and dedupes across them. With a single folder it
re-sorts an existing dump using the current game file, so after changing
masks or rules you don't have to replay anything. Re-using a `run` output
folder continues it rather than mixing in duplicates.

**6. Compare with a finished colourisation** (`harvest.py compare`)
Reads a ColorizingDMD project (`.cRom` for hashes and comparison masks,
`.cRP` for the original frames) and checks captured frames the same way
Serum identifies them at runtime (CRC-32 outside the frame's mask, shape
mode = any lit pixel counts as 1). Writes `compare.txt` (coverage),
`missed.png` (project frames the harvester never reached) and
`unrecognised.txt` / `.png` (captured frames Serum would leave uncoloured,
ready to import into ColorizingDMD). The reader was checked against a real
project: all 1,450 stored hashes reproduce exactly.

---

## Roadmap

| Phase | What | Effort | Gets you |
|---|---|---|---|
| 1 | Fuzz play + dedupe + export (this skeleton) | Days to a couple of weeks | Most common frames, unattended |
| 2 | Coverage hook in the PinMAME core (`docs/coverage_hook.md`) | Weeks | "Seen 412 of 450 animations" report |
| 3 | Force display effects directly (WPC deffs first) | Weeks to months per platform | Rare and wizard-mode animations on demand |

Phase 2 is the one that changes the workflow most: it tells you what you
*haven't* captured, so you can point manual play or forced triggers at it.

---

## Getting started

1. Build or obtain **libpinmame** for your OS (it's in the PinMAME repo under
   `src/libpinmame`). Put the shared library somewhere and set `LIBPINMAME` to
   its path.
2. The binding in `harvester/pinmame.py` is checked against current PinMAME
   master: struct sizes and offsets were compared with a C build of the real
   header (copy in `docs/libpinmame.h.reference`). If your libpinmame is an
   older release, diff its `libpinmame.h` against that copy; the
   `PinmameConfig` struct is the part most likely to differ. Set
   `PINMAME_PATH` to the folder that contains your `roms` folder.
3. Copy `games/example_wpc.json`, fill in the switch numbers for your table
   (the PinMAME switch test, or the table's VPX script, will tell you), and draw
   mask rectangles over the score areas.
4. Run a short attract-mode test first to confirm frames come through:

   ```
   pip install pillow          # optional, for PNG previews
   python harvest.py run --rom t2_l8 --game games/t2.json --out out/w1 \
          --strategy attract --minutes 2
   ```

5. Then fuzz, in parallel:

   ```
   python harvest.py run --rom t2_l8 --game games/t2.json --out out/w1 --minutes 60 --seed 1 &
   python harvest.py run --rom t2_l8 --game games/t2.json --out out/w2 --minutes 60 --seed 2 &
   python harvest.py merge out/w1 out/w2 --out out/merged
   ```

6. Run the offline tests any time: `python tests/test_offline.py`

---

## Tested against real colourisations

Sharkey's Shootout, harvested blind and checked against the finished
project (1,002 frames): 53% after one 20-minute run; 71.5% after adding
attract time, initials entry, a service-menu run and a second gameplay run.
Some "misses" can't be hit by anyone (e.g. high-score screens that match
only the author's own score values).

Harley-Davidson, using the author's raw dumps (53,845 frames) and finished
Serum project (1,460 frames):

| Step | Frames left | Needed frames still present |
|---|---|---|
| Raw dumps | 53,845 | 1,292 of 1,292 |
| Exact duplicates removed | 29,765 | 100% |
| + learned digits ignored | 20,148 | 100% |
| Similarity grouping (95% alike) | 16,173 | 93.7% |
| Similarity grouping (80% alike) | 2,432 | 73.0% |

Exact dedupe plus learned digits is safe. Merging frames just because they
look alike is not: small differences are often frames the author kept. So
the harvester never does that. The rest of the reduction is the author's
job (masks in ColorizingDMD).

Tip: your own manual dumps can go through the same safe clean-up before you
import them: `learn-digits` on them, then `merge` them into one folder.

## The app (Windows)

Double-click **`Harvester.bat`**. The window follows the work in order:

- **Step 1 · Harvest.** A: choose the **table** (.vpx). The app reads its
  script, finds the ROM (and says if it's missing from your roms folder) and
  makes a game file from the script if there isn't one
  (`harvester/scriptgen.py`; a report lists anything it couldn't work out).
  A `.vbs` next to the table is used first, like VPX does. First time with a
  ROM: press **Create NVRAM (30 s)**. B: game play / attract mode / service
  menu, minutes, runs in a row, players. C: extras (lower high scores, free
  play, one ball missing). Then **Start harvesting**. The live DMD (the ROM's
  own shades, in orange), time left, unique frames, the game's player, ball
  and score, and an activity log show what it's doing.
- **Step 2 · Clean & export.** 1: add the runs. 2: learn the number digits,
  check the picture, remove stray shapes or delete the digit file. 3: build
  the cleaned dump, a `frames.txt` that imports into ColorizingDMD.
- **Optional · Compare with a finished project.** Coverage against a
  `.cRom` / `.cRP`, with the missed screens, closest captures and frames the
  author dropped.

**PinMAME setup** (top right) holds the libpinmame DLL and PinMAME folder.
The app runs `harvest.py` underneath; every command it runs is in its logs.

## Reaching ENTER INITIALS

Initials entry only appears when a score beats the high-score table, which
blind play rarely does. `harvest.py low-scores --rom ROM --game GAME.json`
lowers the saved table (in PinMAME's `.nv` file) using the community
pinball-memory-maps (the whole set, 1,589 ROMs, bundled in `games/nvram_maps`, ODbL licence), and fixes
any checksum over it. It first checks that it can reproduce the checksums
already in the file, and refuses to change anything if not. The original is
kept as `<rom>.nv.before-low-scores`.

## Game files

| File | Table | Notes |
|---|---|---|
| `games/tom.json` | Theatre of Magic (WPC) | score-screen rule, learned digits |
| `games/ngg.json` | No Good Gofers (WPC-95) | 6-ball trough, auto-launch |
| `games/sprk.json` | South Park (Whitestar) | from the table script |
| `games/harl.json` | Harley-Davidson (Whitestar) | 2 drop banks, launch button |
| `games/shrk.json` | Sharkey's Shootout (Whitestar) | 4 balls in a 5-switch trough, drop banks |
| `games/jupk.json` | Jurassic Park (Data East) | two-stage trough, T-Rex toy (turn + bend) simulated |
| `games/ft.json` | Fish Tales (WPC) | physical trough + outhole, reel position switches simulated |

Useful fields: `attract_first_s` / `attract_between_s` (time spent in
attract mode before the first game and after each game over, so game over,
match, high scores and the attract show get captured), `balls` (if fewer balls than trough switches), `drop_banks`
(targets stay down until the bank's reset coil fires), `buttons` (extra
cabinet buttons tapped now and then).

## Known hard parts

- **Ball and trough logic.** If the switches don't look like real balls
  moving, the game goes into ball search or reports missing balls. Expect to
  tune the trough and shooter settings per table.
- **Timing.** Pulses that are too short get debounced away; too long and you
  trigger "stuck switch" logic. `pulse_ms` is configurable.
- **Speed.** Emulation runs at real time by default. Running several workers
  is the easy speedup; unthrottling the core is a possible later change.
- **Masks.** Too few masks and you get thousands of near-duplicates; too many
  and you hide real differences. Start with the score area only.
