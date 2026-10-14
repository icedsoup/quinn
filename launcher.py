"""
launcher.py — Project Quinn mission control.

A terminal launcher that starts/stops each component.  Bridge and Quinn
Discord run in this same terminal window (their output is piped in with
color-coded prefixes so you can watch everything in one place).  SillyTavern
and the Sniper each open their own window because they have their own UIs.

Managed components:
  [S] Karuta Sniper     main.py                        → own window
  [Q] Quinn Discord     quinn_discord.py               → this terminal
  [B] Bridge Server     QuinnBridge/bridge/server.py   → this terminal
  [T] SillyTavern       configurable via quinn_config.json → own window

Launch combos:
  [1]  Sniper only
  [2]  Quinn setup  →  bridge + SillyTavern + Quinn Discord
  [3]  Everything   →  bridge + SillyTavern + Quinn Discord + Sniper

Bridge token note:
  After the bridge starts for the first time it writes bridge_token.txt.
  The launcher shows that token so you can paste it into SillyTavern's
  Quinn Bridge extension panel (Settings → Extensions → Quinn Bridge).
  You only need to do this once — SillyTavern stores it in localStorage
  and auto-starts polling on every future load.

Usage:
  python launcher.py
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from colorama import Back, Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ─── constants ────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent
IS_WIN     = platform.system() == "Windows"
QUINN_CFG  = ROOT / "quinn_config.json"
BRIDGE_TOK = ROOT / "QuinnBridge" / "bridge" / "bridge_token.txt"

VERSION    = "1.1"

# ─── color helpers ────────────────────────────────────────────────────────────

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + Style.RESET_ALL

accent = lambda t: _c(t, Fore.CYAN)
dim    = lambda t: _c(t, Style.DIM)
hi     = lambda t: _c(t, Style.BRIGHT)
ok_    = lambda t: _c(t, Fore.GREEN)
warn_  = lambda t: _c(t, Fore.YELLOW)
err_   = lambda t: _c(t, Fore.RED)
key    = lambda t: _c(t, Fore.YELLOW + Style.BRIGHT)
pfx_b  = lambda t: _c(t, Fore.BLUE   + Style.BRIGHT)   # bridge prefix
pfx_q  = lambda t: _c(t, Fore.MAGENTA + Style.BRIGHT)  # quinn prefix

def pill(text: str, on: bool) -> str:
    return _c(f" {text} ", Fore.BLACK + (Back.GREEN if on else Back.RED))

# ─── live log buffer (bridge + quinn pipe here) ───────────────────────────────
# A ring-buffer of the last 20 lines from piped subprocesses.
# Background reader threads append; draw() reads under the lock.

_LOG: deque[str] = deque(maxlen=20)
_LOG_LOCK        = threading.Lock()

# Suppress bridge's constant /v1/next polling spam — 1 poll/sec is noise.
_BRIDGE_POLL_NOISE = "/v1/next"


def _log_push(line: str) -> None:
    with _LOG_LOCK:
        _LOG.append(line)


def _pipe_reader(proc: subprocess.Popen, label: str, color_fn) -> None:
    """
    Daemon thread: reads piped subprocess output line-by-line and pushes
    formatted lines into _LOG.  Exits cleanly when the process ends.
    """
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            # Suppress bridge polling noise so the log stays readable
            if label == "[bridge]" and _BRIDGE_POLL_NOISE in line:
                continue
            ts = time.strftime("%H:%M:%S")
            _log_push(f"  {dim(ts)} {color_fn(label):<20} {line}")
    except Exception:
        pass


# ─── component model ──────────────────────────────────────────────────────────

@dataclass
class Component:
    """
    One managed process.

    open_window=True  → launches in its own console (SillyTavern, Sniper)
    open_window=False → runs windowless with stdout piped into the log buffer
                        (Bridge, Quinn Discord)

    health_check      → optional callable that returns True when the service
                        is reachable.  Used for processes (like ST) whose
                        launcher wrapper exits immediately but whose service
                        stays up.
    """
    label       : str
    log_prefix  : str                      = ""
    log_color   : Callable                 = staticmethod(accent)
    open_window : bool                     = False
    health_check: Optional[Callable[[], bool]] = None

    _proc  : Optional[subprocess.Popen]   = field(default=None, init=False, repr=False)
    _reader: Optional[threading.Thread]   = field(default=None, init=False, repr=False)

    @property
    def alive(self) -> bool:
        # If we have a health check, prefer it (e.g. ST's node server stays
        # running after the cmd.exe launcher process exits).
        if self.health_check is not None:
            return self.health_check()
        return self._proc is not None and self._proc.poll() is None

    # ── spawn ──

    def start_python(self, script: Path, *args: str) -> bool:
        # -u = unbuffered stdout/stderr so piped output appears immediately
        return self._spawn([sys.executable, "-u", str(script)] + list(args))

    def start_cmd(self, cmd: list[str], cwd: Optional[Path] = None) -> bool:
        return self._spawn(cmd, cwd=cwd)

    def _spawn(self, cmd: list[str], cwd: Optional[Path] = None) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True   # already running

        kwargs: dict = {"cwd": str(cwd or ROOT)}

        if self.open_window:
            # Own console window (Sniper, SillyTavern)
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL
        else:
            # Pipe into launcher terminal
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
        except Exception as exc:
            print(err_(f"\n  Could not start {self.label}: {exc}"))
            return False

        # Start the reader thread for piped processes
        if not self.open_window and self._reader is None:
            self._reader = threading.Thread(
                target=_pipe_reader,
                args=(self._proc, self.log_prefix, self.log_color),
                daemon=True,
                name=f"reader-{self.label}",
            )
            self._reader.start()

        return True

    # ── stop ──

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc   = None
        self._reader = None

    # ── display ──

    def status_line(self) -> str:
        up    = self.alive
        dot   = ok_("●") if up else dim("○")
        state = pill("RUNNING", True) if up else dim("stopped")
        return f"  {dot}  {self.label:<26} {state}"


# ─── registry ─────────────────────────────────────────────────────────────────

COMPS: dict[str, Component] = {
    "sniper": Component(
        "Karuta Sniper",
        log_prefix="[sniper]",
        log_color=warn_,
        open_window=True,
    ),
    "bridge": Component(
        "Bridge Server",
        log_prefix="[bridge]",
        log_color=pfx_b,
        open_window=False,   # pipes into launcher terminal
    ),
    "quinn": Component(
        "Quinn Discord",
        log_prefix="[quinn]",
        log_color=pfx_q,
        open_window=False,   # pipes into launcher terminal
    ),
    "tavern": Component(
        "SillyTavern",
        log_prefix="[tavern]",
        log_color=ok_,
        open_window=True,    # needs its own window (opens a browser)
        health_check=None,   # set after sillytavern_healthy is defined below
    ),
}

# ─── health checks ────────────────────────────────────────────────────────────

def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def bridge_healthy() -> bool:
    return _http_ok("http://127.0.0.1:8765/health")


def sillytavern_healthy() -> bool:
    for url in ("http://127.0.0.1:8000/", "http://127.0.0.1:8001/"):
        if _http_ok(url, timeout=2.0):
            return True
    return False


def kobold_healthy() -> bool:
    # Different KoboldCpp versions expose different endpoints — try them all.
    for url in (
        "http://127.0.0.1:5001/api/v1/info",
        "http://127.0.0.1:5001/api/extra/version",
        "http://127.0.0.1:5001/",
    ):
        if _http_ok(url, timeout=2.5):
            return True
    return False


# Wire up ST health check now that the function is defined
COMPS["tavern"].health_check = sillytavern_healthy

# ─── config helpers ───────────────────────────────────────────────────────────

def _read_quinn_cfg() -> dict:
    if QUINN_CFG.exists():
        with QUINN_CFG.open(encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _tavern_start_cmd() -> Optional[list[str]]:
    """
    Find a SillyTavern start command.
    Priority:
      1. quinn_config.json → "sillytavern_start"  (path string or cmd list)
      2. Relative paths from repo root
      3. Common absolute Windows paths
    """
    cfg = _read_quinn_cfg()
    explicit = cfg.get("sillytavern_start")
    if explicit:
        if isinstance(explicit, list):
            return explicit
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if p.suffix.lower() == ".bat":
            return ["cmd", "/c", str(p)]
        return [str(p)]

    rel_candidates = (
        "SillyTavern/start.bat",
        "SillyTavern/Start SillyTavern.bat",
        "../SillyTavern/start.bat",
        "../SillyTavern/Start SillyTavern.bat",
    )
    for candidate in rel_candidates:
        p = ROOT / candidate
        if p.exists():
            return ["cmd", "/c", str(p)]

    home = Path.home()
    abs_candidates = (
        home / "SillyTavern" / "start.bat",
        home / "SillyTavern" / "Start SillyTavern.bat",
        Path("C:/SillyTavern/start.bat"),
        Path("C:/SillyTavern/Start SillyTavern.bat"),
        Path("D:/SillyTavern/start.bat"),
        Path("D:/SillyTavern/Start SillyTavern.bat"),
    )
    for p in abs_candidates:
        if p.exists():
            return ["cmd", "/c", str(p)]

    return None


def _bridge_token_str() -> Optional[str]:
    if BRIDGE_TOK.exists():
        return BRIDGE_TOK.read_text(encoding="utf-8").strip()
    return None


def _wait_bridge_token(max_wait: float = 8.0) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if BRIDGE_TOK.exists():
            return True
        time.sleep(0.35)
    return False


def _kill_st_port() -> None:
    """
    Best-effort: kill whatever process is listening on ST's port (8000/8001).
    ST's start.bat launches node, which becomes orphaned when cmd.exe exits,
    so we can't kill it through the Popen handle.
    """
    if not IS_WIN:
        return
    for port in (8000, 8001):
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                    pid = parts[4]
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/F"],
                        capture_output=True, timeout=5,
                    )
        except Exception:
            pass

# ─── draw ─────────────────────────────────────────────────────────────────────

_BANNER = """\
  ╔════════════════════════════════════════════╗
  ║  ◈  PROJECT QUINN  ·  Mission Control      ║
  ╚════════════════════════════════════════════╝"""


def _cls():
    os.system("cls" if IS_WIN else "clear")


def draw(state: dict):
    _cls()
    dry = state["dry_run"]
    print()
    print(accent(_BANNER))
    print(dim(f"  launcher v{VERSION}"))
    print()

    # ── managed ──
    print(dim("  ── MANAGED ──────────────────────────────────────────"))
    for comp in COMPS.values():
        print(comp.status_line())
    print()

    # ── external ──
    print(dim("  ── EXTERNAL (you start these) ───────────────────────"))
    for name, alive in [("KoboldCpp", kobold_healthy())]:
        dot    = ok_("●") if alive else dim("○")
        state_ = ok_("reachable") if alive else dim("not detected")
        print(f"  {dot}  {name:<26} {state_}")
    print()

    # ── quinn mode ──
    mode = warn_(" DRY-RUN ") if dry else ok_("  LIVE  ")
    print(f"  Quinn mode: {mode}")
    if dry:
        print(dim("  (Quinn logs messages but never sends replies)"))

    # ── bridge token reminder ──
    tok = _bridge_token_str()
    if tok:
        print()
        print(dim("  ── BRIDGE TOKEN (paste into SillyTavern extension) ──"))
        print(f"  {accent(tok)}")
        print(dim("  SillyTavern → Extensions → Quinn Bridge → paste → Save"))
        print(dim("  (one-time setup — token persists in localStorage after that)"))

    # ── controls ──
    print()
    print(dim("  ── LAUNCH ───────────────────────────────────────────"))
    print(f"  {key('[1]')}  Sniper only")
    print(f"  {key('[2]')}  Quinn setup   (bridge + ST + Quinn Discord)")
    print(f"  {key('[3]')}  Everything    (bridge + ST + Quinn + Sniper)")
    print(f"  {key('[t]')}  SillyTavern   (standalone)")
    print()
    print(dim("  ── CONTROLS ─────────────────────────────────────────"))
    print(f"  {key('[s]')}  Stop all managed components")
    print(f"  {key('[d]')}  Toggle dry-run  (Quinn is {'DRY' if dry else 'LIVE'})")
    print(f"  {key('[r]')}  Refresh status")
    print(f"  {key('[q]')}  Quit launcher   (components keep running)")
    print()

    # ── live log (bridge + quinn pipe here) ──
    with _LOG_LOCK:
        lines = list(_LOG)

    if lines:
        print(dim("  ── LOG ──────────────────────────────────────────────"))
        for line in lines:
            print(line)
        print()

    print(accent("  ▶ "), end="", flush=True)


# ─── launch actions ───────────────────────────────────────────────────────────

def do_sniper() -> bool:
    if COMPS["sniper"].alive:
        print(warn_("\n  Sniper already running."))
        return False
    ok = COMPS["sniper"].start_python(ROOT / "main.py")
    if ok:
        print(ok_("\n  ✓ Sniper launched in new window."))
    return ok


def do_bridge() -> bool:
    """Start bridge server; wait for its token file before returning."""
    if bridge_healthy():
        return True   # already up (maybe from a previous session)
    if COMPS["bridge"].alive:
        return True
    ok = COMPS["bridge"].start_python(ROOT / "QuinnBridge" / "bridge" / "server.py")
    if not ok:
        return False
    print(ok_("\n  ✓ Bridge launched."), end="  ")
    print(dim("Waiting for token…"), end="", flush=True)
    if _wait_bridge_token():
        tok = _bridge_token_str()
        print(ok_(" ready"))
        print(f"  Bridge token: {accent(tok)}")
        print(dim("  → Paste into SillyTavern extension if not already done."))
    else:
        print(warn_(" timed out — bridge may not have written its token yet"))
    return True


def do_tavern() -> bool:
    if sillytavern_healthy():
        _log_push(f"  {dim(time.strftime('%H:%M:%S'))} {ok_('[tavern]'):<20} Already running — skipping launch.")
        return True
    if COMPS["tavern"].alive:
        print(warn_("\n  SillyTavern already launched."))
        return True
    cmd = _tavern_start_cmd()
    if cmd is None:
        print(err_("\n  SillyTavern start script not found."))
        print(dim("  Add the exact path to quinn_config.json:"))
        print(dim('  "sillytavern_start": "C:\\\\SillyTavern\\\\start.bat"'))
        print(dim("  You can still launch it manually."))
        return False
    ok = COMPS["tavern"].start_cmd(cmd)
    if ok:
        print(ok_("\n  ✓ SillyTavern launched in new window."))
        print(dim("  (Waiting for ST to become reachable…)"), end="", flush=True)
        # Give ST ~20 s to start before we continue
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if sillytavern_healthy():
                print(ok_(" up!"))
                break
            time.sleep(0.8)
        else:
            print(warn_(" still not reachable — continuing anyway"))
    return ok


def do_quinn(dry_run: bool) -> bool:
    if not QUINN_CFG.exists():
        print(err_("\n  quinn_config.json not found."))
        print(dim("  Copy quinn_config_template.json → quinn_config.json and fill in your token."))
        return False
    if not do_bridge():
        return False
    if COMPS["quinn"].alive:
        print(warn_("  Quinn already running."))
        return False
    args = ("--dry-run",) if dry_run else ()
    ok = COMPS["quinn"].start_python(ROOT / "quinn_discord.py", *args)
    if ok:
        print(ok_("  ✓ Quinn Discord launched (output appears in log below)."))
    return ok


def do_stop_all():
    for comp in COMPS.values():
        comp.stop()
    # ST's node process may outlive the cmd.exe launcher — try to kill by port
    _kill_st_port()
    print(err_("\n  All managed components stopped."))


# ─── main loop ────────────────────────────────────────────────────────────────

def main():
    if IS_WIN:
        signal.signal(signal.SIGINT,   lambda *_: None)
        signal.signal(signal.SIGBREAK, lambda *_: None)

    state    = {"dry_run": False}
    pause_ms = 0.0

    while True:
        if pause_ms:
            time.sleep(pause_ms)
            pause_ms = 0.0
        draw(state)

        try:
            choice = input().strip().lower()
        except (KeyboardInterrupt, EOFError):
            do_stop_all()
            print(accent("\n  Bye.\n"))
            sys.exit(0)

        match choice:
            case "1":
                do_sniper()
                pause_ms = 1.2
            case "2":
                # Bridge → ST → Quinn (ST needs to be up before Quinn polls it)
                if do_bridge():
                    do_tavern()
                    do_quinn(state["dry_run"])
                pause_ms = 1.5
            case "3":
                if do_bridge():
                    do_tavern()
                    do_quinn(state["dry_run"])
                time.sleep(0.3)
                do_sniper()
                pause_ms = 1.5
            case "t":
                do_tavern()
                pause_ms = 1.2
            case "s":
                do_stop_all()
                pause_ms = 1.2
            case "d":
                state["dry_run"] = not state["dry_run"]
            case "q":
                print(accent("\n  Launcher closed.  Components keep running.\n"))
                sys.exit(0)
            case _:
                pass   # unknown input → silent redraw


if __name__ == "__main__":
    main()