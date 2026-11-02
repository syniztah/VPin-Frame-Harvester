# Phase 2: coverage tracking

Goal: a report that says **which animations the ROM contains that you haven't
captured yet**, instead of guessing whether you've played enough.

## Idea

DMD animations live in ROM. When the game shows one, the CPU reads that ROM
data. If the emulator logs *which ROM addresses were read while drawing*, then
"addresses never read" is a map of content you haven't triggered.

```
 static pass:    ROM  ->  list of graphics regions (animation/font IDs + address ranges)
 dynamic pass:   PinMAME (patched)  ->  log of ROM reads  ->  set of touched regions
 report:         regions - touched  =  "not seen yet"   (+ which mode/deff uses them)
```

## Where to hook (WPC first)

WPC games see a 16 KB banked ROM window at `0x4000-0x7FFF`, with the bank
selected by a register (check `wpc.c` in the PinMAME source for the exact
address and handler). Graphics data sits in those banked pages.

1. In the banked-ROM read path, record `(bank, offset)` for every data read
   (not opcode fetches, if the core lets you tell them apart).
2. Keep a bitmap per bank, e.g. 1 bit per 16 bytes, so it's cheap enough to
   leave on.
3. Expose it to the harvester: write the bitmap to a file on exit, or add a
   small libpinmame export such as `PinmameGetCoverage(buffer, size)`.

Code also sits in banked pages, so the bitmap will include code reads.
That's fine: the report only compares against regions the static pass
identified as graphics.

## Static pass

You need to know which ROM ranges are graphics. Options, easiest first:

- **Empirical:** run the coverage build in attract and fuzz modes, then diff
  "regions read while the DMD changed" against "regions read at other times".
  Crude, but needs no format knowledge.
- **Format-aware:** decode the WPC graphics/animation tables directly. The
  FreeWPC project and community ROM tools document a lot of this.

## Report shape

```
Animations found in ROM:     450
Captured (data read):        412
Not yet seen:                 38
  bank 0x2c  0x5120-0x5a40   (~12 frames)
  bank 0x31  0x4000-0x47ff   (~6 frames)
  ...
```

## Phase 3 hook point

Once you know where the display-effect dispatch lives (WPC "deffs"), the same
patched core can call it directly with each effect ID while the harvester
records. That turns the "not yet seen" list into frames without having to
reach the mode legitimately.
