"""Make a game file from a VPX table.

Reads the table's VBScript (from a .vbs next to the table, a saved script,
or the script stored inside the .vpx), finds which ROM it runs, which
machine type it is (from the system script it loads: sega.vbs, wpc.vbs...),
and builds the switch map the harvester needs:

- cabinet switches (start, coin, tilt, slam tilt, flippers, service buttons)
  from the system script's own constants, unless the table overrides them
- the ball trough (switches, ball count, release coil, two-stage troughs)
- holes / scoops / VUKs that hold a ball until a coil fires
- drop-target banks and their reset coils
- every playfield switch, spinners, the shooter lane, launch and extra buttons

Anything it can't work out safely (motor-driven toys, multi-ball locks) is
listed in the report instead of guessed.
"""
from __future__ import annotations

import glob
import json
import os
import re
import struct

# Cabinet switches per system script, from VPX's own scripts folder.
SYSTEMS = {
    "sega":    dict(start=54, coin=5, tilt=56, slam_tilt=55, flippers=[82, 84], service_buttons=[0, -1, -2],
                    label="Sega / Stern Whitestar"),
    "sega2":   dict(start=3, coin=5, tilt=1, slam_tilt=8, flippers=[64, 63], service_buttons=[0, -1, -2],
                    label="Sega 1990s"),
    "de":      dict(start=3, coin=4, tilt=1, slam_tilt=7, flippers=[82, 84], service_buttons=[-7, -6],
                    label="Data East"),
    "de2":     dict(start=3, coin=4, tilt=1, slam_tilt=7, flippers=[82, 84], service_buttons=[-7, -6],
                    label="Data East"),
    "wpc":     dict(start=13, coin=1, tilt=14, slam_tilt=21, flippers=[112, 114], service_buttons=[5, 6, 7, 8],
                    label="Williams/Bally WPC"),
    "s11":     dict(start=3, coin=6, tilt=1, slam_tilt=7, flippers=[82, 84], service_buttons=[-7, -6],
                    label="Williams System 11"),
    "s7":      dict(start=3, coin=6, tilt=1, slam_tilt=7, flippers=[82, 84], service_buttons=[-7, -6],
                    label="Williams System 7"),
    "gts3":    dict(start=4, coin=0, tilt=-7, slam_tilt=-6, flippers=[141, 143], service_buttons=[-8],
                    label="Gottlieb System 3"),
    "sys80":   dict(start=47, coin=17, tilt=57, slam_tilt=-1, flippers=[112, 114], service_buttons=[7],
                    label="Gottlieb System 80"),
    "sam":     dict(start=16, coin=65, tilt=-7, slam_tilt=-6, flippers=[82, 84], service_buttons=[-3, -2, -1, 0],
                    label="Stern SAM"),
    "capcom":  dict(start=7, coin=1, tilt=10, slam_tilt=9, flippers=[82, 84], service_buttons=[8],
                    label="Capcom"),
    "stern":   dict(start=6, coin=1, tilt=7, slam_tilt=8, flippers=[82, 84], service_buttons=[-7],
                    label="Stern (1980s)"),
    "bally":   dict(start=6, coin=11, tilt=7, slam_tilt=16, flippers=[82, 84], service_buttons=[-7],
                    label="Bally (1970s-80s)"),
    "alving":  dict(start=9, coin=1, slam_tilt=5, flippers=[114, 116], service_buttons=[-7, -6],
                    cabinet_pulse_ms=400, label="Alvin G."),
}
CPC = {"wpc": 2, "sega": 2, "sega2": 2, "de": 2, "de2": 2, "s11": 2, "sam": 2, "alving": 2}   # coins per credit (usual default)

from .paths import BASE
GAMES = os.path.join(BASE, "games")


# ---------------------------------------------------------------- reading
def read_vpx_script(path: str) -> str | None:
    """The script stored inside a .vpx (GameStg/GameData, record 'CODE')."""
    data = None
    try:
        import olefile                                  # pip install olefile
        with olefile.OleFileIO(path) as ole:
            if ole.exists("GameStg/GameData"):
                data = ole.openstream("GameStg/GameData").read()
    except ImportError:
        pass
    except Exception:
        data = None
    blobs = [data] if data else []
    if not blobs:                                       # fallback: scan the raw file
        blobs = [open(path, "rb").read()]
    for blob in blobs:
        i = blob.find(b"CODE")
        while i != -1:
            if i + 8 <= len(blob):
                n = struct.unpack("<i", blob[i + 4:i + 8])[0]
                if 20 < n <= len(blob) - i - 8:
                    text = blob[i + 8:i + 8 + n]
                    t = text.decode("utf-8", "replace") if b"\x00" not in text[:200] else ""
                    if re.search(r"(?i)\b(sub|option explicit|controller)\b", t):
                        return t
            i = blob.find(b"CODE", i + 4)
    return None


def read_table(path: str) -> tuple[str | None, str]:
    """(script, where it came from). A .vbs next to a .vpx wins, like in VPX."""
    base, ext = os.path.splitext(path)
    if ext.lower() == ".vpx":
        for cand in (base + ".vbs", base + ".VBS"):
            if os.path.exists(cand):
                return open(cand, encoding="latin-1").read(), os.path.basename(cand)
        s = read_vpx_script(path)
        return s, ("script inside " + os.path.basename(path)) if s else "nothing found"
    return open(path, encoding="latin-1").read(), os.path.basename(path)


# ---------------------------------------------------------------- parsing
def _strip_comment(line: str) -> str:
    out, q = [], False
    for ch in line:
        if ch == '"':
            q = not q
        if ch == "'" and not q:
            break
        out.append(ch)
    s = "".join(out)
    if re.match(r"(?i)\s*rem\b", s):
        return ""
    return s


def _statements(script: str):
    """(sub_name, with_var, statement) for every statement, comments removed."""
    sub, withs = None, []
    for raw in script.replace("\r", "").split("\n"):
        line = _strip_comment(raw)
        parts, q, cur = [], False, ""
        for ch in line:                                  # split on ':' outside quotes
            if ch == '"':
                q = not q
            if ch == ":" and not q:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        for st in (p.strip() for p in parts):
            if not st:
                continue
            m = re.match(r"(?i)(?:private\s+|public\s+)?sub\s+(\w+)", st)
            if m:
                sub = m.group(1)
            if re.match(r"(?i)end\s+sub\b", st):
                yield sub, None, st
                sub = None
                continue
            m = re.match(r"(?i)with\s+(\w+)", st)
            if m:
                withs.append(m.group(1))
                continue
            if re.match(r"(?i)end\s+with\b", st):
                if withs:
                    withs.pop()
                continue
            yield sub, (withs[-1] if withs else None), st


def _ints(s: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", s)]


def rom_name(script: str) -> str | None:
    for _, _, st in _statements(script):
        m = re.search(r'(?i)\bcGameName\s*=\s*"([^"]+)"', st) or \
            re.search(r'(?i)\.GameName\s*=\s*"([^"]+)"', st)
        if m:
            return m.group(1)
    return None


def system_of(script: str) -> str | None:
    names = [n.lower() for n in re.findall(r'(?i)"(\w+)\.vbs"', script)]
    for n in names:
        if n in SYSTEMS:
            return n
    return None


def make_game(script: str, rom: str | None = None, table: str = "") -> tuple[dict, list[str]]:
    rep: list[str] = []
    rom = rom or rom_name(script)
    sysname = system_of(script)
    st_list = list(_statements(script))
    body: dict[str, list[str]] = {}
    for sub, _, st in st_list:
        if sub:
            body.setdefault(sub.lower(), []).append(st)

    # --- objects: ball stacks, drop targets, mechs
    kinds: dict[str, str] = {}
    for _, _, st in st_list:
        m = re.match(r"(?i)set\s+(\w+)\s*=\s*new\s+(cvpm\w+)", st)
        if m:
            kinds[m.group(1).lower()] = m.group(2).lower()
    calls: dict[str, list[tuple[str, str]]] = {}
    for _, w, st in st_list:
        m = re.match(r"(?i)(\w+)?\.(\w+)\b\s*=?\s*(.*)", st)
        if not m:
            continue
        var = (m.group(1) or w or "").lower()
        if var in kinds:
            calls.setdefault(var, []).append((m.group(2).lower(), m.group(3)))

    def method(var, name):
        return [a for n, a in calls.get(var, []) if n == name]

    stacks = {v for v, k in kinds.items() if k in ("cvpmballstack", "cvpmtrough", "cvpmsaucer")}
    drops = {v for v, k in kinds.items() if k == "cvpmdroptarget"}
    mechs = {v for v, k in kinds.items() if k == "cvpmmech"}

    def stack_switches(v):
        sw = []
        for a in method(v, "initsw"):
            sw = [x for x in _ints(a)[1:] if x != 0]
        for a in method(v, "initswitches"):
            sw = [x for x in _ints(a) if x != 0]
        for a in method(v, "initsaucer"):
            n = _ints(a.split(",", 1)[1]) if "," in a else []
            sw = n[:1]
        return sw

    # --- solenoid callbacks: coil -> text it runs (expanded one sub deep)
    sol: dict[int, str] = {}
    for _, _, st in st_list:
        m = re.match(r'(?i)solcallback\s*\(\s*(\d+)\s*\)\s*=\s*"([^"]*)"', st)
        if m:
            sol[int(m.group(1))] = m.group(2)

    def coil_text(c):
        expr = sol.get(c, "")
        name = re.match(r"\s*(\w+)", expr)
        txt = expr
        if name and name.group(1).lower() in body:
            txt += "\n" + "\n".join(body[name.group(1).lower()])
        for t in re.findall(r"(?i)(\w+)\.enabled\s*=\s*(?:1|true)", txt):   # a timer it starts
            txt += "\n" + "\n".join(body.get(t.lower() + "_timer", []))
        return txt.lower()

    # the table's own start-up sub (vpmInit / GameName / creating balls)
    init_subs = [n for n, sts in body.items() if n.endswith("_init") and
                 (n.startswith(("table", "tbl")) or
                  any(re.search(r"(?i)vpminit|gamename|\.run\b|create\w*ball", x) for x in sts))]
    for n in list(init_subs):                  # plus helper subs they call (CreateBalls, InitTrough...)
        for st in body[n]:
            c = re.fullmatch(r"(?i)(?:call\s+)?(\w+)\s*(?:\(\s*\))?", st.strip())
            if c and c.group(1).lower() in body and c.group(1).lower() not in init_subs:
                init_subs.append(c.group(1).lower())

    # "physical" troughs: real balls created in trough kickers at start-up. A kicker named
    # sw16 / sw14k is switch 16 / 14; otherwise its _Hit sub says which switch it closes.
    def kicker_switch(obj):
        m = re.fullmatch(r"sw(\d+)[a-z]?", obj)
        if m:
            return int(m.group(1))
        for st in body.get(obj + "_hit", []):
            s = re.search(r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b", st)
            if s:
                return int(s.group(1))
        return None
    made, made_obj = [], {}
    for n in init_subs:
        sts = [x.lower() for x in body[n]]
        for i, st in enumerate(sts):
            m = re.search(r"(?:=\s*|^)(\w+)\.create\w*ball", st)
            if not m:
                continue
            obj = m.group(1)
            if any(re.match(rf"{obj}\.enabled\s*=\s*(?:0|false)", x) for x in sts):
                continue                        # a one-off kicker that is switched off again
            sw = kicker_switch(obj)
            if sw is not None and sw not in made:
                made.append(sw)
                made_obj[sw] = obj

    # --- trough
    g: dict = {"name": table or rom or "Unknown table", "roms": [rom] if rom else []}
    cand = [v for v in stacks if "trough" in v or any("true" in a.lower() or a.strip() == "1"
                                                         for a in method(v, "istrough"))]
    trough = max(cand, key=lambda v: len(stack_switches(v)), default=None)
    if trough and len(made) >= 2 and len(made) > len(stack_switches(trough)):
        rep.append(f"? {trough} looks like a ball lock, not the trough (the table puts its own "
                   f"balls in the trough) - treated as a hole.")
        trough = None                           # e.g. Apollo 13's "bsTroughSuperVUK"
    trough_sw: list[int] = stack_switches(trough) if trough else []
    eject = None
    if trough:
        others = [v for v in cand if v != trough and len(stack_switches(v)) == 1]
        eject = others[0] if others else None
        balls = [a for a in method(trough, "balls") if re.fullmatch(r"\s*\d+\s*", a)]
        g["trough"] = trough_sw
        if balls:
            n = int(balls[0])
            if n < len(trough_sw):
                g["balls"] = n
        rel = [c for c in sol if re.search(rf"\b{eject or trough}\.(solout|exitsol_on)", coil_text(c))]
        if rel:
            g["trough_solenoid"] = rel[0]
        else:
            rep.append("! Couldn't find the coil that releases balls from the trough.")
        if eject:
            load = [c for c in sol if c not in rel and re.search(rf"\b{eject}\.addball", coil_text(c))]
            g["eject"] = {"switch": stack_switches(eject)[0], "load_coil": load[0] if load else None}
        entry = [_ints(a)[0] for a in method(trough, "initsw") if _ints(a)]
        sol_in = [c for c in sol if re.search(rf"\b{trough}\.solin\b", coil_text(c))]
        if entry and entry[0] and sol_in:
            g["outhole"] = {"switch": entry[0], "coil": sol_in[0]}
            rep.append(f"Outhole: switch {entry[0]}, moved into the trough by coil {sol_in[0]}")
        for c in rel[:1]:
            p = re.search(r"pulsesw\s+(\d+)", coil_text(c))
            if p:
                g["serve_pulse"] = int(p.group(1))
        rep.append(f"Trough: switches {trough_sw}" + (f", {g['balls']} balls" if "balls" in g else "")
                   + (f", released by coil {g.get('trough_solenoid')}" if "trough_solenoid" in g else "")
                   + (f"; eject lane {g['eject']['switch']} loaded by coil {g['eject']['load_coil']}" if eject else ""))
    else:
        # Newer "physical" troughs (found above): the exit is the trough kicker a coil
        # kicks out or takes the ball from (it may be the first or the last created)
        if made:
            rel, exit_sw = [], None
            for c in sol:
                for n in made:
                    if re.search(rf"\b{made_obj[n]}\.(kick|destroyball)\b", coil_text(c)):
                        rel.append(c)
                        exit_sw = exit_sw if exit_sw is not None else n
                        break
            if exit_sw is not None:            # only the exit's own row of kickers (sw14k..sw10k,
                pat = re.sub(r"\d+", "#", made_obj[exit_sw])   # not a ball lock's sw24b..sw17b)
                made = [n for n in made if re.sub(r"\d+", "#", made_obj[n]) == pat]
            if exit_sw is not None and exit_sw == made[-1] and exit_sw != made[0]:
                made = made[::-1]                      # list the trough from the exit backwards
            trough_sw = made
            g["trough"] = made
            if rel:
                g["trough_solenoid"] = rel[0]
            else:
                rep.append("! Couldn't find the coil that releases balls from the trough.")
            rep.append(f"Trough (physical): switches {made}" +
                       (f", released by coil {rel[0]}" if rel else ""))
        else:
            rep.append("! No ball trough found - games can't be played until one is added.")
    # Outhole: the drain kicker holds the ball (switch) until a coil kicks it to the trough
    for n, sts in ([] if "outhole" in g else body.items()):
        m = re.match(r"(\w*(?:drain|outhole)\w*)_hit$", n) or re.match(r"(sw\d+)_hit$", n)
        if not m or not trough_sw:
            continue
        if not re.search(r"drain|outhole", n) and not any(re.search(r"(?i)drain|outhole", x) for x in sts):
            continue                                # a numbered kicker only counts if it's the drain
        sws = [int(x) for st in sts for x in re.findall(r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b", st)]
        coils = [c for c in sol if re.search(rf"\b{m.group(1)}\.kick\b", coil_text(c))]
        if sws and coils:
            g["outhole"] = {"switch": sws[0], "coil": coils[0]}
            rep.append(f"Outhole: switch {sws[0]}, kicked into the trough by coil {coils[0]}")
            break

    # --- holes (ball stacks with one switch, and kicker subs)
    holes, locks = [], []
    launch_stack_sw = None
    for v in sorted(stacks - {trough, eject}):
        if re.search(r"launch|plunger|shoot", v) and len(stack_switches(v)) == 1:
            launch_stack_sw = stack_switches(v)[0]          # auto-launch lane = shooter lane
            continue
        sw = stack_switches(v)
        coils = [c for c in sol if re.search(rf"\b{v}\.(solout|exitsol_on|kickball)", coil_text(c))]
        if len(sw) == 1 and coils:
            holes.append({"switch": sw[0], "coil": coils[0]})
        elif len(sw) > 1:
            locks.append((v, sw))
    lock_sw = {x for _, sw in locks for x in sw}
    entered = set()                 # switches a ball closes by landing in something (*_Hit)
    for n, sts in body.items():
        if n.endswith("_hit"):
            for st in sts:
                entered.update(int(x) for x in re.findall(
                    r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b", st))
    for c in sol:
        t = coil_text(c)
        # newer scripts: "If Controller.Switch(67) <> 0 Then KickBall ..." releases a saucer
        if re.search(r"kickball|\.kick", t):
            for n in re.findall(r"controller\.switch\s*\(\s*(\d+)\s*\)\s*(?:<>\s*0|=\s*(?:true|1))", t):
                n = int(n)
                if n in entered and n not in [h["switch"] for h in holes] and n not in trough_sw \
                        and n != g.get("outhole", {}).get("switch"):
                    holes.append({"switch": n, "coil": c})
        if ".kick" in t:
            for n in re.findall(r"controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:false|0)\b", t):
                if int(n) not in [h["switch"] for h in holes] and int(n) not in trough_sw \
                        and int(n) in entered and int(n) != g.get("outhole", {}).get("switch"):
                    holes.append({"switch": int(n), "coil": c})
    if holes:
        g["holes"] = holes
        rep.append("Holes/scoops: " + ", ".join(f"{h['switch']} (coil {h['coil']})" for h in holes))
    for v, sw in locks:
        rep.append(f"? {v} holds several balls on switches {sw} - treated as plain switches.")

    # --- drop targets
    banks = []
    for v in sorted(drops):
        sws = []
        for a in method(v, "initdrop"):
            arrs = re.findall(r"(?i)array\s*\(([^)]*)\)", a)
            sws = _ints(arrs[1]) if len(arrs) >= 2 else _ints(a.split(",")[-1])
        reset = [c for c in sol if re.search(rf"\b{v}\.(soldropup|dropsol_on)", coil_text(c))]
        if sws:
            banks.append({"switches": sws, "reset_coil": reset[0] if reset else None})
    for c in sol:                                     # newer scripts: DTRaise 17 in the reset sub
        ns = [int(n) for n in re.findall(r"dtraise\s*\(?\s*(\d+)", coil_text(c))]
        if ns and not any(set(ns) & set(b["switches"]) for b in banks):
            banks.append({"switches": ns, "reset_coil": c})
    if banks:
        g["drop_banks"] = banks
        rep.append("Drop targets: " + "; ".join(f"{b['switches']} reset by coil {b['reset_coil']}" for b in banks))

    # --- cabinet
    cab = dict(SYSTEMS.get(sysname or "", {}))
    for k, key in (("start", "swstartbutton"), ("coin", "swcoin1"), ("tilt", "swtilt"), ("slam_tilt", "swslamtilt")):
        m = re.search(rf"(?i)\bconst\s+{key}\s*=\s*(-?\d+)", script)
        if m:
            cab[k] = int(m.group(1))
    m = re.search(r"(?i)vpmnudge\.tiltswitch\s*=\s*(-?\d+)\b", script)
    if m:
        cab["tilt"] = int(m.group(1))
    if sysname:
        rep.insert(0, f"Machine type: {cab.pop('label')} ({sysname}.vbs)")
    else:
        cab.pop("label", None)
        rep.insert(0, "! Machine type not recognised - start, coin and flipper switches need filling in.")
    for k in ("coin", "start", "tilt", "slam_tilt", "flippers", "service_buttons", "cabinet_pulse_ms"):
        if k in cab:
            g[k] = cab[k]
    g["coins_per_credit"] = CPC.get(sysname or "", 1)

    # --- keys: launch and extra buttons
    launch, buttons = None, []
    for sub, _, st in st_list:
        if not sub or not sub.lower().endswith("_keydown"):
            continue
        m = re.search(r"(?i)if\s+keycode\s*=\s*([\w ]+?)\s+then\s+controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(true|1)", st)
        if not m:
            continue
        key, n = m.group(1).lower(), int(m.group(2))
        if "plunger" in key or "lockbar" in key or "fire" in key:
            launch = launch if launch is not None else n
        elif "magna" in key or "save" in key or "extra" in key:
            buttons.append(n)
    # multi-line If blocks inside KeyDown
    for name, sts in body.items():
        if not name.endswith("_keydown"):
            continue
        cur = None
        for st in sts:
            m = re.match(r"(?i)if\s+(.*?)\s+then\s*$", st)
            if m:
                cur = m.group(1).lower()
                continue
            m = re.search(r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(true|1)", st)
            if m and cur:
                n = int(m.group(1))
                if "plunger" in cur or "lockbar" in cur:
                    launch = launch if launch is not None else n
                elif ("magna" in cur or "save" in cur) and n not in buttons:
                    buttons.append(n)
            if re.match(r"(?i)end\s+if", st):
                cur = None
    if launch is not None:
        g["launch_button"] = launch
    if buttons:
        g["buttons"] = sorted(set(buttons))

    # --- playfield switches from hit/spin events
    hit_sw, spin_sw, shooter, outl, init_closed = set(), set(), launch_stack_sw, [], set()
    decl = {}                                          # sub name -> its declaration line (with comment)
    for raw in script.replace("\r", "").split("\n"):
        m = re.match(r"(?i)\s*(?:private\s+|public\s+)?sub\s+(\w+)", raw)
        if m:
            decl.setdefault(m.group(1).lower(), raw.lower())
    pat_on = r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b"
    pat_hit = r"(?i)\b(?:pulsesw|dthit|sthit)\s*\(?\s*(\d+)"
    for sub, _, st in st_list:
        if not sub:
            continue
        m = re.search(r"(?i)_(hit|spin|slingshot)$", sub)
        if not m:
            continue
        nums = [int(n) for n in re.findall(pat_on, st)] + [int(n) for n in re.findall(pat_hit, st)]
        low = sub.lower() + " " + decl.get(sub.lower(), "")
        for n in nums:
            if m.group(1).lower() == "spin":
                spin_sw.add(n)
            hit_sw.add(n)
            if shooter is None and re.search(r"shoot|plunger|\bpls|swpls", low):
                shooter = n
            if "outlane" in low:
                outl.append(n)
    hit_sw |= lock_sw
    # switches set closed once at start-up (glass, lockdown...)
    for sub, w, st in st_list:
        if sub and sub.lower() in init_subs:
            pat = r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b"
            if (w or "").lower() == "controller":          # With Controller ... .Switch(22) = 1
                pat = r"(?i)^\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b"
            for n in re.findall(pat, st):
                init_closed.add(int(n))
    for _, w, st in st_list:                              # Controller.Switch(9)=1 outside any sub
        pass
    m_sw = set()
    if mechs or re.search(r"(?i)motortimer|mech", script):
        for v in mechs:
            for a in method(v, "addsw") + method(v, "addpulsesw"):
                m_sw.update(x for x in _ints(a)[:1])
    if shooter is None:                       # sw23_Hit: ... BIP = 1 (ball in plunger lane)
        for n, sts in body.items():
            m = re.fullmatch(r"(\w+)_hit", n)
            if not m:
                continue
            if any(re.search(r"(?i)\b(bip\w*|ball_?in_?plunger\w*|b?inplunger\w*|plungerball)\s*=\s*(?:1|true)\b", x)
                   for x in sts):
                ons = [int(x) for st in sts for x in re.findall(
                    r"(?i)controller\.switch\s*\(\s*(\d+)\s*\)\s*=\s*(?:true|1)\b", st)]
                if ons:
                    shooter = ons[0]
                    break
    if shooter is None:
        for raw in script.replace("\r", "").split("\n"):
            if re.search(r"(?i)(plunger|shooter)\s*lane", raw):
                m = re.search(r"(?i)\bsw(\d+)", raw)
                if m:
                    shooter = int(m.group(1))
                    break
    if shooter is None and "16" in re.findall(r"(?i)controller\.switch\s*\(\s*(\d+)", script) \
            and sysname in ("sega", "sega2", "sam"):
        shooter = 16
    if shooter is not None:
        g["shooter_lane"] = shooter
        rep.append(f"Shooter lane: {shooter}")
        if any(h["switch"] == shooter for h in holes):    # "only release if the lane is empty"
            holes = [h for h in holes if h["switch"] != shooter]
            if holes:
                g["holes"] = holes
            else:
                g.pop("holes", None)
            for i, line in enumerate(rep):
                if line.startswith("Holes/scoops: "):
                    rep[i] = "Holes/scoops: " + ", ".join(f"{h['switch']} (coil {h['coil']})" for h in holes)
    else:
        rep.append("! Shooter lane switch not found.")
    used = set(trough_sw) | {h["switch"] for h in holes} | {s for b in banks for s in b["switches"]}
    if eject:
        used.add(g["eject"]["switch"])
    if "outhole" in g:
        used.add(g["outhole"]["switch"])
    if shooter is not None:
        used.add(shooter)
    used |= set(g.get("buttons", [])) | ({launch} if launch is not None else set()) | m_sw | init_closed
    used |= {cab.get(k) for k in ("start", "coin", "tilt", "slam_tilt") if k in cab}
    used.discard(None)
    g["playfield"] = sorted(hit_sw - used)
    if spin_sw:
        g["spinners"] = sorted(spin_sw - used)
    if outl:
        g["outlanes"] = sorted(set(outl) - used)
    if init_closed - set(trough_sw):
        g["always_closed"] = sorted(init_closed - set(trough_sw))
        if g.get("holes") and any(h["switch"] in init_closed for h in g["holes"]):
            # balls the table loads into a lock at start-up (Apollo 13's 8-ball lock):
            # keep those switches closed rather than treating them as empty holes
            g["holes"] = [h for h in g["holes"] if h["switch"] not in init_closed]
            if not g["holes"]:
                del g["holes"]
            for i, line in enumerate(rep):
                if line.startswith("Holes/scoops: "):
                    rep[i] = "Holes/scoops: " + ", ".join(f"{h['switch']} (coil {h['coil']})"
                                                          for h in g.get("holes", []))
            rep.append("Closed at start-up (balls loaded in a lock, toy home positions): switches "
                       f"{sorted(s for s in init_closed if s not in trough_sw)}")
    rep.append(f"Playfield switches: {len(g['playfield'])} found")

    # --- things that need a person
    if mechs:
        rep.append(f"! Motor-driven toy(s) ({', '.join(sorted(mechs))}) - the harvester doesn't move them yet; "
                   "if the game reports a toy fault, this table needs a hand-made entry.")
    elif re.search(r"(?i)motortimer|\bmotor\b", script):
        rep.append("? The script mentions a motor - if there's a moving toy, the game may report a fault.")

    # --- memory map
    nv = nvram_map_for(rom)
    if nv:
        g["nvram_map"] = nv
        rep.append("Memory map: found (player/ball/score reading, lower high scores)")
    else:
        rep.append("Memory map: none for this ROM (runs fine, without the game-state extras)")

    g.update({"levels": 0, "pulse_ms": 60, "ball_time_s": [20, 90], "single_player_chance": 0.7,
              "drain": [], "boot_s": 12, "serve_timeout_s": 30})
    g["_comment"] = (f"Made automatically from the table script ({table or 'table'}) by "
                     "harvester/scriptgen.py. Check the report; edit freely.")
    g["_generated"] = True
    order = ["_comment", "_generated", "name", "roms"]
    return {k: g[k] for k in order if k in g} | {k: v for k, v in g.items() if k not in order}, rep


def nvram_map_for(rom: str | None) -> str | None:
    if not rom:
        return None
    direct = os.path.join(GAMES, "nvram_maps", f"{rom}.map.json")
    if os.path.exists(direct):
        return f"nvram_maps/{rom}.map.json"
    idx = os.path.join(GAMES, "nvram_maps", "index.json")
    if os.path.exists(idx):
        p = json.load(open(idx)).get(rom)
        if p:
            return "nvram_maps/" + p
    return None


def game_file_for(rom: str) -> str | None:
    """An existing game file (hand-made ones first) that lists this ROM."""
    found = None
    for p in sorted(glob.glob(os.path.join(GAMES, "*.json"))):
        if os.path.basename(p).startswith("_") or p.endswith(".digits.json"):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if rom in d.get("roms", []):
            if not d.get("_generated"):
                return p
            found = found or p
    return found
