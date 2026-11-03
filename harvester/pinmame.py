"""ctypes binding for libpinmame.

Checked against libpinmame.h / libpinmame.cpp / core.c from PinMAME master
(fetched 2026-10-30; a copy is in docs/libpinmame.h.reference). If your shared
library is from an older release, diff its header against that copy - the
PinmameConfig struct is the part that has changed in the past.

Facts confirmed from the source:
- DMD frames arrive as ONE BYTE PER PIXEL (size = width * height).
- layout.depth is 2 (4 shades: WPC, DE, Whitestar...) or 4 (16 shades: SAM,
  Spike, GTS3, ALVG...). It describes the value range, not bit packing.
- PINMAME_DMD_MODE_RAW gives the raw shade values the colorizing tools work
  with. BRIGHTNESS gives PWM-smoothed 0-255 luminance with blended frames,
  which is bad for dedupe, so we use RAW.
- The display callback is also called with data=NULL when nothing changed.
- Callbacks are __stdcall on Windows MSVC builds (only matters on 32-bit).
"""
from __future__ import annotations

import ctypes as C
import os
import queue
import threading
import struct
import sys
import time
from dataclasses import dataclass

PINMAME_MAX_PATH = 512

AUDIO_FORMAT_INT16 = 0
DMD_MODE_BRIGHTNESS = 0
DMD_MODE_RAW = 1
STATUS_OK = 0
STATUS_NAMES = {0: "OK", 1: "CONFIG_NOT_SET", 2: "GAME_NOT_FOUND", 3: "GAME_ALREADY_RUNNING",
                4: "EMULATOR_NOT_RUNNING", 5: "MECH_HANDLE_MECHANICS", 6: "MECH_NO_INVALID",
                7: "FILE_TYPE_INVALID", 8: "ROM_NOT_FOUND"}
DISPLAY_TYPE_DMD = 14
DISPLAY_TYPE_SEGMASK = 0x3F            # PinMAME compares (type & SEGMASK) == DMD

# __stdcall only exists on 32-bit Windows; everywhere else this is plain cdecl.
_IS_WIN32_X86 = sys.platform == "win32" and struct.calcsize("P") == 4
_FUNCTYPE = C.WINFUNCTYPE if _IS_WIN32_X86 else C.CFUNCTYPE


class NVRAMState(C.Structure):
    _fields_ = [("nvramNo", C.c_int), ("oldStat", C.c_uint8), ("currStat", C.c_uint8)]


class SolenoidState(C.Structure):
    _fields_ = [("solNo", C.c_int), ("state", C.c_int)]


class DisplayLayout(C.Structure):
    _fields_ = [
        ("type", C.c_int),             # PINMAME_DISPLAY_TYPE (plain C enum, int-sized)
        ("top", C.c_int32),
        ("left", C.c_int32),
        ("length", C.c_int32),
        ("width", C.c_int32),
        ("height", C.c_int32),
        ("depth", C.c_int32),
    ]


CB_STATE = _FUNCTYPE(None, C.c_int, C.c_void_p)
CB_DISPLAY_AVAILABLE = _FUNCTYPE(None, C.c_int, C.c_int, C.POINTER(DisplayLayout), C.c_void_p)
CB_DISPLAY_UPDATED = _FUNCTYPE(None, C.c_int, C.c_void_p, C.POINTER(DisplayLayout), C.c_void_p)
# (level, printf format, va_list, user data) - PinMAME's own messages, e.g. a missing file in the zip
CB_LOG = _FUNCTYPE(None, C.c_int, C.c_char_p, C.c_void_p, C.c_void_p)
CB_SOLENOID = _FUNCTYPE(None, C.POINTER(SolenoidState), C.c_void_p)


def _vsnprintf():
    """The C library's vsnprintf, to turn PinMAME's (format, va_list) into text."""
    try:
        if sys.platform == "win32":
            f = C.cdll.msvcrt._vsnprintf
        else:
            import ctypes.util
            f = C.CDLL(ctypes.util.find_library("c")).vsnprintf
        f.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p, C.c_void_p]
        f.restype = C.c_int
        return f
    except Exception:
        return None


class PinmameConfig(C.Structure):
    _fields_ = [
        ("audioFormat", C.c_int),
        ("sampleRate", C.c_int),
        ("vpmPath", C.c_char * PINMAME_MAX_PATH),
        ("cb_OnStateUpdated", CB_STATE),
        ("cb_OnDisplayAvailable", CB_DISPLAY_AVAILABLE),
        ("cb_OnDisplayUpdated", CB_DISPLAY_UPDATED),
        # Unused callbacks stay NULL; libpinmame checks each one before calling.
        ("cb_OnAudioAvailable", C.c_void_p),
        ("cb_OnAudioUpdated", C.c_void_p),
        ("cb_OnMechAvailable", C.c_void_p),
        ("cb_OnMechUpdated", C.c_void_p),
        ("cb_OnSolenoidUpdated", CB_SOLENOID),
        ("cb_OnConsoleDataUpdated", C.c_void_p),
        ("fn_IsKeyPressed", C.c_void_p),
        ("cb_OnLogMessage", CB_LOG),
        ("cb_OnSoundCommand", C.c_void_p),
    ]


@dataclass
class RawFrame:
    t_ms: int
    width: int
    height: int
    depth: int               # 2 -> values 0..3, 4 -> values 0..15
    pixels: bytes            # one byte per pixel, raw shade value


class PinMame:
    """One emulator per process (libpinmame is a singleton)."""

    def __init__(self, lib_path: str | None = None, vpm_path: str | None = None,
                 dmd_index: int | None = None):
        lib_path = lib_path or os.environ.get("LIBPINMAME")
        if not lib_path:
            sys.exit("Set LIBPINMAME to the path of the libpinmame shared library.")
        self.lib = C.CDLL(lib_path)
        self._bind()
        self.frames: "queue.Queue[RawFrame]" = queue.Queue(maxsize=10000)
        self.dropped = 0
        self.running = False
        self.dmd_index = dmd_index      # None = first DMD that reports in
        self._t0 = time.monotonic()

        # Keep references to callbacks so they aren't garbage-collected.
        self._cb_state = CB_STATE(self._on_state)
        self._cb_avail = CB_DISPLAY_AVAILABLE(self._on_display_available)
        self._cb_update = CB_DISPLAY_UPDATED(self._on_display_updated)
        self._cb_log = CB_LOG(self._on_log)
        self._cb_sol = CB_SOLENOID(self._on_solenoid)
        self._sol_level: dict[int, int] = {}
        self._sol_rises: list[int] = []        # coils that switched on since pop_rises() (the
        self._sol_lock = threading.Lock()      # callback sees pulses too short to catch by polling)
        self.sol_events = False                # True once libpinmame has reported a coil
        self._fmt = _vsnprintf()
        self.messages: list[str] = []            # PinMAME's own log, newest last
        self.got_display = False

        cfg = PinmameConfig()
        cfg.audioFormat = AUDIO_FORMAT_INT16
        cfg.sampleRate = 44100
        path = vpm_path or os.environ.get("PINMAME_PATH", "")
        if path and not path.endswith(("/", "\\")):
            path += os.sep
        cfg.vpmPath = path.encode()
        cfg.cb_OnStateUpdated = self._cb_state
        cfg.cb_OnDisplayAvailable = self._cb_avail
        cfg.cb_OnDisplayUpdated = self._cb_update
        cfg.cb_OnSolenoidUpdated = self._cb_sol
        if not os.environ.get("HARVESTER_NO_PINMAME_LOG"):
            cfg.cb_OnLogMessage = self._cb_log
        self._cfg = cfg
        self.lib.PinmameSetConfig(C.byref(self._cfg))   # libpinmame copies the struct
        self.lib.PinmameSetDmdMode(DMD_MODE_RAW)
        self.lib.PinmameSetHandleKeyboard(0)
        self.lib.PinmameSetHandleMechanics(0xFF)       # let PinMAME simulate built-in mechs

    def _bind(self):
        L = self.lib
        L.PinmameSetConfig.argtypes = [C.POINTER(PinmameConfig)]
        L.PinmameSetConfig.restype = None
        L.PinmameRun.argtypes = [C.c_char_p]
        L.PinmameRun.restype = C.c_int
        L.PinmameIsRunning.restype = C.c_int
        L.PinmameSetSwitch.argtypes = [C.c_int, C.c_int]
        L.PinmameSetSwitch.restype = None
        L.PinmameGetSwitch.argtypes = [C.c_int]
        L.PinmameGetSwitch.restype = C.c_int
        L.PinmameSetDmdMode.argtypes = [C.c_int]
        L.PinmameSetHandleKeyboard.argtypes = [C.c_int]
        L.PinmameSetHandleMechanics.argtypes = [C.c_int]
        L.PinmameStop.restype = None
        L.PinmameReadMainCPUMemory.argtypes = [C.c_uint32, C.POINTER(C.c_uint8), C.c_int]
        L.PinmameReadMainCPUMemory.restype = C.c_int
        L.PinmameGetRawMemoryRegion.argtypes = [C.c_int]
        L.PinmameGetRawMemoryRegion.restype = C.c_void_p
        L.PinmameGetRawMemoryRegionLength.argtypes = [C.c_int]
        L.PinmameGetRawMemoryRegionLength.restype = C.c_size_t
        L.PinmameGetSolenoid.argtypes = [C.c_int]
        L.PinmameGetSolenoid.restype = C.c_int
        # Optional extras (present in current builds):
        # PinmameSetRecord/PinmameSetPlayback(const char*) -> record and replay switch input
        # PinmameGetRawMemoryRegion(int) / ...Length(int)  -> read ROM data (useful for Phase 2)

    # --- callbacks: run on the emulator thread, so keep them short ----------

    def _on_state(self, state, _user):
        self.running = bool(state)

    def _on_solenoid(self, st_p, _user):
        try:
            st = st_p.contents
            prev = self._sol_level.get(st.solNo, 0)
            self._sol_level[st.solNo] = st.state
            if st.state > 0 and prev <= 0:
                with self._sol_lock:
                    self._sol_rises.append(st.solNo)
            self.sol_events = True
        except Exception:
            pass

    def pop_rises(self) -> list[int]:
        """Coils that switched on since the last call, in order (may repeat)."""
        with self._sol_lock:
            r, self._sol_rises = self._sol_rises, []
        return r

    def _on_log(self, level, fmt, args, _user):
        try:
            text = (fmt or b"").decode("latin-1", "replace")
            if self._fmt is not None and args and "%" in text:
                buf = C.create_string_buffer(1024)
                self._fmt(buf, len(buf) - 1, fmt, args)
                text = buf.value.decode("latin-1", "replace")
            text = " ".join(text.split())
            if not text or (self.messages and self.messages[-1] == text):
                return
            self.messages.append(text)
            del self.messages[:-40]
            if level >= 2 or len(self.messages) <= 25:   # errors always; info only at start-up
                print(f"[pinmame{' ERROR' if level >= 2 else ''}] {text}")
        except Exception:
            pass

    def _on_display_available(self, index, count, layout_p, _user):
        self.got_display = True
        lay = layout_p.contents
        is_dmd = (lay.type & DISPLAY_TYPE_SEGMASK) == DISPLAY_TYPE_DMD
        print(f"[pinmame] display {index + 1}/{count}: type={lay.type:#x} "
              f"{'DMD ' if is_dmd else ''}{lay.width}x{lay.height} depth={lay.depth}")
        if is_dmd and self.dmd_index is None:
            self.dmd_index = index

    def _on_display_updated(self, index, data_p, layout_p, _user):
        if not data_p or index != self.dmd_index:      # NULL = unchanged
            return
        lay = layout_p.contents
        pixels = C.string_at(data_p, lay.width * lay.height)   # copy before returning
        try:
            self.frames.put_nowait(RawFrame(self.now_ms(), lay.width, lay.height, lay.depth, pixels))
        except queue.Full:
            self.dropped += 1

    # --- control -------------------------------------------------------------

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def start(self, rom: str, timeout_s: float = 15.0):
        status = self.lib.PinmameRun(rom.encode())
        if status != STATUS_OK:
            sys.exit(f"PinmameRun({rom}) failed: {STATUS_NAMES.get(status, status)}. "
                     f"Check PINMAME_PATH (it should contain a 'roms' folder).")
        # PinmameIsRunning(): 0 = stopped, 2 = starting (machine not built yet,
        # touching switches crashes), 1 = fully running. Wait for exactly 1.
        deadline = time.monotonic() + timeout_s
        seen_starting = False
        while self.lib.PinmameIsRunning() != 1:
            state = self.lib.PinmameIsRunning()
            seen_starting = seen_starting or state == 2
            if time.monotonic() > deadline or (seen_starting and state == 0):
                why = ("PinMAME stopped while starting" if state == 0 else
                       f"PinMAME didn't finish starting within {timeout_s:.0f} s")
                last = "; ".join(self.messages[-6:])
                sys.exit(f"{why} {rom}. This usually means a file inside {rom}.zip is missing or "
                         f"doesn't match (often the sound or DMD ROMs), or this build of libpinmame "
                         f"doesn't run this machine."
                         + (f"\nPinMAME said: {last}" if last else ""))
            time.sleep(0.05)
        time.sleep(0.5)                                # let the core settle
        self._t0 = time.monotonic()

    def set_switch(self, number: int, closed: bool):
        if self.lib.PinmameIsRunning() == 1:
            self.lib.PinmameSetSwitch(number, 1 if closed else 0)

    def read_memory(self, address: int, size: int) -> bytes | None:
        """Read the main CPU's memory (RAM/NVRAM); None if not available.

        Reads the main CPU's memory region directly (REGION_CPU1 = 0x81).
        PinmameReadMainCPUMemory goes through whichever CPU the emulator is
        running at that moment, so on boards with a sound CPU (Data East)
        it sometimes returns the sound CPU's memory, and on Whitestar it
        returns nothing. It's only the fallback."""
        if self.lib.PinmameIsRunning() != 1:
            return None
        REGION_CPU1 = 0x81
        base = self.lib.PinmameGetRawMemoryRegion(REGION_CPU1)
        length = self.lib.PinmameGetRawMemoryRegionLength(REGION_CPU1)
        if base and address + size <= length:
            return C.string_at(base + address, size)
        buf = (C.c_uint8 * size)()
        got = self.lib.PinmameReadMainCPUMemory(address, buf, size)
        return bytes(buf[:got]) if got == size else None

    def read_nvram(self) -> bytes | None:
        """The game's battery-backed memory, exactly as PinMAME would save it to the .nv file
        (memory maps describe positions in this). None if this libpinmame can't do it."""
        if self.lib.PinmameIsRunning() != 1:
            return None
        try:
            if not hasattr(self, "_nv_buf"):
                f = self.lib.PinmameGetMaxNVRAM
                f.restype = C.c_int
                n = f()
                if n <= 0:
                    return None
                self._nv_buf = (NVRAMState * n)()
                self.lib.PinmameGetNVRAM.argtypes = [C.POINTER(NVRAMState)]
                self.lib.PinmameGetNVRAM.restype = C.c_int
            got = self.lib.PinmameGetNVRAM(self._nv_buf)
            if got <= 0:
                return None
            import numpy as np
            arr = np.frombuffer(self._nv_buf, dtype=np.uint8).reshape(-1, C.sizeof(NVRAMState))
            return arr[:got, C.sizeof(C.c_int) + 1].tobytes()   # currStat of each entry
        except (AttributeError, OSError):
            return None

    def cpu_region(self, start: int = 0, size: int | None = None) -> bytes | None:
        """Part of the main CPU's memory region (to line .nv positions up with live memory).
        Whole region if size is None, but only when it's small (SAM's is 80 MB)."""
        if self.lib.PinmameIsRunning() != 1:
            return None
        base = self.lib.PinmameGetRawMemoryRegion(0x81)
        length = self.lib.PinmameGetRawMemoryRegionLength(0x81)
        if not base or not length:
            return None
        if size is None:
            if length > 4 * 1024 * 1024:
                return None
            size = length - start
        if start < 0 or start + size > length:
            return None
        return C.string_at(base + start, size)

    def write_memory(self, address: int, data: bytes) -> bool:
        """Write into the main CPU's RAM while the game runs (used to lower
        the high-score table the game actually uses)."""
        if self.lib.PinmameIsRunning() != 1:
            return False
        REGION_CPU1 = 0x81
        base = self.lib.PinmameGetRawMemoryRegion(REGION_CPU1)
        length = self.lib.PinmameGetRawMemoryRegionLength(REGION_CPU1)
        if not base or address + len(data) > length:
            return False
        C.memmove(base + address, data, len(data))
        return True

    def solenoid_on(self, number: int) -> bool:
        """Any value above 0 counts as on (modulated outputs report a level)."""
        return self.lib.PinmameIsRunning() == 1 and self.lib.PinmameGetSolenoid(number) > 0

    def stop(self):
        self.lib.PinmameStop()
