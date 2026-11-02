#!/usr/bin/env python3
"""VPin Frame Harvester - Windows app.

Step 1 Harvest: choose a table, play its ROM and capture frames.
Step 2 Clean & export: combine runs, learn score digits, build frames.txt.
Optional: compare with a finished Serum project.
Start it by double-clicking Harvester.bat. Every command it runs is
harvest.py underneath, shown in its logs.
"""
from __future__ import annotations

import glob
import json
import os
import queue
import random
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox

FROZEN = getattr(sys, "frozen", False)          # the packaged Windows app (PyInstaller)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
SETTINGS = os.path.join(HERE, "app_settings.json")
HARVEST = os.path.join(HERE, "harvest.py")
HARVEST_EXE = os.path.join(HERE, "harvest.exe")    # packaged app: the command-line part
ASSETS = os.path.join(getattr(sys, "_MEIPASS", HERE), "assets")
ICON_PNG = os.path.join(ASSETS, "icon.png")

VERSION = "1.0.0"
GITHUB_URL = ""        # your GitHub page, e.g. "https://github.com/<name>/vpin-frame-harvester"
SPLASH_MS = 1500       # how long the splash screen shows

MODES = [("Game play", "fuzz"), ("Attract mode", "attract"), ("Service menu", "service")]
MIX = "Mix (1-4)"
PLAYERS = ["1", "2", "3", "4", MIX]

# ---- look ------------------------------------------------------------------
WIN = "#23974f"        # window
PANEL = "#35b064"      # cards
SUB = "#4cc077"        # boxes inside cards
FIELD = "#effcf3"      # type-in boxes
LINE = "#1a6e3c"       # card borders
TEXT = "#111311"
MUTED = "#0f2e1d"
SOFT = "#143b26"
HEAD = "#00ae42"       # title bar
BLACK = "#111311"
GREEN = "#3fd17a"      # green on black
TABSEL = "#1c211d"
TABOFF = "#8f9892"
LIGHTBTN = "#d6f6e2"
TIPBG, TIPFG, TIPLINE = "#c8f2d8", "#0f2e1d", "#1f7a45"
DANGERBG, DANGERFG, DANGERLINE = "#fbecec", "#9b2c2c", "#d9a3a3"
OKFG = "#0b3d22"
RED = "#e01b1b"        # logo red (splash and About)

UI = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"
F = (UI, 10)
F_B = (UI, 10, "bold")
F_SMALL = (UI, 9)
F_H = (UI, 11, "bold")
F_TITLE = (UI, 14, "bold")
F_BIG = (UI, 19, "bold")
F_MONO = (MONO, 9)


def load_settings() -> dict:
    try:
        return json.load(open(SETTINGS))
    except Exception:
        return {}


def save_settings(d: dict):
    try:
        json.dump(d, open(SETTINGS, "w"), indent=1)
    except OSError:
        pass


# ---- small styled widgets ---------------------------------------------------
def btn(parent, text, cmd, kind="light", big=False, width=None):
    colours = {"dark": (BLACK, GREEN, "#2a302b"), "light": (LIGHTBTN, TEXT, "#c2efd3"),
               "outline": (PANEL, TEXT, SUB), "danger": (DANGERBG, DANGERFG, "#f6dada"),
               "head": (BLACK, GREEN, "#2a302b")}
    bg, fg, act = colours[kind]
    solid = kind not in ("dark", "head")      # a drawn 1px border (Windows ignores highlight borders)
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=act,
                  activeforeground=fg, relief="solid" if solid else "flat", bd=1 if solid else 0,
                  cursor="hand2", overrelief="solid" if solid else "flat",
                  font=(UI, 12, "bold") if big else (F_B if kind in ("dark", "head") else F),
                  padx=16 if big else 12, pady=10 if big else 5, highlightthickness=0,
                  disabledforeground="#6b7a70")
    if width:
        b.configure(width=width)
    return b


def card(parent, bg=PANEL):
    return tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=LINE, bd=0)


def label(parent, text="", bg=PANEL, fg=TEXT, font=F, **kw):
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=font, anchor="w", justify="left", **kw)


def entry(parent, var, width=30, mono=False):
    return tk.Entry(parent, textvariable=var, width=width, bg=FIELD, fg=TEXT, relief="flat",
                    insertbackground=TEXT, font=F_MONO if mono else F, highlightthickness=1,
                    highlightbackground=TIPLINE, highlightcolor=BLACK)


def check(parent, text, var, bg=PANEL, cmd=None, fg=TEXT):
    return tk.Checkbutton(parent, text=text, variable=var, bg=bg, fg=fg, activebackground=bg,
                          activeforeground=fg, selectcolor=FIELD, font=F, anchor="w",
                          highlightthickness=0, bd=0, command=cmd)


def badge(parent, text, bg=PANEL, filled=True, size=24):
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    if filled:
        c.create_oval(1, 1, size - 1, size - 1, fill=BLACK, outline=BLACK)
        c.create_text(size // 2, size // 2, text=text, fill=GREEN, font=(UI, 9, "bold"))
    else:
        c.create_oval(2, 2, size - 2, size - 2, outline=GREEN, width=1.5)
        c.create_text(size // 2, size // 2, text=text, fill=GREEN, font=(UI, 9, "bold"))
    return c


def tip(parent, text, wrap=640):
    f = tk.Frame(parent, bg=TIPBG, highlightthickness=1, highlightbackground=TIPLINE)
    tk.Label(f, text="i", bg=TIPLINE, fg=TIPBG, font=(UI, 9, "bold"), width=2).pack(side="left", padx=(8, 6), pady=8, anchor="n")
    tk.Label(f, text=text, bg=TIPBG, fg=TIPFG, font=F, wraplength=wrap, justify="left",
             anchor="w").pack(side="left", fill="x", expand=True, pady=6, padx=(0, 8))
    return f


def log_box(parent, height=10):
    t = tk.Text(parent, height=height, state="disabled", font=F_MONO, wrap="none", bg=BLACK,
                fg=GREEN, insertbackground=GREEN, relief="flat", highlightthickness=1,
                highlightbackground=LINE, padx=10, pady=8)
    return t


class Runner:
    """Runs harvest.py commands one after another, streaming their output."""

    def __init__(self, app: "App"):
        self.app = app
        self.proc: subprocess.Popen | None = None
        self.queue: list[tuple[list[str], str | None]] = []
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.current_out = None
        self.total_jobs, self.job_no = 0, 0
        self.job_started, self.job_minutes = None, 0.0

    @property
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, jobs):
        self.queue = list(jobs)
        self.total_jobs, self.job_no = len(jobs), 0
        self._next()

    def _next(self):
        if not self.queue:
            self.proc = None
            self.current_out = None
            self.lines.put("\n[app] all done\n")
            return
        args, out = self.queue.pop(0)
        self.current_out = out
        self.job_no += 1
        self.job_started = time.monotonic()
        self.job_minutes = float(args[args.index("--minutes") + 1]) if "--minutes" in args else 0.0
        env = dict(os.environ)
        env["LIBPINMAME"] = self.app.v_dll.get()
        env["PINMAME_PATH"] = self.app.v_pm.get()
        env["PYTHONUNBUFFERED"] = "1"
        if FROZEN:
            cmd = [HARVEST_EXE] + args
        else:
            cmd = [sys.executable.replace("pythonw", "python"), "-u", HARVEST] + args
        self.lines.put("\n> harvest.py " + " ".join(args) + "\n")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     creationflags=flags)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.lines.put(line)
        proc.wait()
        self.lines.put("\x00EXIT")   # the GUI thread starts the next job (Tk isn't thread-safe)

    def stop(self):
        """Ask the running harvest to stop and save (it checks for this file)."""
        self.queue.clear()
        if self.busy and self.current_out:
            try:
                open(os.path.join(HERE, self.current_out, "_stop"), "w").close()
            except OSError:
                self.proc.terminate()
        elif self.busy:
            self.proc.terminate()


class App(tk.Tk):
    def __init__(self):
        if sys.platform == "win32":                   # own taskbar icon instead of Python's
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Syniztah.VPinFrameHarvester")
            except Exception:
                pass
        super().__init__()
        self.withdraw()                               # hidden until the splash is done
        self._splash_until = time.time() + SPLASH_MS / 1000
        self._splash = self._show_splash()
        self.title("VPin Frame Harvester by Syniztah")
        self.geometry("1180x800")
        self.minsize(1100, 760)
        self.configure(bg=WIN)
        try:
            self._icon = tk.PhotoImage(file=ICON_PNG)
            self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.s = load_settings()
        self.runner = Runner(self)
        self.live_img = None
        self.live_mtime = 0
        self.report: list[str] = []
        self.done = {"harvest": False, "clean": False}
        self._clean_target = None
        self._built_out = None
        self._cmp_out = None
        self.src_paths: list[str] = []

        self.v_dll = tk.StringVar(value=self.s.get("dll", r"C:\libpinmame\pinmame64.dll"))
        self.v_pm = tk.StringVar(value=self.s.get("pinmame", r"C:\libpinmame"))
        self.v_pm_info = tk.StringVar(value="")
        # Table, ROM and game file always start empty: they come from the table you choose
        self.v_table = tk.StringVar(value="")
        self.v_rom = tk.StringVar(value="")
        self.v_game = tk.StringVar(value="")
        self.v_out = tk.StringVar(value="")
        self.v_mode = tk.StringVar(value="fuzz")
        self.v_min = tk.IntVar(value=20)
        self.v_runs = tk.IntVar(value=1)
        self.v_players = tk.StringVar(value="1")
        self.v_low = tk.BooleanVar(value=True)
        self.v_free = tk.BooleanVar(value=False)
        self.v_missing = tk.BooleanVar(value=False)
        self.v_attract_first = tk.BooleanVar(value=False)
        self.v_show = tk.BooleanVar(value=self.s.get("show_dmd", True))
        self.v_showlog = tk.BooleanVar(value=self.s.get("show_log", True))
        self.v_crom = tk.StringVar(value=self.s.get("crom", ""))
        self.v_crp = tk.StringVar(value=self.s.get("crp", ""))
        self.v_clean_out = tk.StringVar(value="")
        self.v_forget = tk.StringVar()
        self.v_unique = ""

        self._build_header()
        self._build_nav()
        self.pages = tk.Frame(self, bg=WIN)
        self.pages.pack(fill="both", expand=True)
        self.page = {}
        self._build_harvest()
        self._build_clean()
        self._build_compare()
        self.show_tab("harvest")

        self.v_pm.trace_add("write", self._pm_changed)
        self.v_rom.trace_add("write", self._rom_changed)
        self._last_rom = None
        self._pm_changed()
        self._refresh_table_card()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        wait = max(0, int((self._splash_until - time.time()) * 1000))
        self.after(wait, self._end_splash)

    # ---------------------------------------------------------------- splash & about
    def _logo_frame(self, parent, image_file):
        """Black panel with the red outline, holding the full logo."""
        f = tk.Frame(parent, bg="#000000", highlightthickness=3, highlightbackground=RED)
        try:
            img = tk.PhotoImage(file=os.path.join(ASSETS, image_file))
            l = tk.Label(f, image=img, bg="#000000", bd=0)
            l.image = img
            l.pack(padx=24, pady=(20, 4))
        except Exception:
            tk.Label(f, text="FRAME DUMP", bg="#000000", fg=RED, font=(UI, 28, "bold")).pack(padx=40, pady=30)
        return f

    def _show_splash(self):
        try:
            sp = tk.Toplevel(self)
            sp.withdraw()                             # build it hidden, place it, then show it
            sp.overrideredirect(True)                 # no window border
            sp.configure(bg="#000000")
            f = self._logo_frame(sp, "logo_splash.png")
            f.pack()
            tk.Label(f, text=f"VPin Frame Harvester  v{VERSION}", bg="#000000", fg="white",
                     font=(UI, 12, "bold")).pack()
            tk.Label(f, text="by Syniztah", bg="#000000", fg="#b9b9b9", font=F).pack(pady=(0, 18))
            sp.update_idletasks()
            w, h = sp.winfo_reqwidth(), sp.winfo_reqheight()
            x = (sp.winfo_screenwidth() - w) // 2
            y = (sp.winfo_screenheight() - h) // 2
            sp.geometry(f"{w}x{h}+{x}+{y}")         # full size and place (Windows needs both)
            sp.deiconify()
            sp.attributes("-topmost", True)
            sp.update()
            sp.geometry(f"{w}x{h}+{x}+{y}")         # and again once it's shown, in case it moved
            return sp
        except Exception:
            return None

    def _end_splash(self, alpha=1.0):
        sp = self._splash
        if sp is not None and alpha > 0:
            try:                                      # quick fade out
                sp.attributes("-alpha", alpha)
                self.after(25, self._end_splash, round(alpha - 0.1, 2))
                return
            except tk.TclError:
                pass
        if sp is not None:
            sp.destroy()
            self._splash = None
        self.deiconify()
        self.lift()
        if not os.path.exists(self.v_dll.get()):
            self.after(300, self._setup_dialog)     # first start: ask where PinMAME is

    def _about(self):
        w = tk.Toplevel(self, bg="#000000")
        w.title("About")
        w.transient(self)
        w.resizable(False, False)
        try:
            w.iconphoto(False, self._icon)
        except Exception:
            pass
        f = self._logo_frame(w, "logo_about.png")
        f.pack()
        tk.Label(f, text="VPin Frame Harvester", bg="#000000", fg="white", font=F_TITLE).pack()
        tk.Label(f, text=f"Version {VERSION}  ·  by Syniztah", bg="#000000", fg="#b9b9b9", font=F).pack()
        tk.Label(f, text="Plays a pinball ROM for you and dumps every unique DMD frame\n"
                         "for ColorizingDMD and Serum. Free to use (MIT licence).",
                 bg="#000000", fg="#d8d8d8", font=F_SMALL, justify="center").pack(padx=24, pady=(10, 8))

        def link(text, url):
            l = tk.Label(f, text=text, bg="#000000", fg="#ff5a5a", font=(UI, 9, "underline"), cursor="hand2")
            l.bind("<Button-1>", lambda e: webbrowser.open(url))
            l.pack(pady=1)
        if GITHUB_URL:
            link("Download updates on GitHub", GITHUB_URL)
        tk.Label(f, text="Thanks to", bg="#000000", fg="#b9b9b9", font=F_SMALL).pack(pady=(8, 0))
        link("libpinmame by the PinMAME team", "https://github.com/vpinball/pinmame")
        link("pinball-memory-maps by Tom Collins (ODbL)", "https://github.com/tomlogic/pinball-memory-maps")
        tk.Button(f, text="Close", command=w.destroy, bg=RED, fg="white", activebackground="#ff3b3b",
                  activeforeground="white", relief="flat", bd=0, font=F_B, padx=22, pady=4,
                  cursor="hand2", highlightthickness=0).pack(pady=(14, 16))
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - w.winfo_reqheight()) // 2
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        w.bind("<Escape>", lambda e: w.destroy())

    # ---------------------------------------------------------------- header & tabs
    def _build_header(self):
        h = tk.Frame(self, bg=HEAD, height=52)
        h.pack(fill="x")
        h.pack_propagate(False)
        try:
            self._logo = tk.PhotoImage(file=ICON_PNG.replace("icon.png", "logo_header.png"))
            logo = tk.Label(h, image=self._logo, bg=HEAD, bd=0)
        except Exception:
            logo = tk.Canvas(h, width=26, height=26, bg=HEAD, highlightthickness=0)
            logo.create_rectangle(0, 0, 26, 26, fill=BLACK, outline=BLACK)
        logo.pack(side="left", padx=(20, 10))
        logo.configure(cursor="hand2")
        logo.bind("<Button-1>", lambda e: self._about())
        tk.Label(h, text="VPin Frame Harvester", bg=HEAD, fg="white", font=F_TITLE).pack(side="left")
        tk.Label(h, text="by Syniztah", bg=HEAD, fg="white", font=(UI, 10)).pack(side="left", padx=(8, 0), pady=(5, 0))
        tk.Button(h, text="i", command=self._about, bg=BLACK, fg="white", activebackground=RED,
                  activeforeground="white", relief="flat", bd=0, font=(UI, 11, "bold"), width=2,
                  cursor="hand2", highlightthickness=0).pack(side="right", padx=(0, 20))
        btn(h, "PinMAME setup", self._setup_dialog, kind="head").pack(side="right", padx=10)
        self.l_pm_status = tk.Label(h, textvariable=self.v_pm_info, bg=HEAD, fg="white", font=F)
        self.l_pm_status.pack(side="right")

    def _build_nav(self):
        n = tk.Frame(self, bg=BLACK)
        n.pack(fill="x")
        self.tabs = {}
        specs = [("harvest", "1", "Harvest", "play the ROM and capture frames"),
                 ("clean", "2", "Clean & export", "one tidy dump for ColorizingDMD"),
                 ("compare", None, "Compare with a finished project", "")]
        for key, num, name, sub in specs:
            t = tk.Frame(n, bg=BLACK, cursor="hand2")
            t.pack(side="left", padx=(20 if key == "harvest" else 2, 2))
            inner = tk.Frame(t, bg=BLACK)
            inner.pack(padx=14, pady=(10, 8))
            if num:
                b = tk.Canvas(inner, width=24, height=24, bg=BLACK, highlightthickness=0)
            else:
                b = tk.Label(inner, text=" Optional ", bg=BLACK, fg=GREEN, font=(UI, 8, "bold"),
                             highlightthickness=1, highlightbackground=GREEN)
            b.pack(side="left", padx=(0, 8))
            l1 = tk.Label(inner, text=name, bg=BLACK, fg=TABOFF, font=F_B)
            l1.pack(side="left")
            l2 = tk.Label(inner, text=f"  ·  {sub}" if sub else "", bg=BLACK, fg="#c9d1cb", font=F)
            l2.pack(side="left")
            bar = tk.Frame(t, bg=BLACK, height=3)
            bar.pack(fill="x", side="bottom")
            self.tabs[key] = dict(frame=t, inner=inner, badge=b, name=l1, sub=l2, bar=bar, num=num, subtext=sub)
            for w in (t, inner, b, l1, l2):
                w.bind("<Button-1>", lambda e, k=key: self.show_tab(k))

    def show_tab(self, key):
        self.current_tab = key
        for k, t in self.tabs.items():
            on = k == key
            bg = TABSEL if on else BLACK
            for w in (t["frame"], t["inner"], t["badge"], t["name"], t["sub"]):
                w.configure(bg=bg)
            t["name"].configure(fg="white" if on else TABOFF)
            t["sub"].configure(text=f"  ·  {t['subtext']}" if (on and t["subtext"]) else "")
            t["bar"].configure(bg=GREEN if on else BLACK)
            b = t["badge"]
            if t["num"]:
                b.delete("all")
                done = self.done.get(k) and not on
                if on:
                    b.create_oval(1, 1, 23, 23, fill=GREEN, outline=GREEN)
                    b.create_text(12, 12, text=t["num"], fill=BLACK, font=(UI, 9, "bold"))
                else:
                    b.create_oval(2, 2, 22, 22, outline=GREEN, width=1.5)
                    b.create_text(12, 12, text="✓" if done else t["num"], fill=GREEN, font=(UI, 9, "bold"))
            else:
                b.configure(bg=GREEN if on else bg, fg=BLACK if on else GREEN)
        for k, p in self.page.items():
            if k == key:
                p.pack(fill="both", expand=True)
            else:
                p.pack_forget()
        if key == "clean":
            self._clean_refresh()

    # ---------------------------------------------------------------- setup
    def _setup_dialog(self):
        w = tk.Toplevel(self, bg=PANEL)
        w.title("PinMAME setup")
        w.transient(self)
        label(w, "PinMAME setup", font=F_H).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(14, 4))
        label(w, "Where libpinmame and your ROMs are. These are remembered.", fg=MUTED).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 10))
        for r, (text, var, is_file) in enumerate([("libpinmame DLL", self.v_dll, True),
                                                   ("PinMAME folder", self.v_pm, False)], start=2):
            label(w, text).grid(row=r, column=0, sticky="w", padx=14, pady=4)
            entry(w, var, width=60).grid(row=r, column=1, sticky="ew", pady=4, ipady=4)

            def browse(v=var, f=is_file):
                p = filedialog.askopenfilename(parent=w, filetypes=[("DLL", "*.dll"), ("All", "*.*")]) if f \
                    else filedialog.askdirectory(parent=w)
                if p:
                    v.set(os.path.normpath(p))
                    self._remember()
            btn(w, "Browse…", browse).grid(row=r, column=2, padx=14, pady=4)
        label(w, "", textvariable=self.v_pm_info, fg=OKFG).grid(row=4, column=1, sticky="w", pady=(2, 10))
        label(w, "Tip: a separate folder (like C:\\libpinmame with copies of your ROMs) keeps your VPX "
                 "high scores and settings untouched.", fg=MUTED, wraplength=560).grid(
            row=5, column=0, columnspan=3, sticky="w", padx=14)
        btn(w, "Done", lambda: (self._remember(), w.destroy()), kind="dark").grid(
            row=6, column=2, sticky="e", padx=14, pady=14)

    # ---------------------------------------------------------------- helpers
    def _remember(self):
        self.s.update(dll=self.v_dll.get(), pinmame=self.v_pm.get(), crom=self.v_crom.get(),
                      crp=self.v_crp.get(), show_dmd=self.v_show.get(), show_log=self.v_showlog.get())
        save_settings(self.s)

    def _roms_dir(self) -> str | None:
        """Where the ROM zips are: <PinMAME folder>\\roms, or the folder itself."""
        base = self.v_pm.get().strip().strip('"')
        for d in (os.path.join(base, "roms"), base):
            if glob.glob(os.path.join(d, "*.zip")) or glob.glob(os.path.join(d, "*.ZIP")):
                return d
        return None

    def _roms(self) -> list[str]:
        d = self._roms_dir()
        if not d:
            return []
        files = set(glob.glob(os.path.join(d, "*.zip")) + glob.glob(os.path.join(d, "*.ZIP")))
        return sorted({os.path.splitext(os.path.basename(p))[0] for p in files}, key=str.lower)

    def _pm_changed(self, *_):
        base = self.v_pm.get().strip().strip('"')
        d = self._roms_dir()
        if d and os.path.normcase(os.path.normpath(d)) == os.path.normcase(os.path.normpath(base)) \
                and os.path.basename(os.path.normpath(base)).lower() == "roms":
            self.v_pm.set(os.path.dirname(os.path.normpath(base)))     # PinMAME wants the folder above
            return
        n = len(self._roms())
        dll_ok = os.path.exists(self.v_dll.get())
        if not dll_ok:
            self.v_pm_info.set("●  libpinmame not found - press PinMAME setup   ")
        elif n:
            self.v_pm_info.set(f"●  PinMAME ready · {n} ROMs found   ")
        elif os.path.isdir(base):
            self.v_pm_info.set("●  No ROMs found in that folder - press PinMAME setup   ")
        else:
            self.v_pm_info.set("●  PinMAME folder not found - press PinMAME setup   ")

    def _games(self) -> list[str]:
        return sorted(os.path.relpath(p, HERE) for p in glob.glob(os.path.join(HERE, "games", "*.json"))
                      if not p.lower().endswith(".digits.json") and not os.path.basename(p).startswith("_"))

    def _outs(self) -> list[str]:
        return sorted(os.path.relpath(os.path.dirname(p), HERE)
                      for p in glob.glob(os.path.join(HERE, "out", "*", "frames.txt")))

    def _log(self, widget, text):
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _open(self, path):
        if not path:
            messagebox.showinfo("Nothing yet", "There's nothing to open yet.")
            return
        path = os.path.normpath(os.path.join(HERE, path) if not os.path.isabs(path) else path)
        if not os.path.exists(path):
            parent = os.path.dirname(path)            # open the nearest folder that does exist
            while parent and not os.path.exists(parent) and os.path.dirname(parent) != parent:
                parent = os.path.dirname(parent)
            if not parent or not os.path.exists(parent):
                messagebox.showinfo("Not there yet", f"{path} doesn't exist yet.")
                return
            self.v_status.set(f"{os.path.basename(path)} isn't there yet - opened {parent}")
            path = parent
        try:
            os.startfile(path)                      # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", path])

    def _fresh(self, base):
        p, n = base, 2
        while os.path.exists(os.path.join(HERE, p)):
            p, n = f"{base}{n}", n + 1
        return p

    def _frame_count(self, folder) -> int | None:
        try:
            return len(json.load(open(os.path.join(HERE, folder, "index.json"))))
        except Exception:
            return None

    # ================================================================ STEP 1: HARVEST
    def _build_harvest(self):
        p = tk.Frame(self.pages, bg=WIN)
        self.page["harvest"] = p
        left = tk.Frame(p, bg=WIN, width=450)
        left.pack(side="left", fill="y", padx=(20, 9), pady=16)
        left.pack_propagate(False)
        right = tk.Frame(p, bg=WIN)
        right.pack(side="left", fill="both", expand=True, padx=(9, 20), pady=16)

        # A - table
        a = card(left)
        a.pack(fill="x")
        hd = tk.Frame(a, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 6))
        label(hd, "A", fg=OKFG, font=F_H).pack(side="left")
        label(hd, "  Choose the table", font=F_H).pack(side="left")
        row = tk.Frame(a, bg=PANEL)
        row.pack(fill="x", padx=14)
        entry(row, self.v_table, width=28).pack(side="left", fill="x", expand=True, ipady=4)
        btn(row, "Choose…", self._choose_table, kind="outline").pack(side="left", padx=(8, 0))
        grid = tk.Frame(a, bg=PANEL)
        grid.pack(fill="x", padx=14, pady=(8, 4))
        label(grid, "ROM", fg=SOFT, width=9).grid(row=0, column=0, sticky="w")
        self.l_rom = label(grid, "—", font=(MONO, 10))
        self.l_rom.grid(row=0, column=1, sticky="w")
        self.l_rom_ok = label(grid, "", fg=OKFG)
        self.l_rom_ok.grid(row=0, column=2, sticky="w", padx=8)
        label(grid, "Game file", fg=SOFT, width=9).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.l_game = label(grid, "—", font=(MONO, 10))
        self.l_game.grid(row=1, column=1, sticky="w", pady=(3, 0))
        self.l_game_tag = tk.Label(grid, text="", bg=BLACK, fg=GREEN, font=(UI, 8, "bold"), padx=6)
        self.b_report = tk.Label(grid, text="report", bg=PANEL, fg=OKFG, font=(UI, 9, "underline"), cursor="hand2")
        self.b_report.bind("<Button-1>", lambda e: self._show_report())
        links = tk.Frame(a, bg=PANEL)
        links.pack(fill="x", padx=14)
        nl = tk.Label(links, text="No table? Pick a ROM to watch its attract mode", bg=PANEL, fg=SOFT,
                      font=(UI, 9, "underline"), cursor="hand2")
        nl.pack(side="left")
        nl.bind("<Button-1>", lambda e: self._pick_rom_dialog())
        nv = tk.Frame(a, bg=TIPBG, highlightthickness=1, highlightbackground=TIPLINE)
        nv.pack(fill="x", padx=14, pady=(8, 12))
        tk.Label(nv, text="First time with this ROM? Boot it once so it makes its NVRAM.", bg=TIPBG,
                 fg=TIPFG, font=F, wraplength=250, justify="left").pack(side="left", padx=8, pady=6)
        btn(nv, "Create NVRAM (30 s)", self._make_nvram).pack(side="right", padx=8, pady=6)

        # B - how to play
        b = card(left)
        b.pack(fill="x", pady=10)
        hd = tk.Frame(b, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 8))
        label(hd, "B", fg=OKFG, font=F_H).pack(side="left")
        label(hd, "  How to play it", font=F_H).pack(side="left")
        seg = tk.Frame(b, bg=BLACK, highlightthickness=1, highlightbackground=BLACK)
        seg.pack(fill="x", padx=14)
        self.mode_btns = {}
        for i, (name, key) in enumerate(MODES):
            mb = tk.Button(seg, text=name, relief="flat", bd=0, font=F, cursor="hand2", pady=6,
                           command=lambda k=key: self._set_mode(k))
            mb.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 1, 0))
            self.mode_btns[key] = mb
        self._set_mode("fuzz")
        nums = tk.Frame(b, bg=PANEL)
        nums.pack(fill="x", padx=14, pady=(10, 12))
        for col, (text, widget) in enumerate([
                ("Minutes per run", lambda f: tk.Spinbox(f, from_=1, to=600, textvariable=self.v_min, width=7,
                                                         bg=FIELD, fg=TEXT, relief="flat", font=F,
                                                         buttonbackground=LIGHTBTN, highlightthickness=1,
                                                         highlightbackground=TIPLINE)),
                ("Runs in a row", lambda f: tk.Spinbox(f, from_=1, to=50, textvariable=self.v_runs, width=7,
                                                       bg=FIELD, fg=TEXT, relief="flat", font=F,
                                                       buttonbackground=LIGHTBTN, highlightthickness=1,
                                                       highlightbackground=TIPLINE)),
                ("Players", lambda f: self._optionmenu(f, self.v_players, PLAYERS))]):
            cf = tk.Frame(nums, bg=PANEL)
            cf.grid(row=0, column=col, sticky="w", padx=(0, 16))
            label(cf, text, fg=SOFT).pack(anchor="w")
            widget(cf).pack(anchor="w", pady=(2, 0), ipady=3)

        # C - extras
        c = card(left)
        c.pack(fill="x")
        hd = tk.Frame(c, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 4))
        label(hd, "C", fg=OKFG, font=F_H).pack(side="left")
        label(hd, "  Extras", font=F_H).pack(side="left")
        label(hd, "  (for rarer screens)", fg=SOFT).pack(side="left")
        for text, hint, var in [("Watch attract mode first", "2 minutes before each game", self.v_attract_first),
                                ("Lower the high scores", "reaches ENTER INITIALS", self.v_low),
                                ("Free play", "settings put back afterwards", self.v_free),
                                ("One ball missing", "BALL MISSING screens", self.v_missing)]:
            r = tk.Frame(c, bg=PANEL)
            r.pack(fill="x", padx=12)
            check(r, text, var).pack(side="left")
            label(r, f"– {hint}", fg=SOFT).pack(side="left")
        tk.Frame(c, bg=PANEL, height=8).pack()

        # start / stop (packed from the bottom up: Stop/Open under Start)
        bot = tk.Frame(left, bg=WIN)
        bot.pack(fill="x", side="bottom")
        self.b_stop = btn(bot, "Stop and save", self.runner.stop)
        self.b_stop.configure(state="disabled")
        self.b_stop.pack(side="left", fill="x", expand=True, padx=(0, 5))
        btn(bot, "Open folder", lambda: self._open(self.v_out.get())).pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.b_start = btn(left, "▶   Start harvesting", self._start, kind="dark", big=True)
        self.b_start.pack(fill="x", side="bottom", pady=(0, 8))

        # right side
        tg = tk.Frame(right, bg=WIN)
        tg.pack(fill="x")
        check(tg, "Show the DMD", self.v_show, bg=WIN, cmd=self._toggle_dmd).pack(side="left")
        check(tg, "Show what it's doing", self.v_showlog, bg=WIN, cmd=self._toggle_log).pack(side="left", padx=16)
        self.dmd_box = tk.Frame(right, bg=BLACK, highlightthickness=1, highlightbackground=LINE)
        self.dmd_box.pack(fill="x", pady=(8, 10))
        self._blank = tk.PhotoImage(width=512, height=128)
        self.dmd = tk.Label(self.dmd_box, bg=BLACK, image=self._blank, bd=0)
        self.dmd.pack(pady=14)
        stats = tk.Frame(right, bg=WIN)
        stats.pack(fill="x")
        self.stats_frame = stats
        s1 = card(stats)
        s1.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.v_time_head = tk.StringVar(value="Time left")
        self.v_time = tk.StringVar(value="—")
        label(s1, textvariable=self.v_time_head, fg=SOFT, font=F_SMALL).pack(anchor="w", padx=12, pady=(8, 0))
        label(s1, textvariable=self.v_time, font=F_BIG).pack(anchor="w", padx=12, pady=(0, 8))
        s2 = card(stats)
        s2.pack(side="left", fill="both", expand=True, padx=5)
        self.v_frames = tk.StringVar(value="—")
        label(s2, "Unique frames", fg=SOFT, font=F_SMALL).pack(anchor="w", padx=12, pady=(8, 0))
        label(s2, textvariable=self.v_frames, font=F_BIG).pack(anchor="w", padx=12, pady=(0, 8))
        s3 = card(stats)
        s3.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.v_game_line = tk.StringVar(value="Not running")
        self.v_game_line2 = tk.StringVar(value="")
        label(s3, "In the game", fg=SOFT, font=F_SMALL).pack(anchor="w", padx=12, pady=(8, 0))
        label(s3, textvariable=self.v_game_line, font=F_B, wraplength=300).pack(anchor="w", padx=12, pady=(2, 0))
        label(s3, textvariable=self.v_game_line2, fg=SOFT, wraplength=300).pack(anchor="w", padx=12, pady=(0, 8))
        self.v_status = tk.StringVar(value="")
        label(right, textvariable=self.v_status, bg=WIN, fg=TEXT, font=F_B).pack(anchor="w", pady=(8, 0))
        self.t_log = log_box(right, height=12)
        self.t_log.pack(fill="both", expand=True, pady=(6, 0))
        if not self.v_show.get():
            self.dmd_box.pack_forget()
        if not self.v_showlog.get():
            self.t_log.pack_forget()

    def _optionmenu(self, parent, var, values):
        om = tk.OptionMenu(parent, var, *values)
        om.configure(bg=FIELD, fg=TEXT, activebackground=LIGHTBTN, relief="flat", font=F,
                     highlightthickness=1, highlightbackground=TIPLINE, width=8, anchor="w")
        om["menu"].configure(bg=FIELD, fg=TEXT, font=F)
        return om

    def _set_mode(self, key):
        self.v_mode.set(key)
        for k, b in self.mode_btns.items():
            on = k == key
            b.configure(bg=BLACK if on else FIELD, fg=GREEN if on else TEXT,
                        activebackground=BLACK if on else LIGHTBTN, activeforeground=GREEN if on else TEXT,
                        font=F_B if on else F)

    def _toggle_log(self):
        if self.v_showlog.get():
            self.t_log.pack(fill="both", expand=True, pady=(6, 0))
            self.t_log.see("end")
        else:
            self.t_log.pack_forget()
        self._remember()

    def _toggle_dmd(self):
        if self.v_show.get():
            self.dmd_box.pack(fill="x", pady=(8, 10), before=self.stats_frame)
            self.live_mtime = 0
        else:
            self.dmd_box.pack_forget()
        self._remember()

    def _refresh_table_card(self):
        rom, game = self.v_rom.get(), self.v_game.get()
        self.l_rom.configure(text=rom or "—")
        if rom:
            ok = rom in self._roms()
            self.l_rom_ok.configure(text="✓ in your roms folder" if ok else "✗ not in your roms folder",
                                    fg=OKFG if ok else DANGERFG)
        else:
            self.l_rom_ok.configure(text="")
        self.l_game.configure(text=os.path.basename(game) if game else ("none yet" if rom else "—"))
        self.l_game_tag.grid_forget()
        self.b_report.grid_forget()
        if game:
            try:
                gen = json.load(open(os.path.join(HERE, game))).get("_generated")
            except Exception:
                gen = False
            self.l_game_tag.configure(text="made from the script" if gen else "hand-made")
            self.l_game_tag.grid(row=1, column=2, sticky="w", padx=8, pady=(3, 0))
            if self.report:
                self.b_report.grid(row=1, column=3, sticky="w", pady=(3, 0))
        self.tabs["clean"]["subtext"] = "one tidy dump for ColorizingDMD"

    def _show_report(self):
        messagebox.showinfo("What was read from the script", "\n".join(self.report) or "Nothing to show.")

    def _pick_rom_dialog(self):
        roms = self._roms()
        if not roms:
            messagebox.showwarning("No ROMs", "No ROMs found. Press PinMAME setup and pick your PinMAME folder.")
            return
        w = tk.Toplevel(self, bg=PANEL)
        w.title("Pick a ROM")
        w.transient(self)
        label(w, "Without a table, only Attract mode can be run.", fg=MUTED).pack(anchor="w", padx=14, pady=(12, 6))
        lb = tk.Listbox(w, height=16, width=40, bg=FIELD, fg=TEXT, font=F_MONO, relief="flat",
                        highlightthickness=1, highlightbackground=TIPLINE, selectbackground=BLACK,
                        selectforeground=GREEN)
        for r in roms:
            lb.insert("end", r)
        lb.pack(padx=14)

        def ok():
            sel = lb.curselection()
            if sel:
                self.v_table.set("")
                self.report = []
                self._last_rom = None
                self.v_rom.set(roms[sel[0]])
                if not self.v_game.get():
                    self._set_mode("attract")
            w.destroy()
        btn(w, "Use this ROM", ok, kind="dark").pack(anchor="e", padx=14, pady=12)

    def _choose_table(self):
        p = filedialog.askopenfilename(
            title="Choose the table",
            initialdir=self.s.get("table_dir") or os.path.join(os.path.dirname(self.v_pm.get()), "Tables"),
            filetypes=[("Visual Pinball table", "*.vpx"), ("Table script", "*.vbs *.txt"), ("All files", "*.*")])
        if p:
            self.v_table.set(os.path.basename(p))
            self.s["table_dir"] = os.path.dirname(p)
            self._load_table(os.path.normpath(p))

    def _load_table(self, path: str):
        """Read the table's script: find its ROM, and make a game file if there isn't one."""
        sys.path.insert(0, HERE)
        from harvester import scriptgen
        try:
            script, src = scriptgen.read_table(path)
        except OSError as e:
            messagebox.showerror("Table", f"Couldn't open the table:\n{e}")
            return
        if not script:
            messagebox.showwarning(
                "Script not found",
                "Couldn't read the script from this table.\n\nIn VPX, open the table, open the Script "
                "editor and save the script as a .vbs file next to the table (same name), then choose "
                "the table again.")
            return
        rom = scriptgen.rom_name(script)
        if rom:
            try:                       # keep a copy of the script, for checking the game file later
                open(os.path.join(HERE, "games", f"{rom}.script.vbs"), "w", encoding="latin-1",
                     errors="replace").write(script)
            except OSError:
                pass
        if not rom:
            messagebox.showwarning("ROM not found", f"The script ({src}) doesn't say which ROM it uses "
                                   "(no cGameName).")
            return
        if rom not in self._roms():
            messagebox.showwarning("ROM missing",
                                   f"This table uses the ROM '{rom}', but {rom}.zip isn't in your roms "
                                   f"folder ({self._roms_dir() or 'not found'}).\n\nCopy it there, then choose "
                                   "the table again.")
        existing = scriptgen.game_file_for(rom)
        made = False
        report = []
        if existing is None or json.load(open(existing)).get("_generated"):
            name = os.path.splitext(os.path.basename(path))[0]
            game, report = scriptgen.make_game(script, rom, name)
            out = existing or os.path.join(HERE, "games", f"{rom}.json")
            json.dump(game, open(out, "w"), indent=2)
            existing, made = out, True
        self._last_rom = None
        self.v_rom.set(rom)
        self.v_game.set(os.path.relpath(existing, HERE))
        self.report = [f"Table: {os.path.basename(path)}", f"Script: {src}", f"ROM: {rom}",
                       f"Game file: {os.path.relpath(existing, HERE)}" +
                       (" (made from the script)" if made else " (hand-made, kept)"), ""] + report
        self._refresh_table_card()
        self._log(self.t_log, "\n" + "\n".join(self.report) + "\n")
        self._set_mode("fuzz")
        warn = [r for r in report if r.startswith(("!", "?"))]
        if warn:
            messagebox.showinfo("Table loaded", f"{rom} is ready.\n\nWorth checking:\n" + "\n".join(warn))
        self.v_status.set(f"Ready: {rom}")

    def _game_roms(self, game: str) -> list[str]:
        try:
            return json.load(open(os.path.join(HERE, game))).get("roms", [])
        except Exception:
            return []

    def _rom_changed(self, *_):
        if getattr(self, "_in_rom_change", False):
            return
        rom = self.v_rom.get().strip()
        if rom == getattr(self, "_last_rom", None):
            return
        self._last_rom = rom
        self._in_rom_change = True
        try:
            if rom:
                self._rom_chosen()
        finally:
            self._in_rom_change = False
        self._refresh_table_card()

    def _rom_chosen(self):
        rom = self.v_rom.get()
        self.v_out.set(os.path.join("out", rom))
        games = self._games()
        exact = [g for g in games if rom in self._game_roms(g)]
        if exact:
            self.v_game.set(exact[0])
        else:
            self.v_game.set("")    # never carry another table's game file over
        self.src_paths = []
        self.done = {"harvest": bool(self._frame_count(self.v_out.get())), "clean": False}
        self.show_tab(getattr(self, "current_tab", "harvest"))

    def _make_nvram(self):
        rom, out = self.v_rom.get().strip(), self.v_out.get().strip()
        if not rom:
            messagebox.showinfo("Choose a table", "Choose the table (or a ROM) first.")
            return
        if self.runner.busy or not self._check_dll():
            return
        self.runner.start([(["run", "--rom", rom, "--game", os.path.join("games", "_generic.json"),
                             "--out", out, "--strategy", "attract", "--minutes", "0.5", "--seed", "0"], out)])
        self._running(True, "Creating NVRAM (30 s)…")

    def _check_dll(self) -> bool:
        if not os.path.exists(self.v_dll.get()):
            messagebox.showwarning("libpinmame", "libpinmame isn't set up yet. Press PinMAME setup (top right).")
            return False
        return True

    def _running(self, on: bool, status=""):
        self.b_start.configure(state="disabled" if on else "normal")
        self.b_stop.configure(state="normal" if on else "disabled")
        self.v_status.set(status)
        if on:
            self.v_unique = ""
            self.v_frames.set("—")

    def _start(self):
        if self.runner.busy:
            return
        rom, game, out = self.v_rom.get().strip(), self.v_game.get().strip(), self.v_out.get().strip()
        mode = self.v_mode.get()
        if not rom:
            messagebox.showinfo("Choose a table", "Choose the table first (A · Choose the table).")
            return
        if game:
            for_roms = self._game_roms(game)
            if for_roms and rom not in for_roms:
                messagebox.showwarning("Wrong game file", f"{game} is for {', '.join(for_roms)}, not {rom}.")
                return
        if not game:
            if mode == "attract":
                game = os.path.join("games", "_generic.json")       # watching needs no switches
            else:
                messagebox.showwarning("No game file",
                                       f"There's no game file for {rom}, so it can't play games or use the "
                                       "service menu. Choose the table's .vpx to make one, or use Attract mode.")
                return
        if not self._check_dll():
            return
        self._remember()
        jobs = []
        seed = random.randint(1, 99999)
        for i in range(max(1, self.v_runs.get())):
            args = ["run", "--rom", rom, "--game", game, "--out", out, "--strategy", mode,
                    "--minutes", str(self.v_min.get()), "--seed", str(seed + i)]
            if mode == "fuzz":
                if self.v_players.get() != MIX:
                    args += ["--players", self.v_players.get()]
                if self.v_low.get():
                    args.append("--low-scores")
                if self.v_attract_first.get():
                    args.append("--attract-first")
            if self.v_free.get():
                args.append("--free-play")
            if self.v_missing.get():
                args.append("--missing-ball")
            jobs.append((args, out))
        self.runner.start(jobs)
        self._running(True, "Starting…")

    # ================================================================ STEP 2: CLEAN & EXPORT
    def _build_clean(self):
        p = tk.Frame(self.pages, bg=WIN)
        self.page["clean"] = p
        main = tk.Frame(p, bg=WIN)
        main.pack(side="left", fill="both", expand=True, padx=(20, 9), pady=14)
        aside = tk.Frame(p, bg=WIN, width=340)
        aside.pack(side="left", fill="y", padx=(9, 20), pady=14)
        aside.pack_propagate(False)

        label(main, "Combines your runs into one cleaned dump. Exact duplicates are removed, and frames that "
                    "differ only by a score number can count as one. Nothing is ever merged just because it "
                    "looks similar, so no frame you need is lost. Work down the three boxes in order.",
              bg=WIN, fg=TEXT, wraplength=800).pack(anchor="w", pady=(0, 8))

        # 1 runs
        c1 = card(main)
        c1.pack(fill="x")
        hd = tk.Frame(c1, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 8))
        badge(hd, "1").pack(side="left")
        label(hd, "  Pick the runs to combine", font=F_H).pack(side="left")
        btn(hd, "Add folder…", self._add_src).pack(side="right")
        self.b_addall = btn(hd, "Add all runs for this table", self._add_rom_outs, kind="dark")
        self.b_addall.pack(side="right", padx=8)
        self.l_src = tk.Listbox(c1, height=3, bg=FIELD, fg=TEXT, font=F_MONO, relief="flat",
                                highlightthickness=1, highlightbackground=TIPLINE, selectbackground=BLACK,
                                selectforeground=GREEN, activestyle="none")
        self.l_src.pack(fill="x", padx=14)
        r = tk.Frame(c1, bg=PANEL)
        r.pack(fill="x", padx=14, pady=(6, 10))
        label(r, "Every run you did for this table, including attract and service-menu runs. Leave them all in.",
              fg=SOFT).pack(side="left")
        btn(r, "Remove selected", self._remove_src).pack(side="right")

        # 2 digits
        c2 = card(main)
        c2.pack(fill="x", pady=8)
        hd = tk.Frame(c2, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 4))
        badge(hd, "2").pack(side="left")
        label(hd, "  Learn the number digits", font=F_H).pack(side="left")
        tk.Label(hd, text=" optional · recommended ", bg=SUB, fg=MUTED, font=F_SMALL).pack(side="left", padx=8)
        label(c2, "Scores change constantly, and every new number makes a \"new\" frame. Teaching the harvester "
                  "the table's digits lets those frames count as one.", fg=MUTED, wraplength=780).pack(
            anchor="w", padx=14, pady=(0, 6))
        steps = tk.Frame(c2, bg=PANEL)
        steps.pack(fill="x", padx=14)
        for col in range(3):
            steps.columnconfigure(col, weight=1, uniform="s")
        for col, (tag, make, hint) in enumerate([
                ("a", lambda f: btn(f, "Learn digits", self._learn, kind="outline"),
                 "Looks for small shapes that tick over like a score."),
                ("b", lambda f: btn(f, "Check the digit picture", self._show_digits),
                 "It should show only numbers (rolling half-digits are fine)."),
                ("c · only if something's wrong", None, None)]):
            box = tk.Frame(steps, bg=SUB, highlightthickness=1, highlightbackground=LINE)
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 2 else 5))
            label(box, tag, bg=SUB, fg=SOFT, font=F_SMALL).pack(anchor="w", padx=10, pady=(8, 4))
            if make:
                make(box).pack(fill="x", padx=10)
                label(box, hint, bg=SUB, fg=MUTED, wraplength=220).pack(anchor="w", padx=10, pady=(4, 8))
            else:
                rr = tk.Frame(box, bg=SUB)
                rr.pack(fill="x", padx=10)
                e = entry(rr, self.v_forget, width=8)
                e.pack(side="left", fill="x", expand=True, ipady=4)
                btn(rr, "Remove", self._forget).pack(side="left", padx=(6, 0))
                label(box, "Shape numbers from the picture", bg=SUB, fg=MUTED).pack(anchor="w", padx=10, pady=(4, 2))
                btn(box, "Delete digit file", self._delete_digits, kind="danger").pack(fill="x", padx=10, pady=(2, 10))
        tip(c2, "A letter or two in the picture? Type their numbers in c and press Remove. Mostly not numbers? "
                "Press Delete digit file, do more Game play runs (an hour or more is best) and learn again. "
                "Skipping this step is safe: the dump is just bigger.", wrap=740).pack(fill="x", padx=14, pady=(8, 10))

        # 3 build
        c3 = card(main)
        c3.pack(fill="x")
        hd = tk.Frame(c3, bg=PANEL)
        hd.pack(fill="x", padx=14, pady=(12, 4))
        badge(hd, "3").pack(side="left")
        label(hd, "  Build the cleaned dump", font=F_H).pack(side="left")
        label(c3, "Exports the cleaned frames as one frames.txt, ready to import into ColorizingDMD. It always goes "
                  "into a new folder, so your captured runs are never changed and you can build again at any time.",
              fg=MUTED, wraplength=780).pack(anchor="w", padx=14, pady=(0, 6))
        r = tk.Frame(c3, bg=PANEL)
        r.pack(fill="x", padx=14, pady=(0, 10))
        label(r, "Save as", fg=SOFT).pack(side="left", padx=(0, 8))
        entry(r, self.v_clean_out, mono=True).pack(side="left", fill="x", expand=True, ipady=5)
        btn(r, "Build cleaned dump", self._merge, kind="dark").pack(side="left", padx=(10, 0), ipady=2)

        # aside
        d = card(aside)
        d.pack(fill="x")
        self.v_digits_head = tk.StringVar(value="Learned digits")
        label(d, textvariable=self.v_digits_head, fg=SOFT, font=F_SMALL).pack(anchor="w", padx=12, pady=(10, 4))
        self.digit_img = tk.Label(d, bg=BLACK, height=5, text="none yet", fg="#8f9892", font=F)
        self.digit_img.pack(fill="x", padx=12)
        self.v_digits_note = tk.StringVar(value="Learn them in box 2.")
        label(d, textvariable=self.v_digits_note, fg=OKFG, wraplength=280).pack(anchor="w", padx=12, pady=(6, 10))
        self.done_card = tk.Frame(aside, bg=TIPBG, highlightthickness=1, highlightbackground=TIPLINE)
        self.v_done = tk.StringVar(value="")
        self.v_done_path = tk.StringVar(value="")
        tk.Label(self.done_card, text="Done · ready for ColorizingDMD", bg=TIPBG, fg=OKFG, font=F_B).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(self.done_card, textvariable=self.v_done, bg=TIPBG, fg=TEXT, font=F_H, justify="left").pack(anchor="w", padx=12)
        tk.Label(self.done_card, textvariable=self.v_done_path, bg=TIPBG, fg=MUTED, font=F_MONO, wraplength=280,
                 justify="left").pack(anchor="w", padx=12, pady=(4, 6))
        bb = tk.Frame(self.done_card, bg=TIPBG)
        bb.pack(fill="x", padx=12)
        btn(bb, "Open folder", lambda: self._open(self._built_out or self.v_clean_out.get())).pack(side="left", fill="x", expand=True, padx=(0, 4))
        btn(bb, "Copy file path", self._copy_path).pack(side="left", fill="x", expand=True, padx=(4, 0))
        tk.Label(self.done_card, text="In ColorizingDMD, import frames.txt like any dump.", bg=TIPBG, fg=MUTED,
                 font=F, wraplength=280, justify="left").pack(anchor="w", padx=12, pady=(6, 10))
        self.t_clean = log_box(aside, height=8)
        self.t_clean.pack(fill="both", expand=True, pady=(10, 0))

    def _clean_refresh(self):
        rom = self.v_rom.get()
        self.b_addall.configure(text=f"Add all runs for {rom}" if rom else "Add all runs for this table")
        png = self._digits_png()
        if png and os.path.exists(png):
            try:
                img = tk.PhotoImage(file=png)
                k = max(1, (img.width() + 299) // 300)
                img = img.subsample(k, k)
                self.digit_img.configure(image=img, text="", height=img.height())
                self.digit_img.image = img
                n = len(json.load(open(png[:-4] + ".json")))
                self.v_digits_head.set(f"Learned digits · {n} shapes")
                self.v_digits_note.set("Check it shows only numbers.")
            except Exception:
                pass
        else:
            self.digit_img.configure(image="", text="none yet", height=5)
            self.digit_img.image = None
            self.v_digits_head.set("Learned digits")
            self.v_digits_note.set("Learn them in box 2 (optional).")

    def _add_rom_outs(self):
        rom = self.v_rom.get()
        if not rom:
            messagebox.showinfo("Choose a table", "Choose the table on the Harvest tab first.")
            return
        for o in self._outs():
            name = os.path.basename(o)
            if (name == rom or name.startswith(rom + "_")) and "clean" not in name and "compare" not in name \
                    and o not in self.src_paths:
                self._add_path(o)
        if not self.src_paths:
            messagebox.showinfo("No runs yet", f"There are no runs for {rom} yet. Do a Harvest run first.")
        if not self.v_clean_out.get():
            self.v_clean_out.set(self._fresh(os.path.join("out", f"{rom}_clean")))

    def _add_path(self, o):
        self.src_paths.append(o)
        n = self._frame_count(o)
        self.l_src.insert("end", f"{o}" + (f"     {n:,} frames" if n else ""))

    def _add_src(self):
        p = filedialog.askdirectory(initialdir=os.path.join(HERE, "out"))
        if p:
            self._add_path(os.path.relpath(p, HERE))

    def _remove_src(self):
        for i in reversed(self.l_src.curselection()):
            self.l_src.delete(i)
            del self.src_paths[i]

    def _srcs(self) -> list[str]:
        return list(self.src_paths)

    def _side_job(self, args, widget):
        if self.runner.busy:
            messagebox.showinfo("Busy", "Wait for the current job to finish (or stop it).")
            return
        self._clean_target = widget
        self.runner.start([(args, None)])

    def _learn(self):
        if not self._srcs() or not self.v_game.get():
            messagebox.showwarning("Missing", "Add the runs in box 1 first (and choose a table on the Harvest tab).")
            return
        self._side_job(["learn-digits", *self._srcs(), "--game", self.v_game.get()], self.t_clean)

    def _digits_png(self):
        game = self.v_game.get()
        return os.path.join(HERE, os.path.splitext(game)[0] + ".digits.png") if game else ""

    def _delete_digits(self):
        if not self.v_game.get():
            messagebox.showinfo("No table", "Choose a table on the Harvest tab first.")
            return
        base = os.path.join(HERE, os.path.splitext(self.v_game.get())[0])
        files = [p for p in (base + ".digits.json", base + ".digits.png") if os.path.exists(p)]
        if not files:
            messagebox.showinfo("No digit file", "There's no learned digit file for this table.")
            return
        if not messagebox.askyesno("Delete digit file",
                                   f"Delete the learned digits for {self.v_rom.get() or 'this table'}?\n\n"
                                   "Your captured frames are not touched. Do more Game play runs, then press "
                                   "'Learn digits' again."):
            return
        for p in files:
            os.remove(p)
        for w in self.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()
        self._log(self.t_clean, "\nDigit file deleted. Do more Game play runs, then learn again.\n")
        self._clean_refresh()

    def _show_digits(self):
        p = self._digits_png()
        if not p or not os.path.exists(p):
            messagebox.showinfo("No digits yet", "Press 'Learn digits' first.")
            return
        w = tk.Toplevel(self, bg=PANEL)
        w.title("Learned digits - should be ONLY numbers")
        img = tk.PhotoImage(file=p)
        lab = tk.Label(w, image=img, bg=BLACK)
        lab.image = img
        lab.pack(padx=12, pady=12)
        tip(w, "One or two letters? Type their numbers in box c and press Remove.\n"
               "Mostly not numbers? Press Delete digit file and get more Game play data first.",
            wrap=600).pack(fill="x", padx=12)
        btn(w, "Delete digit file", self._delete_digits, kind="danger").pack(pady=12)

    def _forget(self):
        nums = re.findall(r"\d+", self.v_forget.get())
        if not nums:
            return
        self._side_job(["forget-digit", *nums, "--game", self.v_game.get()], self.t_clean)
        self.v_forget.set("")

    def _merge(self):
        out = self.v_clean_out.get().strip()
        if not self._srcs():
            messagebox.showwarning("Missing", "Add the runs in box 1 first.")
            return
        if not out:
            out = os.path.join("out", f"{self.v_rom.get() or 'dump'}_clean")
        if os.path.exists(os.path.join(HERE, out)):
            out = self._fresh(out)
        self.v_clean_out.set(out)
        self._built_out = out                       # the Done card points here, whatever box 3 says later
        self.done_card.pack_forget()
        self._side_job(["merge", *self._srcs(), "--out", out, "--game", self.v_game.get() or
                        os.path.join("games", "_generic.json")], self.t_clean)

    def _copy_path(self):
        p = os.path.join(HERE, self._built_out or self.v_clean_out.get(), "frames.txt")
        self.clipboard_clear()
        self.clipboard_append(p)
        self.v_status.set("Copied: " + p)

    # ================================================================ OPTIONAL: COMPARE
    def _build_compare(self):
        p = tk.Frame(self.pages, bg=WIN)
        self.page["compare"] = p
        body = tk.Frame(p, bg=WIN)
        body.pack(fill="both", expand=True, padx=20, pady=14)
        label(body, "Already coloured this table, or someone else has? Point at the finished Serum project to see "
                    "how much of it the harvester reached on its own, and which screens it missed. You don't "
                    "need this to make a dump.", bg=WIN, wraplength=1000).pack(anchor="w", pady=(0, 10))
        top = tk.Frame(body, bg=WIN)
        top.pack(fill="x")
        pc = card(top)
        pc.pack(side="left", fill="x", expand=True)
        g = tk.Frame(pc, bg=PANEL)
        g.pack(fill="x", padx=14, pady=12)
        g.columnconfigure(1, weight=1)
        for r, (text, var, types) in enumerate([
                ("Serum .cRom", self.v_crom, [("Serum", "*.cRom *.crom"), ("All", "*.*")]),
                ("Project .cRP", self.v_crp, [("ColorizingDMD", "*.cRP *.crp"), ("All", "*.*")])]):
            label(g, text, fg=SOFT, width=12).grid(row=r, column=0, sticky="w", pady=4)
            entry(g, var, mono=True).grid(row=r, column=1, sticky="ew", pady=4, ipady=4)

            def browse(v=var, t=types):
                f = filedialog.askopenfilename(filetypes=t)
                if f:
                    v.set(os.path.normpath(f))
                    self._remember()
            btn(g, "Browse…", browse).grid(row=r, column=2, padx=(8, 0), pady=4)
        label(g, "Uses the runs listed in Step 2. The .cRP is optional, but without it there are no pictures "
                 "of the missed screens.", fg=SOFT).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        btn(top, "Compare", self._compare, kind="dark", big=True, width=12).pack(side="left", fill="y", padx=(12, 0))

        res = tk.Frame(body, bg=WIN)
        res.pack(fill="x", pady=12)
        self.v_cmp = [tk.StringVar(value="—") for _ in range(3)]
        self.v_cmp_sub = [tk.StringVar(value="") for _ in range(3)]
        for i, head in enumerate(["Reached, pixel for pixel", "Reached, allowing for sparkle", "Not reached"]):
            c = card(res)
            c.pack(side="left", fill="both", expand=True, padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            label(c, head, fg=SOFT, font=F_SMALL).pack(anchor="w", padx=14, pady=(10, 0))
            label(c, textvariable=self.v_cmp[i], font=(UI, 24, "bold")).pack(anchor="w", padx=14)
            label(c, textvariable=self.v_cmp_sub[i], fg=MUTED).pack(anchor="w", padx=14, pady=(0, 10))

        low = tk.Frame(body, bg=WIN)
        low.pack(fill="both", expand=True)
        lc = card(low)
        lc.pack(side="left", fill="both", expand=True)
        label(lc, "Look at the results", font=F_H).pack(anchor="w", padx=14, pady=(12, 8))
        bg = tk.Frame(lc, bg=PANEL)
        bg.pack(fill="x", padx=14)
        for i, (name, sub, fn) in enumerate([("Missed screens", "Project frames it never showed", "missed.png"),
                                             ("Closest captures", "Each missed screen next to its nearest capture", "nearest.png"),
                                             ("Not in the project", "Captured frames the author dropped", "unrecognised.png")]):
            bg.columnconfigure(i, weight=1, uniform="r")
            b = tk.Button(bg, text=f"{name}\n{sub}", justify="left", anchor="w", bg=LIGHTBTN, fg=TEXT,
                          activebackground="#c2efd3", relief="flat", font=F, padx=12, pady=10, cursor="hand2",
                          wraplength=230, highlightthickness=1, highlightbackground=LINE,
                          command=lambda n=fn: self._open(os.path.join(self._cmp_out, n) if self._cmp_out else ""))
            b.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 2 else 5))
        label(lc, "Results are saved in a new out\\<rom>_compare folder each time.", fg=SOFT).pack(
            anchor="w", padx=14, pady=10)
        self.t_cmp = log_box(low, height=10)
        self.t_cmp.configure(width=46)
        self.t_cmp.pack(side="left", fill="both", padx=(12, 0))

    def _compare(self):
        if not self._srcs():
            messagebox.showwarning("Missing", "Add the runs in Step 2, box 1 first.")
            return
        if not os.path.exists(self.v_crom.get()):
            messagebox.showwarning("Missing", "Pick the project's .cRom file.")
            return
        self._remember()
        rom = self.v_rom.get() or "project"
        self._cmp_out = self._fresh(os.path.join("out", f"{rom}_compare"))
        args = ["compare", *self._srcs(), "--crom", self.v_crom.get(), "--out", self._cmp_out]
        if os.path.exists(self.v_crp.get()):
            args += ["--crp", self.v_crp.get()]
        for v in self.v_cmp:
            v.set("…")
        self._side_job(args, self.t_cmp)

    # ================================================================ live updates
    def _poll(self):
        target = self._clean_target
        while True:
            try:
                line = self.runner.lines.get_nowait()
            except queue.Empty:
                break
            if not line:
                continue
            if line == "\x00EXIT":
                p = self.runner.proc
                failed = p is not None and p.returncode not in (0, None)
                if failed and self.runner.queue:
                    self.runner.queue.clear()
                    self.runner.lines.put("\n[app] stopped: the last command failed - see the lines above\n")
                if failed:
                    self.after(50, self._explain_failure, list(self._job_lines))
                self.runner._next()
                continue
            if line.startswith("\n> harvest.py"):
                self._job_lines = []
            self._job_lines = (getattr(self, "_job_lines", []) + [line.strip()])[-60:]
            harvesting = self.runner.current_out is not None
            self._log(self.t_log if harvesting or target is None else target, line)
            self._parse(line)
            if "[app] all done" in line:
                self._running(False, self.v_status.get() if not self.v_status.get().startswith(("Starting", "Creating"))
                              else "Stopped - see the log")
                self._clean_target = None
                self.v_time.set("—")
                self.v_time_head.set("Time left")
        clock = self._clock()
        if clock:
            self.v_time.set(clock[0])
            self.v_time_head.set(clock[1])
            if self.v_status.get().startswith(("Starting", "Creating")):
                self.v_status.set("")
        self._draw_live()
        self.after(150, self._poll)

    def _explain_failure(self, lines):
        """A run ended with an error: say why in a box, so it isn't lost in the log."""
        lines = [l for l in lines if l]
        missing = [m for l in lines for m in re.findall(r"(\S+) NOT FOUND", l)]
        rom = next((m.group(1) for l in lines for m in [re.search(r"--rom (\S+)", l)] if m), "this ROM")
        if missing:
            msg = (f"PinMAME couldn't start {rom}: {rom}.zip doesn't contain "
                   f"{', '.join(sorted(set(missing)))}.\n\n"
                   "Usually the zip is a different version of the game (another revision, or a "
                   "set made for a different emulator). Check the table script's cGameName, and that "
                   "the zip in your PinMAME roms folder is the matching one.")
        else:
            why = [l for l in lines if "ERROR" in l or "PinMAME stopped" in l or "didn't finish" in l
                   or "Traceback" in l or "Error" in l]
            msg = "The run stopped with an error:\n\n" + "\n".join((why or lines)[-4:])
        messagebox.showwarning("Couldn't run", msg, parent=self)

    def _parse(self, line):
        m = re.search(r"unique=\s*(\d+)", line)
        if m:
            self.v_frames.set(f"{int(m.group(1)):,}")
        if "[game]" in line and "high-score" not in line and "lowered" not in line and "credits set" not in line:
            txt = line.strip().split("[game]", 1)[1].strip().replace("now ", "")
            parts = txt.split(", scores ")
            self.v_game_line.set(parts[0].replace(", ", " · ").replace("player", "Player").replace("ball", "Ball")
                                 .replace("credits", "Credits"))
            self.v_game_line2.set(("Score " + parts[1]) if len(parts) > 1 else "")
        m = re.search(r"Done: (\d+) unique frames", line)
        if m:
            self.v_frames.set(f"{int(m.group(1)):,}")
            self.v_status.set(f"Finished · {int(m.group(1)):,} unique frames saved")
            self.done["harvest"] = True
        m = re.search(r"Read (\d+) frames from (\d+) source\(s\): (\d+) unique", line)
        if m:
            self.v_done.set(f"{int(m.group(1)):,} frames read\n{int(m.group(3)):,} in the cleaned dump")
            self.v_done_path.set(os.path.join(self._built_out or self.v_clean_out.get(), "frames.txt"))
            self.done_card.pack(fill="x", pady=(10, 0), before=self.t_clean)
            self.done["clean"] = True
        if re.search(r"Learned \d+ digit shapes|Removed \d+ shape", line):
            self.after(300, self._clean_refresh)
        m = re.search(r"reached by the harvester: (\d+) of (\d+) \(([\d.]+)%\)", line)
        if m:
            self.v_cmp[0].set(f"{m.group(3)}%")
            self.v_cmp_sub[0].set(f"{int(m.group(1)):,} of {int(m.group(2)):,} project frames")
        m = re.search(r"same screen, only the shade/sparkle differs: (\d+) screens", line)
        if m:
            self.v_cmp_sub[1].set(f"+{m.group(1)} screens where only the glitter differs")
        m = re.search(r"reached allowing for sparkle: (\d+) of (\d+) \(([\d.]+)%\)", line)
        if m:
            self.v_cmp[1].set(f"{m.group(3)}%")
        m = re.search(r"Project frames NOT reached: (\d+)", line)
        if m:
            self.v_cmp[2].set(f"{int(m.group(1)):,}")
            self.v_cmp_sub[2].set("see Missed screens")

    def _clock(self):
        r = self.runner
        if not (r.busy and r.current_out and r.job_started and r.job_minutes):
            return None
        left = max(0.0, r.job_minutes * 60 - (time.monotonic() - r.job_started))
        rest = sum(float(a[a.index("--minutes") + 1]) * 60 for a, _ in r.queue if "--minutes" in a)
        fmt = lambda x: f"{int(x // 60)}:{int(x % 60):02d}"
        head = "Time left"
        if r.total_jobs > 1:
            head = f"Time left · run {r.job_no} of {r.total_jobs} ({fmt(left + rest)} in all)"
        return fmt(left), head

    def _draw_live(self):
        out = self.runner.current_out
        if not out or not self.v_show.get():
            return
        p = os.path.join(HERE, out, "_live.bin")
        try:
            mt = os.path.getmtime(p)
            if mt == self.live_mtime:
                return
            data = open(p, "rb").read()
        except OSError:
            return
        self.live_mtime = mt
        if len(data) < 5:
            return
        w, h, levels = struct.unpack("<HHB", data[:5])
        px = data[5:5 + w * h]
        if len(px) != w * h or w == 0:
            return
        top = max(1, levels - 1)
        pal = []
        for v in range(256):                     # the ROM's shades, in DMD orange
            k = min(v, top) / top
            pal.append(bytes((int(20 + 235 * k), int(8 + 120 * k), int(0 + 20 * k))))
        rgb = b"".join(pal[v] for v in px)
        img = tk.PhotoImage(data=f"P6 {w} {h} 255\n".encode() + rgb, format="PPM")
        scale = max(1, 512 // w)
        img = img.zoom(scale, scale)
        self.live_img = img
        self.dmd.configure(image=img)

    def _close(self):
        if self.runner.busy:
            if not messagebox.askyesno("Still running", "A run is going. Stop it (saving what it has) and quit?"):
                return
            self.runner.stop()
        self._remember()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
