import asyncio
import re
import sys
import unicodedata
from datetime import datetime
from os import get_terminal_size

from colorama import Fore, Style

R = Style.RESET_ALL
C_TEAL = Fore.LIGHTCYAN_EX
C_PURPLE = Fore.LIGHTMAGENTA_EX
C_AMBER = Fore.LIGHTWHITE_EX
C_MINT = Fore.LIGHTGREEN_EX
C_CORAL = Fore.LIGHTRED_EX
C_GRAY = Fore.LIGHTBLACK_EX
C_WHITE = Fore.WHITE
C_GOLD = Fore.LIGHTWHITE_EX
C_BLUE = Fore.LIGHTWHITE_EX
C_ROSE = Fore.RED
C_DIM = Fore.LIGHTBLACK_EX

_KARUTA = [
    r"     dBP dBP dBBBBBb   dBBBBBb    dBP dBP dBBBBBBP dBBBBBb  .dBBBBP   dBBBBb  dBP dBBBBBb  dBBBP ",
    r"    d8P.dBP       BB       dBP                          BB  BP           dBP          dB'        ",
    r"   dBBBBP     dBP BB   dBBBBK   dBP dBP    dBP      dBP BB  `BBBBb  dBP dBP dBP   dBBBP' dBBP    ",
    r"  dBP BB     dBP  BB  dBP  BB  dBP_dBP    dBP      dBP  BB     dBP dBP dBP dBP   dBP    dBP      ",
    r" dBP dBP    dBBBBBBB dBP  dB' dBBBBBP    dBP      dBBBBBBBdBBBBP' dBP dBP dBP   dBP    dBBBBP    ",
]

_SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
_SCAN_SPIN = ['◐', '◓', '◑', '◒']
_DROP_SPIN = ['▖', '▘', '▝', '▗']
_IDLE_DOTS = ['·', '·', '·', '○']

_ANSI = re.compile(r'\x1b\[[0-9;]*[mK]')


def _char_width(ch: str) -> int:
    """Return the terminal display width of a single character (1 or 2)."""
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ('W', 'F') else 1


def _vlen(s: str) -> int:
    """Visible display width of a string, stripping ANSI and counting wide chars."""
    clean = _ANSI.sub('', s)
    return sum(_char_width(c) for c in clean)


def _term_w() -> int:
    try:
        return get_terminal_size().columns
    except Exception:
        return 80


def _truncate_visual(text: str, max_width: int) -> str:
    """Truncate plain text to at most max_width display columns."""
    width = 0
    for i, ch in enumerate(text):
        w = _char_width(ch)
        if width + w > max_width:
            return text[:i]
        width += w
    return text

class KarutaConsole:
    def __init__(self, use_timestamp: bool = True):
        self.use_timestamp = use_timestamp
        self.grab_timer: int = 0
        self.drop_timer: int = 0
        self.collected:  int = 0
        self.missed:     int = 0
        self.state:      str = "IDLE"
        self._tick:         int = 0
        self._status_drawn: bool = False
        self._running:      bool = False
        self._active:       bool = False
        self._own_write:    bool = False


    def start(self):
        self._active = True
        self._draw_status()


    def _ts(self) -> str:
        if self.use_timestamp:
            t = datetime.now().strftime('%H:%M:%S')
            return f"{C_GRAY}{t}{R}  "
        return ""

    def _cd_bar(self, secs: int, cap: int, width: int = 10) -> str:
        if secs <= 0:
            return f"{C_MINT}READY     {R}"
        pct = 1.0 - min(secs / cap, 1.0)
        filled = round(pct * width)
        bar = C_TEAL + "█" * filled + C_GRAY + "░" * (width - filled) + R
        m, s   = divmod(secs, 60)
        label = f"{C_DIM}{m}m{s:02d}s{R}" if m else f"{C_DIM}{s:3d}s {R}"
        return f"{bar} {label}"

    def _build_status(self) -> str:
        t = self._tick
        sep = f" {C_GRAY}│{R} "

        if self.state == "SCANNING":
            sp = _SCAN_SPIN[t % len(_SCAN_SPIN)]
            stat = f"{C_TEAL}{sp} SCANNING{R}"
        elif self.state == "WAITING":
            sp = _SPINNER[t % len(_SPINNER)]
            stat = f"{C_GRAY}{sp} COOLDOWN{R}"
        elif self.state == "DROPPING":
            sp = _DROP_SPIN[t % len(_DROP_SPIN)]
            stat = f"{C_PURPLE}{sp} DROPPING{R}"
        else:
            dp  = _IDLE_DOTS[t % len(_IDLE_DOTS)]
            stat = f"{C_GRAY}{dp} IDLE    {R}"

        line = (
            f"{C_GRAY}[{R}{stat}{C_GRAY}]{R}"
            f"{sep}{C_GRAY}Grab{R} {self._cd_bar(self.grab_timer, 600)}"
            f"{sep}{C_GRAY}Drop{R} {self._cd_bar(self.drop_timer, 1800)}"
            f"{sep}{C_MINT}✦{self.collected:>4}{R}"
            f"  {C_CORAL}✗{self.missed:>4}{R}"
        )

        w = _term_w()
        vlen = _vlen(line)
        if vlen > w - 2:
            plain = _ANSI.sub('', line)
            line = _truncate_visual(plain, w - 5) + "..." + R

        return line


    def _erase_status(self):
        if self._status_drawn:
            self._own_write = True
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
            self._own_write = False
            self._status_drawn = False

    def _draw_status(self):
        if not self._active:
            return
        self._own_write = True
        sys.stdout.write('\r\x1b[2K' + self._build_status())
        sys.stdout.flush()
        self._own_write = False
        self._status_drawn = True


    def _emit(self, icon: str, msg: str):
        self._erase_status()
        print(f"{self._ts()}{icon}  {msg}{R}")
        self._draw_status()

    def log(self, msg: str):
        self._erase_status()
        print(f"{self._ts()}{msg}{R}")
        self._draw_status()

    def log_raw(self, msg: str):
        self._erase_status()
        print(msg)
        self._draw_status()

    def log_grab(self, msg: str):
        self._emit(f"{C_TEAL}[GRAB]{R}", msg)

    def log_collect(self, msg: str):
        self._emit(f"{C_MINT}[GOT ]{R}", msg)

    def log_drop(self, msg: str):
        self._emit(f"{C_PURPLE}[DROP]{R}", msg)

    def log_wl(self, msg: str):
        self._emit(f"{C_GOLD}[WL  ]{R}", msg)

    def log_kcd(self, msg: str):
        self._emit(f"{C_BLUE}[KCD ]{R}", msg)

    def log_debug(self, msg: str):
        self._emit(f"{C_ROSE}[DBG ]{R}", msg)

    def log_info(self, msg: str):
        self._emit(f"{C_BLUE}[INFO]{R}", msg)

    def log_farm(self, msg: str):
        self._emit(f"{C_PURPLE}[FARM]{R}", msg)



    def print_banner(self):
        w = _term_w()
        self.log_raw("")
        for line in _KARUTA:
            self.log_raw(C_TEAL + line.center(w) + R)
        self.log_raw("")

    def print_startup_box(self, sections: list):
        inner = min(_term_w() - 6, 68)

        def _rule(lc, rc, fill="─"):
            return f"  {C_GRAY}{lc}{fill * inner}{rc}{R}"

        def _row(content):
            pad = max(inner - _vlen(content) - 2, 0)
            return f"  {C_GRAY}│{R} {content}{' ' * pad} {C_GRAY}│{R}"

        self.log_raw(_rule("╭", "╮"))
        first = True
        for header, items in sections:
            if not first:
                self.log_raw(_rule("├", "┤"))
            first = False
            self.log_raw(_row(f"{C_TEAL}{header}{R}"))
            for item in items:
                self.log_raw(_row(f"  {C_GRAY}▸{R}  {item}"))
        self.log_raw(_rule("╰", "╯"))
        self.log_raw("")


    def set_state(self, state: str):
        self.state = state

    async def animate(self):
        self._running = True
        while self._running:
            await asyncio.sleep(0.12)
            self._tick += 1
            if self._status_drawn:
                self._draw_status()

    def stop(self):
        self._running = False
        self._erase_status()