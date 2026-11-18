import asyncio
import re
import sys
import unicodedata
from datetime import datetime
from shutil import get_terminal_size

from colorama import Fore, Style

R = Style.RESET_ALL
C_TEAL = C_MINT = C_WHITE = C_GOLD = Fore.LIGHTWHITE_EX
C_PURPLE = C_AMBER = C_BLUE = Fore.WHITE
C_CORAL = C_ROSE = Style.BRIGHT + Fore.WHITE
C_GRAY = C_DIM = Fore.LIGHTBLACK_EX
_ANSI = re.compile(r'\x1b\[[0-9;]*[mK]')


def _char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def _vlen(text):
    return sum(_char_width(char) for char in _ANSI.sub('', text))


def _term_w():
    return get_terminal_size((80, 24)).columns


def _truncate_visual(text, max_width):
    width = 0
    for index, char in enumerate(text):
        width += _char_width(char)
        if width > max_width:
            return text[:index]
    return text


class KarutaConsole:
    def __init__(self, use_timestamp=True):
        self.use_timestamp = use_timestamp
        self.grab_timer = 0
        self.drop_timer = 0
        self.collected = 0
        self.missed = 0
        self.state = 'IDLE'
        self._status_drawn = False
        self._running = False
        self._active = False
        self._own_write = False

    def start(self):
        self._active = sys.stdout.isatty()
        self._draw_status()

    def _ts(self):
        return f'{C_DIM}{datetime.now():%H:%M:%S}{R}  ' if self.use_timestamp else ''

    def _cd_bar(self, secs, cap, width=8):
        if secs <= 0:
            return f'{C_WHITE}ready{R}'
        filled = round((1 - min(secs / cap, 1)) * width)
        minutes, seconds = divmod(secs, 60)
        return f'{C_WHITE}{"━" * filled}{C_DIM}{"─" * (width - filled)} {minutes:02d}:{seconds:02d}{R}'

    def _build_status(self):
        labels = {'SCANNING': 'READING', 'WAITING': 'COOLDOWN', 'DROPPING': 'DROPPING', 'IDLE': 'LISTENING'}
        label = labels.get(self.state, self.state)
        line = (
            f'{C_WHITE}{label}{R}  {C_DIM}│{R}  '
            f'Grab {self._cd_bar(self.grab_timer, 600)}  '
            f'Drop {self._cd_bar(self.drop_timer, 1800)}  {C_DIM}│{R}  '
            f'{C_WHITE}{self.collected}{R} collected  {C_DIM}{max(self.missed, 0)} unconfirmed{R}'
        )
        width = max(1, _term_w() - 2)
        if _vlen(line) > width:
            line = _truncate_visual(_ANSI.sub('', line), width)
        return line

    def _erase_status(self):
        if self._status_drawn:
            self._own_write = True
            try:
                sys.stdout.write('\r\x1b[2K')
                sys.stdout.flush()
            finally:
                self._own_write = False
                self._status_drawn = False

    def _draw_status(self):
        if not self._active:
            return
        self._own_write = True
        try:
            sys.stdout.write('\r\x1b[2K' + self._build_status())
            sys.stdout.flush()
            self._status_drawn = True
        finally:
            self._own_write = False

    def _write(self, text):
        self._erase_status()
        print(text if sys.stdout.isatty() else _ANSI.sub('', text))
        self._draw_status()

    def _emit(self, label, message):
        self._write(f'{self._ts()}{C_WHITE}{label:<8}{R} {message}{R}')

    def log(self, message):
        self._write(f'{self._ts()}{message}{R}')

    def log_raw(self, message):
        self._write(message)

    def log_grab(self, message):
        self._emit('MATCH', message)

    def log_collect(self, message):
        self._emit('CLAIMED', message)

    def log_drop(self, message):
        self._emit('DROP', message)

    def log_wl(self, message):
        self._emit('WISHLIST', message)

    def log_kcd(self, message):
        self._emit('TIMERS', message)

    def log_debug(self, message):
        self._emit('DEBUG', message)

    def log_info(self, message):
        self._emit('INFO', message)

    def log_farm(self, message):
        self._emit('FARM', message)

    def print_banner(self):
        width = max(1, min(_term_w() - 4, 72))
        self.log_raw('')
        self.log_raw(f'  {C_WHITE}S N I P E R{R}')
        self.log_raw(f'  {C_DIM}Card recognition / matching / collection{R}')
        self.log_raw(f'  {C_DIM}{"─" * width}{R}')

    def print_startup_box(self, sections):
        width = max(1, min(_term_w() - 4, 72))
        for heading, items in sections:
            self.log_raw(f'\n  {C_WHITE}{_truncate_visual(_ANSI.sub("", heading), width)}{R}')
            for item in items:
                self.log_raw(f'  {C_DIM}{_truncate_visual(_ANSI.sub("", item), width)}{R}')
        self.log_raw('')

    def set_state(self, state):
        self.state = state

    async def animate(self):
        if not self._active:
            return
        self._running = True
        while self._running:
            await asyncio.sleep(0.25)
            self._draw_status()

    def stop(self):
        self._running = False
        self._active = False
        self._erase_status()
