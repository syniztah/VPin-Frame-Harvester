"""Deduplicating frame store with an on-disk index."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .export import read_txt, save_png, write_frame
from .frames import Frame, Mask, frame_key, is_blank, rule_for


@dataclass
class Entry:
    key: str
    first_ms: int
    hits: int = 1
    order: int = 0
    tags: list[str] = field(default_factory=list)


class FrameStore:
    """Keeps the first copy of every unique frame.

    Re-using an output folder continues it (existing frames are loaded first)
    instead of mixing duplicate copies into it.
    """

    def __init__(self, out_dir: str, masks: list[Mask], levels: int,
                 png: bool = True, keep_blank: bool = False, rules=(), digits=None):
        self.out_dir = out_dir
        self.masks = masks
        self.rules = list(rules)
        self.digits = digits or set()
        self.levels = levels
        self.png = png
        self.keep_blank = keep_blank
        self.entries: dict[str, Entry] = {}
        self.last_key: str | None = None
        os.makedirs(out_dir, exist_ok=True)
        if png:
            os.makedirs(os.path.join(out_dir, "png"), exist_ok=True)
        txt = os.path.join(out_dir, "frames.txt")
        if os.path.exists(txt):
            self._resume(txt)
        self._txt = open(txt, "a", encoding="ascii")

    def _resume(self, txt: str):
        idx_path = os.path.join(self.out_dir, "index.json")
        old = json.load(open(idx_path)) if os.path.exists(idx_path) else {}
        for f in read_txt(txt):
            k = frame_key(f, self.masks, self.rules, self.digits)
            if k not in self.entries:
                self.entries[k] = Entry(k, f.t_ms, old.get(k, {}).get("hits", 1),
                                        order=len(self.entries))
        if self.entries:
            print(f"[store] continuing {self.out_dir}: {len(self.entries)} frames already there")

    def add(self, frame: Frame, tag: str = "") -> bool:
        """Returns True if the frame is new."""
        if not self.keep_blank and is_blank(frame):
            return False
        key = frame_key(frame, self.masks, self.rules, self.digits)
        if key == self.last_key:              # same frame still on screen
            return False
        self.last_key = key
        e = self.entries.get(key)
        if e:
            e.hits += 1
            return False
        e = Entry(key, frame.t_ms, order=len(self.entries))
        r = rule_for(frame, self.rules)
        for t in (tag, r.name if r else ""):
            if t:
                e.tags.append(t)
        self.entries[key] = e
        write_frame(self._txt, frame)
        if self.png:
            save_png(frame, os.path.join(self.out_dir, "png", f"{e.order:05d}_{key}.png"), self.levels)
        return True

    def flush(self):
        self._txt.flush()
        with open(os.path.join(self.out_dir, "index.json"), "w") as fh:
            json.dump({k: vars(v) for k, v in self.entries.items()}, fh, indent=1)

    def close(self):
        self.flush()
        self._txt.close()

    def __len__(self):
        return len(self.entries)


def merge(sources: list[str], out_dir: str, masks: list[Mask], levels: int, rules=(),
          digits=None) -> dict:
    """Merge worker output folders (or frames.txt files) into one clean store.

    Also used to re-sort an existing dump after the game file's masks or
    rules change - no need to replay anything.
    """
    if os.path.exists(os.path.join(out_dir, "frames.txt")):
        raise SystemExit(f"{out_dir} already has frames - pick a new folder name for the merged output.")
    merged = FrameStore(out_dir, masks, levels, png=True, rules=rules, digits=digits)
    total_in = 0
    hits: dict[str, int] = {}
    for src in sources:
        txt = src if src.endswith(".txt") else os.path.join(src, "frames.txt")
        frames = read_txt(txt)
        total_in += len(frames)
        for f in frames:
            merged.last_key = None             # don't collapse by adjacency
            merged.add(f)
            k = frame_key(f, masks, rules, digits)
            if k in merged.entries:
                hits[k] = hits.get(k, 0) + 1
    for k, n in hits.items():
        merged.entries[k].hits = n
    merged.close()

    by_rule: dict[str, int] = {}
    for e in merged.entries.values():
        for t in e.tags:
            by_rule[t] = by_rule.get(t, 0) + 1
    report = os.path.join(out_dir, "report.txt")
    with open(report, "w") as fh:
        fh.write(f"Frames read:   {total_in}\n")
        fh.write(f"Unique frames: {len(merged)}\n")
        for name in sorted(by_rule):
            fh.write(f"  of which '{name}': {by_rule[name]}\n")
    return {"read": total_in, "unique": len(merged), "report": report}
