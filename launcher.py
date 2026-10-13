"""
launcher.py — Project Quinn mission control.

A terminal launcher that starts/stops each component in its own console
window so their outputs never tangle.  KoboldCpp is intentionally excluded
(you launch it manually as always).

Managed components:
  [S] Karuta Sniper     main.py
  [Q] Quinn Discord     quinn_discord.py          (also starts bridge)
  [B] Bridge Server     QuinnBridge/bridge/server.py
  [T] SillyTavern       configurable via quinn_config.json → sillytavern_start

Launch combos:
  [1]  Sniper only
  [2]  Quinn only   →  bridge + Quinn Discord
  [3]  Both         →  bridge + Quinn Discord + Sniper

Bridge token note:
  After the bridge starts for the first time it writes bridge_token.txt.
  The launcher shows that token so you can paste it into SillyTavern's
  Quinn Bridge extension panel (Settings → Extensions → Quinn Bridge).
  You only need to do this once; SillyTavern stores it in sessionStorage.

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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from colorama import Back, Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ─── constants ────────────────────────────────────────────────────────────────

ROOT      = Path(__file__).parent
IS_WIN    = platform.system() == "Windows"
QUINN_CFG = ROOT / "quinn_config.json"
BRIDGE_TOK= ROOT / "QuinnBridge" / "bridge" / "bridge_token.txt"

VERSION   = "1.0"

# ─── color helpers ────────────────────────────────────────────────────────────
# Single place to change the palette — every UI element routes through these.

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + Style.RESET_ALL

accent = lambda t: _c(t, Fore.CYAN)
dim    = lambda t: _c(t, Style.DIM)
hi     = lambda t: _c(t, Style.BRIGHT)
ok_    = lambda t: _c(t, Fore.GREEN)
warn_  = lambda t: _c(t, Fore.YELLOW)
err_   = lambda t: _c(t, Fore.RED)
key    = lambda t: _c(t, Fore.YELLOW + Style.BRIGHT)

def pill(text: str, on: bool) -> str:
    """Colored status pill."""
    return _c(f" {text} ", Fore.BLACK + (Back.GREEN if on else Back.RED))

# ─── component model ──────────────────────────────────────────────────────────

@dataclass
class Component:
    """
    One managed process.  open_window=True gives it a separate console so
    KarutaConsole / Quinn logs / bridge logs don't collide with this UI.
    """
    label      : str
    _proc      : Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    open_window: bool = True

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── spawn ──

    def start_python(self, script: Path, *args: str) -> bool:
        """Launch a Python script, returns True if Popen succeeded."""
        return self._spawn([sys.executable, str(script)] + list(args))

    def start_cmd(self, cmd: list[str], cwd: Optional[Path] = None) -> bool:
        """Launch an arbitrary command (e.g. a .bat file)."""
        return self._spawn(cmd, cwd=cwd)

    def _spawn(self, cmd: list[str], cwd: Optional[Path] = None) -> bool:
        if self.alive:
            return True
        kwargs: dict = {"cwd": str(cwd or ROOT)}
        if self.open_window:
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
            return True
        except Exception as exc:
            print(err_(f"\n  Could not start {self.label}: {exc}"))
            return False

    # ── stop ──

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    # ── display ──

    def status_line(self) -> str:
        dot    = ok_("●") if self.alive else dim("○")
        state  = pill("RUNNING", True) if self.alive else dim("stopped")
        return f"  {dot}  {self.label:<26} {state}"


# ─── registry ─────────────────────────────────────────────────────────────────

COMPS: dict[str, Component] = {
    "sniper":  Component("Karuta Sniper"),
    "bridge":  Component("Bridge Server", open_window=True),
    "quinn":   Component("Quinn Discord"),
    "tavern":  Component("SillyTavern"),
}

# ─── health checks ────────────────────────────────────────────────────────────

def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def bridge_healthy()      -> bool: return _http_ok("http://127.0.0.1:8765/health")
def sillytavern_healthy() -> bool: return _http_ok("http://127.0.0.1:8000/", timeout=1.5)
def kobold_healthy()      -> bool: return _http_ok("http://127.0.0.1:5001/api/v1/info", timeout=1.5)

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
      1. quinn_config.json → "sillytavern_start"   (path string or cmd list)
      2. SillyTavern/start.bat                       (default install location)
      3. SillyTavern/Start SillyTavern.bat
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

    for candidate in ("SillyTavern/start.bat", "SillyTavern/Start SillyTavern.bat"):
        p = ROOT / candidate
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
    ext = [("KoboldCpp", kobold_healthy())]
    for name, alive in ext:
        dot   = ok_("●") if alive else dim("○")
        state_= ok_("reachable") if alive else dim("not detected")
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
        print(dim("  SillyTavern → Extensions → Quinn Bridge → paste → Test"))

    print()
    print(dim("  ── LAUNCH ───────────────────────────────────────────"))
    print(f"  {key('[1]')}  Sniper only")
    print(f"  {key('[2]')}  Quinn only     (bridge + Quinn Discord)")
    print(f"  {key('[3]')}  Both           (bridge + Quinn + Sniper)")
    print(f"  {key('[t]')}  SillyTavern    (standalone)")
    print()
    print(dim("  ── CONTROLS ─────────────────────────────────────────"))
    print(f"  {key('[s]')}  Stop all managed components")
    print(f"  {key('[d]')}  Toggle dry-run  (Quinn is {'DRY' if dry else 'LIVE'})")
    print(f"  {key('[r]')}  Refresh")
    print(f"  {key('[q]')}  Quit launcher   (components keep running)")
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
    """Start bridge server; waits for the token file to appear."""
    if COMPS["bridge"].alive:
        return True
    ok = COMPS["bridge"].start_python(ROOT / "QuinnBridge" / "bridge" / "server.py")
    if not ok:
        return False
    print(ok_("\n  ✓ Bridge server launched."), end="")
    print(dim("  Waiting for token…"), end="", flush=True)
    if _wait_bridge_token():
        tok = _bridge_token_str()
        print(ok_(" ready"))
        print(f"  Bridge token: {accent(tok)}")
        print(dim("  → Paste into SillyTavern extension settings if not already done."))
    else:
        print(warn_(" timed out — bridge may not have written its token yet"))
    return True


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
        print(ok_("  ✓ Quinn Discord launched in new window."))
    return ok


def do_tavern() -> bool:
    if COMPS["tavern"].alive:
        print(warn_("\n  SillyTavern already running."))
        return False
    cmd = _tavern_start_cmd()
    if cmd is None:
        print(err_("\n  SillyTavern start command not found."))
        print(dim("  Add sillytavern_start to quinn_config.json:"))
        print(dim('  "sillytavern_start": "SillyTavern\\\\start.bat"'))
        return False
    ok = COMPS["tavern"].start_cmd(cmd)
    if ok:
        print(ok_("\n  ✓ SillyTavern launched in new window."))
    return ok


def do_stop_all():
    for comp in COMPS.values():
        comp.stop()
    print(err_("\n  All managed components stopped."))

# ─── main loop ────────────────────────────────────────────────────────────────

def main():
    if IS_WIN:
        signal.signal(signal.SIGINT,   lambda *_: None)
        signal.signal(signal.SIGBREAK, lambda *_: None)

    state    = {"dry_run": False}
    pause_ms = 0.0   # seconds to pause before redraw (let output settle)

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
                do_quinn(state["dry_run"])
                pause_ms = 1.5
            case "3":
                do_sniper()
                time.sleep(0.3)
                do_quinn(state["dry_run"])
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
                print(accent("\n  Launcher closed.  Components keep running in their windows.\n"))
                sys.exit(0)
            case _:
                pass   # unknown → silent redraw


if __name__ == "__main__":
    main()
