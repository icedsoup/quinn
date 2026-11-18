import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from time import perf_counter

import discord
from colorama import init

from lib import api
from lib.imageapi import ocr_image, ocr_print_number
from lib.ocr import card_count, get_bottom, get_card, get_print, get_top
from console import KarutaConsole, R, C_TEAL, C_PURPLE, C_AMBER, C_MINT, C_CORAL, C_GRAY, C_DIM, C_WHITE, C_GOLD
from telemetry import emit

init(convert=True)
match = "(is dropping [3-4] cards!)|(I'm dropping [3-4] cards since this server is currently active!)"
ROOT = Path(__file__).resolve().parent
OCR_DIR = ROOT / "temp"
KEYWORDS_DIR = ROOT / "keywords"
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "log.txt"
v = "v2.3.3"

KARUTA_ID = 646937666251915264
CARDCOMPANION_ID = 1380936713639166082
GRAB_ATTEMPT_COOLDOWN = 60
BUTTON_WINDOW = 25.0
CARD_REACTIONS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")

try:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
except FileNotFoundError:
    raise SystemExit('Copy "config template.json" to "config.json" and fill in the settings.') from None
except (OSError, ValueError) as exc:
    raise SystemExit(f"Could not read config.json: {exc}") from None

title = os.name == "nt"

token = config["token"]
channels = config["channels"]
guilds = config["servers"]
accuracy = float(config["accuracy"])
blaccuracy = float(config["blaccuracy"])
loghits = config["log_hits"]
logcollection = config["log_collection"]
timestamp = config["timestamp"]
autodrop = config["autodrop"]
debug = config["debug"]
cprint = config["check_print"]
autofarm = config["autofarm"]
verbose = config["very_verbose"]

resourcechannel = config.get("resourcechannel")
min_wishlist = int(config.get("min_wishlist", 50))
wishlist_lookup_enabled = config.get("wishlist_lookup", True)
wishlist_watching_channels = config.get("wishlist_watching_channels", channels)
clear_console_on_start = config.get("clear_console_on_start", True)
lookup_delay = float(config.get("lookup_delay", 1.5))

log_drops = config.get("log_drops", True)
log_grabs = config.get("log_grabs", True)
log_wishlist = config.get("log_wishlist", True)
log_autodrop = config.get("log_autodrop", True)
log_kcd = config.get("log_kcd", True)
log_autofarm = config.get("log_autofarm", True)

if cprint:
    pn = int(config["print_number"])
autodropchannel = config.get("autodropchannel")
if autodrop:
    dropdelay = config["dropdelay"]
    randmin = int(config["randmin"])
    randmax = int(config["randmax"])


class _PatchedWriter:
    """Thin shim that erases the status bar before any unexpected write."""

    __slots__ = ('_o', '_c')

    def __init__(self, orig, con):
        self._o = orig
        self._c = con

    def write(self, s):
        # Skip the erase when the console itself is writing (prevents recursion)
        if not self._c._own_write:
            self._c._erase_status()
        return self._o.write(s)

    def flush(self):
        return self._o.flush()

    def isatty(self):
        try:
            return self._o.isatty()
        except Exception:
            return False

    def fileno(self):
        try:
            return self._o.fileno()
        except Exception:
            raise io.UnsupportedOperation("fileno")

    def __getattr__(self, name):
        return getattr(self._o, name)

console = KarutaConsole(use_timestamp=timestamp)

_orig_stdout = sys.stdout
_orig_stderr = sys.stderr
sys.stdout = _PatchedWriter(_orig_stdout, console)
sys.stderr = _PatchedWriter(_orig_stderr, console)


class _ConsoleLogHandler(logging.Handler):
    def emit(self, record):
        try:
            console.log(self.format(record))
        except Exception:
            pass

_discord_log_handler = _ConsoleLogHandler()
_discord_log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

_discord_logger = logging.getLogger("discord")
_discord_logger.handlers.clear()
_discord_logger.addHandler(_discord_log_handler)
_discord_logger.setLevel(logging.WARNING)
_discord_logger.propagate = False

_CONSONANTS = frozenset('bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ')


def _parse_char_line(line: str):
    line = line.strip()
    if not line:
        return None
    if ',' in line:
        parts = line.split(',', 1)
        return (parts[0].strip(), parts[1].strip())
    return (line, None)


class ChannelConfigurationError(Exception):
    pass


class Main(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.charblacklist = None
        self.aniblacklist = None
        self.animes = None
        self.chars: list[tuple[str, str | None]] = []
        self.ready = False
        self.grab_timer = 0
        self.drop_timer = 0
        self.url = None
        self.grab_reason = ""
        self.missed = 0
        self.collected = 0
        self.lookup_next_at = 0.0
        self.lookup_lock = asyncio.Lock()
        self.drop_lock = asyncio.Lock()
        self.pending_claims = {}

    async def on_ready(self):
        emit('sniper', 'connection', 'complete', 'Connected to Discord', channels=len(channels))
        for guild_id in guilds:
            guild = self.get_guild(guild_id)
            if guild is None:
                tprint(f"{C_CORAL}Server {guild_id} is unavailable{R}")
                continue
            await guild.subscribe(typing=True, activities=False, threads=False, member_updates=False)

        if self.ready:
            return
        if clear_console_on_start and sys.stdout.isatty():
            await asyncio.create_subprocess_shell("cls" if title else "clear")
        await asyncio.sleep(0.5)
        if sys.gettrace() is None:
            console.print_banner()

        _sections = []
        _user_str = (
            f"{C_MINT}{self.user.name}#{self.user.discriminator}{R}"
            f"  {C_DIM}({self.user.id}){R}"
        )
        _sections.append(("ONLINE", [_user_str]))

        _watch_items = []
        for _cid in channels:
            _ch = self.get_channel(_cid)
            _cn = f"#{_ch.name}" if _ch else f"(unknown: {_cid})"
            _gn = _ch.guild.name if _ch else "?"
            _watch_items.append(
                f"{C_WHITE}{_gn}{R}  {C_DIM}▸{R}  {C_MINT}{_cn}{R}"
            )
        _sections.append(
            (f"WATCHING  {C_GRAY}({len(_watch_items)} channels){R}", _watch_items)
        )

        if wishlist_watching_channels != channels:
            _wl_items = []
            for _cid in wishlist_watching_channels:
                _ch = self.get_channel(_cid)
                _cn = f"#{_ch.name}" if _ch else f"(unknown: {_cid})"
                _gn = _ch.guild.name if _ch else "?"
                _wl_items.append(
                    f"{C_WHITE}{_gn}{R}  {C_DIM}▸{R}  {C_MINT}{_cn}{R}"
                )
            _sections.append((f"WISHLIST  {C_GRAY}({len(_wl_items)} channels){R}", _wl_items))

        if autodrop:
            _ch = self.get_channel(autodropchannel)
            _cn = f"#{_ch.name}" if _ch else f"(unknown: {autodropchannel})"
            _gn = _ch.guild.name if _ch else "?"
            _sections.append(
                ("AUTODROP", [f"{C_WHITE}{_gn}{R}  {C_DIM}▸{R}  {C_PURPLE}{_cn}{R}"])
            )

        console.print_startup_box(_sections)

        console.start()

        dprint(f"discord.py-self {discord.__version__}")

        await self.update_files()

        def spawn(coro, name="task"):
            t = asyncio.get_event_loop().create_task(coro)
            def _done(fut, _name=name):
                exc = fut.exception() if not fut.cancelled() else None
                if exc:
                    tprint(f"{C_CORAL}[TASK ERROR]{R} {_name} crashed: {exc!r}")
            t.add_done_callback(_done)
            return t

        spawn(self.cooldown(), "cooldown")
        spawn(self.filewatch(KEYWORDS_DIR / "animes.txt"), "filewatch:animes")
        spawn(self.filewatch(KEYWORDS_DIR / "characters.txt"), "filewatch:characters")
        spawn(self.filewatch(KEYWORDS_DIR / "aniblacklist.txt"), "filewatch:aniblacklist")
        spawn(self.configwatch(CONFIG_PATH), "configwatch")
        spawn(self.filewatch(KEYWORDS_DIR / "charblacklist.txt"),"filewatch:charblacklist")
        spawn(console.animate(), "console:animate")
        if autodrop:
            spawn(self.autodrop(), "autodrop")
        if autofarm:
            spawn(self.autofarm(), "autofarm")
        self.ready = True

    async def on_disconnect(self):
        emit('sniper', 'connection', 'stopped', 'Discord connection interrupted')

    async def on_message(self, message):
        cid = message.channel.id

        if (
            not self.ready
            or message.author.id != KARUTA_ID
            or cid not in channels
        ):
            return

        buttons_ref = [None]

        def mcheck(before, after):
            if before.id != message.id:
                return False
            try:
                children = after.components[0].children
                if not children:
                    return False
                if children[0].disabled:
                    return False
                buttons_ref[0] = children
                dprint("Message edit found")
                return True
            except (IndexError, AttributeError):
                dprint(f"mcheck component error - {after.components}")
                return False

        if re.search("A wishlisted card is dropping!", message.content):
            dprint("Wishlisted card detected")

        is_drop = bool(message.attachments and re.search(match, message.content))
        trace = str(message.id)
        if is_drop and self.grab_timer > 0:
            emit('sniper', 'intake', 'skipped', 'Drop skipped during grab cooldown', trace=trace, grab_seconds=self.grab_timer)
        if self.grab_timer == 0 and is_drop:
            emit('sniper', 'intake', 'complete', f'Drop received in #{message.channel.name}', trace=trace, channel=message.channel.name)
            if isbutton(cid):
                edit_task = asyncio.get_running_loop().create_task(
                    self.wait_for("message_edit", check=mcheck, timeout=BUTTON_WINDOW)
                )
            else:
                edit_task = None

            console.set_state("SCANNING")

            try:
                async with self.drop_lock:
                    if self.grab_timer != 0:
                        emit('sniper', 'intake', 'skipped', 'Another claim started while waiting to scan', trace=trace, grab_seconds=self.grab_timer)
                        return

                    try:
                        charlist, anilist, printlist = await self.scan_drop(message.attachments[0], trace)
                    except (discord.HTTPException, OSError, ValueError) as exc:
                        dprint(f"Skipping unreadable drop: {exc}")
                        return

                    vprint(f"Anilist: {anilist}")
                    vprint(f"Charlist: {charlist}")

                    for i, number in enumerate(printlist):
                        try:
                            printlist[i] = int(re.sub(r" \d$| ", "", number))
                        except ValueError:
                            dprint(f"ValueError parsing print number: '{number}'")
                            printlist[i] = 9999999
                    vprint(f"Printlist: {printlist}")

                    grab_idx = -1
                    grab_reason = ""
                    matching_started = perf_counter()
                    emit('sniper', 'match', 'start', 'Comparing recognized cards with configured rules', trace=trace,
                         characters=len(self.chars), series_rules=len(self.animes), threshold=accuracy, blacklist_threshold=blaccuracy)

                    char_matches = []
                    for i, character in enumerate(charlist):
                        matched = api.isSomethingChar(character, anilist[i], self.chars, accuracy)
                        blocked = matched and (api.isSomething(character, self.charblacklist, accuracy)
                                                or api.isSomething(anilist[i], self.aniblacklist, blaccuracy))
                        emit('sniper', 'match', 'info', f'Card {i + 1} · {character}: ' + ('blocked by blacklist' if blocked else 'character rule matched' if matched else 'no character rule'),
                             trace=trace, card=i + 1, character=character, series=anilist[i], rule='character', matched=bool(matched), blocked=bool(blocked))
                        if matched and not blocked:
                            kw_idx = self.keyword_match_priority_char(character, anilist[i], self.chars, accuracy)
                            char_matches.append((i, kw_idx, character))

                    if char_matches:
                        char_matches.sort(key=lambda x: x[1])
                        grab_idx, _, best_char = char_matches[0]
                        grab_reason = (
                            f"Character: {C_MINT}{best_char}{R}  {C_DIM}from{R}  {C_TEAL}{anilist[grab_idx]}{R}"
                        )
                        self.grab_reason = f"Character: {best_char} from {anilist[grab_idx]}"
                        if loghits:
                            with LOG_PATH.open("a", encoding="utf-8") as ff:
                                ff.write(
                                    f"{current_time() + ' - ' if timestamp else ''}"
                                    f"Character: {best_char} from {anilist[grab_idx]} - {message.attachments[0].url}\n"
                                )

                    if grab_idx == -1:
                        anime_matches = []
                        for i, anime in enumerate(anilist):
                            matched = api.isSomething(anime, self.animes, accuracy)
                            blocked = matched and (api.isSomething(charlist[i], self.charblacklist, accuracy)
                                                    or api.isSomething(anime, self.aniblacklist, blaccuracy))
                            emit('sniper', 'match', 'info', f'Card {i + 1} · {anime}: ' + ('blocked by blacklist' if blocked else 'series rule matched' if matched else 'no series rule'),
                                 trace=trace, card=i + 1, character=charlist[i], series=anime, rule='series', matched=bool(matched), blocked=bool(blocked))
                            if matched and not blocked:
                                kw_idx = self.keyword_match_priority(anime, self.animes, accuracy)
                                anime_matches.append((i, kw_idx, anime))

                        if anime_matches:
                            anime_matches.sort(key=lambda x: x[1])
                            grab_idx, _, best_anime = anime_matches[0]
                            grab_reason = (
                                f"Anime: {C_TEAL}{best_anime}{R}  {C_DIM}│{R}  {C_MINT}{charlist[grab_idx]}{R}"
                            )
                            self.grab_reason = f"Anime: {best_anime} | {charlist[grab_idx]}"
                            if loghits:
                                with LOG_PATH.open("a", encoding="utf-8") as ff:
                                    ff.write(
                                        f"{current_time() + ' - ' if timestamp else ''}"
                                        f"Anime: {best_anime} | {charlist[grab_idx]} - {message.attachments[0].url}\n"
                                    )

                    if grab_idx == -1 and cprint:
                        for i, prin in enumerate(printlist):
                            emit('sniper', 'match', 'info', f'Card {i + 1} · print {prin if prin != 9999999 else "unreadable"}',
                                 trace=trace, card=i + 1, print_number=prin if prin != 9999999 else None, print_threshold=pn,
                                 rule='print', matched=prin <= pn and anilist[i] not in self.aniblacklist and charlist[i] not in self.charblacklist)
                            if (
                                prin <= pn
                                and anilist[i] not in self.aniblacklist
                                and charlist[i] not in self.charblacklist
                            ):
                                grab_idx = i
                                grab_reason = f"Print #{C_GOLD}{prin}{R}"
                                self.grab_reason = f"Print #{prin}: {charlist[i]} from {anilist[i]}"
                                if loghits:
                                    with LOG_PATH.open("a", encoding="utf-8") as ff:
                                        ff.write(
                                            f"{current_time() + ' - ' if timestamp else ''}"
                                            f"Print #{prin}: {charlist[i]} from {anilist[i]} - "
                                            f"{re.sub(chr(63) + '.*', '', message.attachments[0].url)}\n"
                                        )
                                break

                    emit('sniper', 'match', 'complete', self.grab_reason if grab_idx != -1 else 'No character, series, or print rule matched', trace=trace,
                         duration_ms=round((perf_counter() - matching_started) * 1000, 1), matched=grab_idx != -1,
                         card=grab_idx + 1 if grab_idx != -1 else None, character=charlist[grab_idx] if grab_idx != -1 else '',
                         series=anilist[grab_idx] if grab_idx != -1 else '')
                    if grab_idx != -1:
                        drop_print(f"{C_TEAL}[{message.channel.name}]{R}  {grab_reason}")
                        self.url = message.attachments[0].url

                        if edit_task:
                            await self.grab_with_button(grab_idx, edit_task, buttons_ref, drop_print, trace, cid)
                        else:
                            await self.react_add(message, CARD_REACTIONS[grab_idx], trace)

                console.set_state("IDLE")

                if grab_idx == -1 and wishlist_lookup_enabled and cid in wishlist_watching_channels:
                    await self.do_wishlist_lookup(message, charlist, anilist, edit_task, buttons_ref)
                elif grab_idx == -1:
                    emit('sniper', 'wishlist', 'skipped', 'Wishlist lookup is disabled for this channel', trace=trace)
            finally:
                if edit_task:
                    edit_task.cancel()
                    await asyncio.gather(edit_task, return_exceptions=True)
                console.set_state("IDLE")

        elif re.search(
            rf"<@{str(self.user.id)}> took the \*\*.*\*\* card `.*`!"
            rf"|<@{str(self.user.id)}> fought off .* and took the \*\*.*\*\* card `.*`!",
            message.content
        ):
            a = re.search(
                rf"<@{str(self.user.id)}>.*took the \*\*(.*)\*\* card `(.*)`!",
                message.content
            )
            self.grab_timer = 600
            self.missed    -= 1
            self.collected += 1
            emit('sniper', 'claim', 'complete', f'Collection confirmed: {a.group(1)}', trace=self.pending_claims.pop(cid, trace),
                 character=a.group(1), collected=self.collected, unconfirmed=max(self.missed, 0), grab_seconds=self.grab_timer)
            console.collected = self.collected
            console.missed = self.missed
            grab_print(f"{C_TEAL}[{message.channel.name}]{R}  Obtained  {C_MINT}{a.group(1)}{R}")
            if logcollection:
                with LOG_PATH.open("a", encoding="utf-8") as ff:
                    reason_str = f" [{self.grab_reason}]" if self.grab_reason else ""
                    if timestamp:
                        ff.write(f"{current_time()} - Obtained: {a.group(1)}{reason_str} - {self.url}\n")
                    else:
                        ff.write(f"Obtained: {a.group(1)}{reason_str} - {self.url}\n")

        elif message.content.startswith(f"<@{str(self.user.id)}>, your **Evasion"):
            dprint("Evasion blessing detected — resetting grab cd")
            self.grab_timer = 0
        elif message.content.startswith(f"<@{str(self.user.id)}>, your **Generosity"):
            dprint("Generosity blessing detected — resetting drop cd")
            self.drop_timer = 0

    async def scan_drop(self, attachment, trace=None):
        crops = OCR_DIR / "char"
        crops.mkdir(parents=True, exist_ok=True)
        image = OCR_DIR / "card.webp"
        started = perf_counter()
        emit('sniper', 'download', 'start', 'Downloading drop image', trace=trace)
        try:
            await attachment.save(image)
            count = card_count(image)
        except Exception as exc:
            emit('sniper', 'download', 'error', str(exc), trace=trace)
            raise
        emit('sniper', 'download', 'complete', f'Drop image ready · {count} cards', trace=trace, cards=count,
             duration_ms=round((perf_counter() - started) * 1000, 1), image_bytes=image.stat().st_size)
        started = perf_counter()
        emit('sniper', 'ocr', 'start', 'Reading character names, series, and print numbers', trace=trace, cards=count, engine='EasyOCR', device='CPU')
        try:
            chars, animes, prints = [], [], []
            for index in range(count):
                card = OCR_DIR / f"card{index + 1}.png"
                top = crops / f"top{index + 1}.png"
                bottom = crops / f"bottom{index + 1}.png"
                await get_card(card, image, index)
                await get_top(card, top)
                await get_bottom(card, bottom)
                chars.append(self.normalize_ocr_text(await self.ocr_best(str(top), trace, index + 1, 'character')))
                animes.append(self.normalize_ocr_text(await self.ocr_best(str(bottom), trace, index + 1, 'series')))
                if cprint:
                    strip = crops / f"print{index + 1}.png"
                    await get_print(card, strip)
                    prints.append(await asyncio.to_thread(ocr_print_number, str(strip), trace, index + 1))
                else:
                    prints.append("9999999")
        except Exception as exc:
            emit('sniper', 'ocr', 'error', str(exc), trace=trace)
            raise
        emit('sniper', 'ocr', 'complete', f'Read {count} cards', trace=trace, cards=count,
             duration_ms=round((perf_counter() - started) * 1000, 1), passes=count * (3 if cprint else 2))
        return chars, animes, prints


    async def do_wishlist_lookup(self, message, charlist, anilist, edit_task, buttons_ref):
        trace = str(message.id)
        started = perf_counter()
        emit('sniper', 'wishlist', 'start', 'Checking wishlist counts', trace=trace, wishlist_threshold=min_wishlist)
        try:
            await self._wishlist_lookup(message, charlist, anilist, edit_task, buttons_ref)
        except Exception as exc:
            emit('sniper', 'wishlist', 'error', str(exc), trace=trace)
            raise
        else:
            emit('sniper', 'wishlist', 'complete', 'Wishlist checks finished', trace=trace, duration_ms=round((perf_counter() - started) * 1000, 1))

    async def _wishlist_lookup(self, message, charlist, anilist, edit_task, buttons_ref):
        trace = str(message.id)
        if self.grab_timer != 0:
            dprint(f"Grab on cooldown ({self.grab_timer}s) — skipping wishlist lookup")
            emit('sniper', 'wishlist', 'info', 'Grab cooldown prevents lookup', trace=trace, grab_seconds=self.grab_timer)
            return

        channel = self.get_channel(autodropchannel) if autodropchannel else None
        channel = channel or message.channel
        best_idx = -1
        best_wl = -1

        wl_print(f"{C_TEAL}[{channel.name}]{R} No keyword match — checking wishlists")

        for i in range(len(charlist)):

            query_name = self.normalize_for_query(fix_ocr_spaces(charlist[i].strip()))
            query_series = self.normalize_for_query(fix_ocr_spaces(anilist[i].strip()))

            name = self.normalize_ocr_text(fix_ocr_spaces(charlist[i].strip()))
            series = self.normalize_ocr_text(fix_ocr_spaces(anilist[i].strip()))

            if not query_name:
                emit('sniper', 'wishlist', 'info', f'Card {i + 1} has no readable name', trace=trace, card=i + 1)
                continue

            if api.isSomething(name, self.charblacklist, blaccuracy):
                dprint(f"WL: skipping blacklisted character: {name}")
                emit('sniper', 'wishlist', 'info', f'Card {i + 1} excluded by character blacklist', trace=trace, card=i + 1, blocked=True)
                continue
            if series and api.isSomething(series, self.aniblacklist, blaccuracy):
                dprint(f"WL: skipping blacklisted anime: {series}")
                emit('sniper', 'wishlist', 'info', f'Card {i + 1} excluded by series blacklist', trace=trace, card=i + 1, blocked=True)
                continue

            query = f"clu {query_series} {query_name}" if query_series else f"clu {query_name}"

            async with self.lookup_lock:
                now = asyncio.get_running_loop().time()
                wait_time = self.lookup_next_at - now
                if wait_time > 0:
                    wl_print(f"{C_AMBER}Rate limit — waiting {wait_time:.1f}s{R}")
                    emit('sniper', 'wishlist', 'info', 'Waiting for lookup rate limit', trace=trace, wait_seconds=round(wait_time, 2))
                    await asyncio.sleep(wait_time)

                if self.grab_timer != 0:
                    dprint("Grab on cooldown — aborting wishlist lookup")
                    return

                async with channel.typing():
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                response = asyncio.create_task(self.wait_for(
                        "message",
                        timeout=6.0,
                        check=lambda m: (
                            m.author.id == CARDCOMPANION_ID
                            and m.channel.id == channel.id
                            and (len(m.embeds) > 0 or bool(m.content))
                        ),
                ))
                try:
                    emit('sniper', 'wishlist', 'info', f'Looking up {query_name}', trace=trace, card=i + 1, character=query_name, series=query_series)
                    await channel.send(query)
                    self.lookup_next_at = asyncio.get_running_loop().time() + lookup_delay
                    resp = await response
                except asyncio.TimeoutError:
                    wl_print(f"{C_AMBER}Timeout waiting for clu response for:{R} {query_name}")
                    emit('sniper', 'wishlist', 'info', f'Lookup timed out for {query_name}', trace=trace, card=i + 1, timed_out=True)
                    continue
                finally:
                    if not response.done():
                        response.cancel()
                        await asyncio.gather(response, return_exceptions=True)

                if not resp.embeds:
                    dprint(f"WL: clu no match for '{query_name}' — skipping")
                    continue

            wl = -1
            try:
                embed = resp.embeds[0]

                vprint(
                    f"clu embed dump — "
                    f"title={embed.title!r}  "
                    f"desc={embed.description!r}  "
                    f"fields={[(f.name, f.value) for f in (embed.fields or [])]}  "
                    f"content={resp.content!r}"
                )

                if embed.description:
                    desc_clean = embed.description.replace("**", "")
                    m = re.search(r"wishlist\s*[:\-]\s*([\d,]+)", desc_clean, re.IGNORECASE)
                    if m:
                        wl = int(re.sub(r"[^0-9]", "", m.group(1)))

                if wl == -1:
                    for field in (embed.fields or []):
                        if field.name.strip().lower() == "wishlist":
                            wl_text = re.sub(r"[^0-9]", "", field.value)
                            if wl_text:
                                wl = int(wl_text)
                            break

                if wl == -1 and resp.content:
                    m = re.search(r"wishlist\s*[:\-]\s*([\d,]+)", resp.content, re.IGNORECASE)
                    if m:
                        wl = int(re.sub(r"[^0-9]", "", m.group(1)))

            except Exception as e:
                dprint(f"WL parse error for {query_name}: {e}")

            vprint(
                f"WL: {query_name} ({query_series})  →  "
                f"{wl if wl >= 0 else '?'} wishlists"
            )
            emit('sniper', 'wishlist', 'info', f'{query_name}: {wl if wl >= 0 else "unknown"} wishlists', trace=trace,
                 card=i + 1, character=query_name, wishlist_count=wl if wl >= 0 else None, wishlist_threshold=min_wishlist)

            if wl > best_wl:
                best_wl = wl
                best_idx = i

            if i < len(charlist) - 1:
                await asyncio.sleep(random.uniform(0.3, 0.8))

        if best_idx == -1 or best_wl < min_wishlist:
            emit('sniper', 'wishlist', 'info', 'No card meets the wishlist threshold', trace=trace,
                 wishlist_count=best_wl if best_wl >= 0 else None, wishlist_threshold=min_wishlist, matched=False)
            wl_print(
                f"Best wishlist: {C_CORAL}{best_wl if best_wl >= 0 else '?'}{R} — "
                f"below threshold ({C_AMBER}{min_wishlist}{R}), skipping"
            )
            return

        if self.grab_timer != 0:
            dprint("Grab on cooldown — skipping wishlist grab")
            return

        wl_print(f"Grabbing {C_MINT}{charlist[best_idx]}{R} with {C_GOLD}{best_wl}{R} wishlists")
        self.url = message.attachments[0].url
        self.grab_reason = f"Wishlist ({best_wl}): {charlist[best_idx]} from {anilist[best_idx]}"
        emit('sniper', 'wishlist', 'info', self.grab_reason, trace=trace,
             card=best_idx + 1, character=charlist[best_idx], wishlist_count=best_wl, matched=True)

        if loghits:
            with LOG_PATH.open("a", encoding="utf-8") as ff:
                ff.write(
                    f"{current_time() + ' - ' if timestamp else ''}"
                    f"Wishlist ({best_wl}): {charlist[best_idx]} from {anilist[best_idx]} - "
                    f"{message.attachments[0].url}\n"
                )

        if edit_task:
            await self.grab_with_button(best_idx, edit_task, buttons_ref, wl_print, trace, message.channel.id)
        else:
            await self.react_add(message, CARD_REACTIONS[best_idx], trace)

    async def grab_with_button(self, index, edit_task, buttons_ref, report, trace=None, channel_id=None):
        emit('sniper', 'claim', 'start', f'Waiting to claim card {index + 1}', trace=trace, card=index + 1, method='button')
        try:
            await edit_task
            buttons = buttons_ref[0]
            if not buttons or index >= len(buttons):
                report(f"{C_CORAL}Button {index + 1} is unavailable{R}")
                emit('sniper', 'claim', 'error', 'Card button unavailable', trace=trace, card=index + 1)
                return
            await asyncio.sleep(random.uniform(0.1, 0.35))
            await buttons[index].click()
        except asyncio.TimeoutError:
            report(f"{C_CORAL}Timed out waiting for the card buttons{R}")
            emit('sniper', 'claim', 'error', 'Timed out waiting for card buttons', trace=trace)
        except discord.errors.InvalidData:
            report(f"{C_CORAL}Discord did not acknowledge the card grab{R}")
            emit('sniper', 'claim', 'error', 'Discord did not acknowledge the card grab', trace=trace)
        except Exception as exc:
            report(f"{C_CORAL}Button grab failed: {exc}{R}")
            emit('sniper', 'claim', 'error', str(exc), trace=trace)
        else:
            await self.afterclick()
            self.pending_claims[channel_id] = trace
            emit('sniper', 'claim', 'info', 'Claim submitted; waiting for collection confirmation', trace=trace,
                 card=index + 1, grab_seconds=self.grab_timer, unconfirmed=max(self.missed, 0))

    async def react_add(self, message, emoji, trace=None):
        trace = trace or str(message.id)
        emit('sniper', 'claim', 'start', 'Submitting card reaction', trace=trace, method='reaction', card=CARD_REACTIONS.index(emoji) + 1)
        try:
            dprint("Attempting to react")
            await asyncio.sleep(random.uniform(0.1, 0.35))
            await message.add_reaction(emoji)
        except discord.errors.Forbidden as exc:
            dprint(f"React failed: {exc}")
            emit('sniper', 'claim', 'error', str(exc), trace=trace)
            return
        except Exception as exc:
            emit('sniper', 'claim', 'error', str(exc), trace=trace)
            raise
        self.grab_timer = GRAB_ATTEMPT_COOLDOWN
        console.grab_timer = self.grab_timer
        self.missed       += 1
        console.missed = self.missed
        self.pending_claims[message.channel.id] = trace
        emit('sniper', 'claim', 'info', 'Reaction sent; waiting for collection confirmation', trace=trace,
             grab_seconds=self.grab_timer, unconfirmed=max(self.missed, 0))
        dprint(f"Reacted with {emoji} — grab cd set to {GRAB_ATTEMPT_COOLDOWN}s")


    @staticmethod
    def keyword_match_priority(text, keyword_list, threshold):
        text_l = text.lower().strip()
        for idx, kw in enumerate(keyword_list):
            kw_l = kw.lower().strip()
            if kw_l and SequenceMatcher(None, text_l, kw_l).ratio() >= threshold:
                return idx
        return len(keyword_list)

    @staticmethod
    def keyword_match_priority_char(char_text: str, anime_text: str,
                                    parsed_chars: list, threshold: float) -> int:
        char_l = char_text.lower().strip()
        anime_l = anime_text.lower().strip()
        for idx, (char_name, char_anime) in enumerate(parsed_chars):
            if not char_name:
                continue
            if SequenceMatcher(None, char_l, char_name.lower()).ratio() >= threshold:
                if char_anime is None:
                    return idx
                if SequenceMatcher(None, anime_l, char_anime.lower()).ratio() >= threshold:
                    return idx
        return len(parsed_chars)

    @staticmethod
    def _fix_vu_confusion(text: str) -> str:
        """
        EasyOCR sometimes reads 'v' as 'u'. Convert 'u' → 'v' only when it is
        flanked by consonants on both sides (strong indicator it is actually a 'v').
        Used for keyword matching only — NOT for clu query strings.
        """
        if 'u' not in text and 'U' not in text:
            return text
        chars = list(text)
        for i, ch in enumerate(chars):
            if ch not in ('u', 'U'):
                continue
            prev_ok = i > 0 and chars[i - 1] in _CONSONANTS
            next_ok = i < len(chars) - 1 and chars[i + 1] in _CONSONANTS
            if prev_ok and next_ok:
                chars[i] = 'v' if ch == 'u' else 'V'
        return ''.join(chars)


    @staticmethod
    def _base_normalize(text: str) -> str:
        """Common normalisation steps shared by both methods below."""
        text = unicodedata.normalize("NFKC", text or "")
        text = text.replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        tokens = text.split(" ") if text else []
        merged = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if len(token) == 1 and token.isalpha():
                letters = [token]
                j = i + 1
                while j < len(tokens) and len(tokens[j]) == 1 and tokens[j].isalpha():
                    letters.append(tokens[j])
                    j += 1
                if len(letters) >= 2:
                    merged.append("".join(letters))
                    i = j
                    continue
            merged.append(token)
            i += 1
        text = " ".join(merged)
        text = re.sub(r"\s+([:;,\.\?\!\)\]])", r"\1", text)
        text = re.sub(r"([\(\[])\s+", r"\1", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    @staticmethod
    def normalize_ocr_text(text: str) -> str:
        """Full normalisation including v/u fix — use for keyword matching."""
        text = Main._base_normalize(text)
        text = Main._fix_vu_confusion(text)
        return text

    @staticmethod
    def normalize_for_query(text: str) -> str:
        return Main._base_normalize(text)


    async def ocr_best(self, image_path, trace=None, card=None, field='text'):
        def _run():
            text = ocr_image(image_path, trace, card, field)
            dprint(f"ocr_best [{image_path}]: '{text}'")
            return text
        return await asyncio.to_thread(_run)


    async def cooldown(self):
        while True:
            await asyncio.sleep(1)
            if self.grab_timer > 0:
                self.grab_timer -= 1
            if self.drop_timer > 0:
                self.drop_timer -= 1

            console.grab_timer = self.grab_timer
            console.drop_timer = self.drop_timer
            console.collected = self.collected
            console.missed = self.missed
            emit('sniper', 'cooldown', 'info', 'Runtime counters', grab_seconds=self.grab_timer,
                 drop_seconds=self.drop_timer, collected=self.collected, unconfirmed=max(self.missed, 0))
            if self.grab_timer == 0 and self.pending_claims:
                for pending_trace in self.pending_claims.values():
                    emit('sniper', 'claim', 'skipped', 'Cooldown expired without collection confirmation', trace=pending_trace, confirmed=False)
                self.pending_claims.clear()

            if console.state in ("WAITING", "IDLE"):
                console.set_state("WAITING" if self.grab_timer > 0 else "IDLE")

            if title and sys.stdout.isatty():
                grab_str = f"Grab: {self.grab_timer}s" if self.grab_timer > 0 else "Grab: ready"
                drop_str = f"Drop: {self.drop_timer}s" if self.drop_timer > 0 else "Drop: ready"
                await asyncio.create_subprocess_shell(
                    f"title Karuta Sniper {v} - Collected {self.collected} - Missed {self.missed} - "
                    f"{grab_str} - {drop_str}"
                )


    async def update_files(self):
        with open(KEYWORDS_DIR / "characters.txt", encoding="utf-8") as ff:
            raw_chars = ff.read().splitlines()
        self.chars = []
        for line in raw_chars:
            parsed = _parse_char_line(line)
            if parsed:
                self.chars.append(parsed)

        with open(KEYWORDS_DIR / "animes.txt", encoding="utf-8") as ff:
            self.animes = ff.read().splitlines()
        with open(KEYWORDS_DIR / "aniblacklist.txt", encoding="utf-8") as ff:
            self.aniblacklist = ff.read().splitlines()
        with open(KEYWORDS_DIR / "charblacklist.txt", encoding="utf-8") as ff:
            self.charblacklist = ff.read().splitlines()

        tprint(
            f"Loaded  {C_TEAL}{len(self.animes)}{R} animes  "
            f"{C_DIM}│{R}  {C_CORAL}{len(self.aniblacklist)}{R} blacklisted  "
            f"{C_DIM}│{R}  {C_TEAL}{len(self.chars)}{R} characters  "
            f"{C_DIM}│{R}  {C_CORAL}{len(self.charblacklist)}{R} blacklisted"
        )

    async def filewatch(self, path):
        watcher = api.FileWatch(path)
        dprint(f"Filewatch activated for {path}")
        while True:
            await asyncio.sleep(2)
            if watcher.watch():
                await self.update_files()

    async def configwatch(self, path):
        watcher = api.FileWatch(path)
        while True:
            await asyncio.sleep(1)
            if watcher.watch():
                try:
                    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
                    new_accuracy = float(cfg["accuracy"])
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    tprint(f"Could not reload config.json: {exc}")
                    continue
                global accuracy, wishlist_lookup_enabled, wishlist_watching_channels
                accuracy = new_accuracy
                wishlist_lookup_enabled = cfg.get("wishlist_lookup", True)
                wishlist_watching_channels = cfg.get("wishlist_watching_channels", channels)
                dprint("Config reloaded")


    async def configured_channel(self, field, channel_id):
        if type(channel_id) is not int or channel_id <= 0 or channel_id == 1234567890:
            raise ChannelConfigurationError(f"Set {field} to a real numeric Discord channel ID in config.json and restart Sniper.")
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise ChannelConfigurationError(f"{field} must be a channel that accepts messages. Update config.json and restart Sniper.")
        return channel

    async def autofarm(self):
        while True:
            try:
                channel = await self.configured_channel('resourcechannel', resourcechannel)
                await self.farm_in(channel)
            except ChannelConfigurationError as exc:
                tprint(f"{C_CORAL}[Autofarm] {exc}{R}")
                return
            except (discord.Forbidden, discord.NotFound):
                tprint(f"{C_CORAL}[Autofarm] Cannot access or send to channel {resourcechannel}. Check the channel ID and permissions, then restart Sniper.{R}")
                return
            except discord.HTTPException as exc:
                tprint(f"{C_CORAL}[Autofarm] {exc} — retrying in 30s{R}")
                await asyncio.sleep(30)

    async def farm_in(self, channel):
        while True:
            async with channel.typing():
                await asyncio.sleep(random.uniform(0.2, 1))
            await channel.send("kw")
            async for message in channel.history(limit=1):
                dprint(message.content)
                if "you do not have" in message.content:
                    farm_print("Autofarm - You don't have a permit")
                else:
                    match1 = re.search(r"(\d+) hour", message.content)
                    match0 = re.search(r"(\d+) minute", message.content)
                    if match1:
                        hours = int(match1.group(1))
                        farm_print(f"Autofarm - Waiting for {hours} hour(s) to work again")
                        await asyncio.sleep(hours * 3600 + 5)
                    elif match0:
                        minutes = int(match0.group(1))
                        farm_print(f"Autofarm - Waiting for {minutes} minutes to work again")
                        await asyncio.sleep(minutes * 60 + 5)
                    else:
                        farm_print("Autofarm - Processing...")
                if message is not None:
                    reply = await self.wait_for(
                        "message",
                        check=lambda m: m.author.id == KARUTA_ID and m.channel.id == channel.id
                    )
                    if reply:
                        await self.autofindresource(channel)
                        await reply.components[0].children[1].click()
                        farm_print("Autofarm - Worked successfully!")
                        await asyncio.sleep(12 * 3600 + 5)

    async def autofindresource(self, channel):
        async with channel.typing():
            await asyncio.sleep(random.uniform(0.2, 1))
        await channel.send("kn")
        reply = await self.wait_for(
            "message",
            check=lambda m: m.author.id == KARUTA_ID and m.channel.id == channel.id and bool(m.embeds)
        )
        a = re.compile(
            r"`(\w+)` · \*\*(\d+)%\*\* tax · \*\*(\d+)(%\*\* power · \*\*(\d+)|\*)",
            re.MULTILINE
        ).findall(reply.embeds[0].to_dict()["description"].replace(",", ""))
        top = 0
        material = None
        for interest in a:
            if not interest.count("") == 1:
                if int(interest[4]) > top:
                    top = int(interest[4])
                    material = interest[0]
            else:
                top     = int(interest[2])
                material = interest[0]
        async with channel.typing():
            await asyncio.sleep(random.uniform(0.9, 1.7))
        await channel.send(f"kjn abcde {material}")


    async def autodrop(self):
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(dropdelay + random.randint(randmin, randmax))
                first_run = False

                channel = await self.configured_channel('autodropchannel', autodropchannel)
                grab_wait, drop_wait = await self.check_kcd(channel)
                wait_secs = max(grab_wait, drop_wait)
                if wait_secs > 0:
                    console.set_state("WAITING")
                    self.grab_timer = grab_wait
                    self.drop_timer = drop_wait
                    remaining = wait_secs + random.randint(5, 15)
                    while remaining > 0:
                        chunk = min(remaining, 30)
                        await asyncio.sleep(chunk)
                        remaining -= chunk

                console.set_state("DROPPING")
                async with channel.typing():
                    await asyncio.sleep(random.uniform(0.2, 1))
                await channel.send("kd")
                self.drop_timer = 1800
                autodrop_print("Cards dropped successfully")
                console.set_state("IDLE")

            except asyncio.CancelledError:
                raise
            except ChannelConfigurationError as exc:
                tprint(f"{C_CORAL}[Autodrop] {exc}{R}")
                return
            except (discord.Forbidden, discord.NotFound):
                console.set_state("IDLE")
                tprint(f"{C_CORAL}[Autodrop] Cannot access or send to channel {autodropchannel}. Check the channel ID and permissions, then restart Sniper.{R}")
                return
            except Exception as e:
                tprint(f"{C_CORAL}[Autodrop] Error ({type(e).__name__}: {e}) — retrying in 30s{R}")
                console.set_state("IDLE")
                first_run = True
                await asyncio.sleep(30)

    async def check_kcd(self, channel):
        async with channel.typing():
            await asyncio.sleep(random.uniform(0.2, 0.6))

        listener_task = asyncio.get_event_loop().create_task(
            self.wait_for(
                "message",
                timeout=10.0,
                check=lambda m: (
                    m.author.id == KARUTA_ID
                    and m.channel.id == channel.id
                    and len(m.embeds) > 0
                )
            )
        )
        await channel.send("kcd")

        try:
            resp = await listener_task
        except asyncio.TimeoutError:
            kcd_print(f"{C_AMBER}No response from Karuta — assuming both ready{R}")
            return 0, 0

        desc = resp.embeds[0].description or ""
        fields_text = " ".join(f.value for f in (resp.embeds[0].fields or []))
        full_text = desc + " " + fields_text
        clean_text = re.sub(r"[*_`~]", "", full_text)

        def parse_cd(text, label):
            if re.search(rf"{label} is currently available", text, re.IGNORECASE):
                return 0
            mins = re.search(rf"{label}.*?available in (\d+) minute", text, re.IGNORECASE)
            if mins:
                return int(mins.group(1)) * 60
            secs = re.search(rf"{label}.*?available in (\d+) second", text, re.IGNORECASE)
            if secs:
                return int(secs.group(1))
            fallback = re.search(
                rf"{label}.*?(\d+)\s*(?:minute|min|second|sec)", text, re.IGNORECASE
            )
            if fallback:
                val = int(fallback.group(1))
                if re.search(rf"{label}.*?{val}\s*(?:minute|min)", text, re.IGNORECASE):
                    return val * 60
                return val
            return 0

        grab_secs = parse_cd(clean_text, "Grab")
        drop_secs = parse_cd(clean_text, "Drop")
        _grab_s = f"{C_MINT}ready{R}" if grab_secs == 0 else f"{C_AMBER}{grab_secs}s{R}"
        _drop_s = f"{C_MINT}ready{R}" if drop_secs == 0 else f"{C_AMBER}{drop_secs}s{R}"
        kcd_print(f"Grab: {_grab_s}  {C_DIM}│{R}  Drop: {_drop_s}")
        return grab_secs, drop_secs


    async def afterclick(self):
        dprint(f"Clicked on button. Grab cooldown set to {GRAB_ATTEMPT_COOLDOWN}s")
        self.grab_timer = GRAB_ATTEMPT_COOLDOWN
        console.grab_timer = self.grab_timer
        self.missed       += 1
        console.missed = self.missed


def fix_ocr_spaces(text):
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def current_time():
    return datetime.now().strftime("%H:%M:%S")


def isbutton(data):
    return data in [648044573536550922, 776520559621570621, 858004885809922078, 857978372688445481]


def tprint(message):
    console.log(message)
def dprint(message):
    if debug:
        console.log_debug(message)
def vprint(message):
    if verbose:
        console.log_info(message)
def drop_print(message):
    if log_drops:
        console.log_grab(message)
def grab_print(message):
    if log_grabs:
        console.log_collect(message)
def wl_print(message):
    if log_wishlist:
        console.log_wl(message)
def autodrop_print(message):
    if log_autodrop:
        console.log_drop(message)
def kcd_print(message):
    if log_kcd:
        console.log_kcd(message)
def farm_print(message):
    if log_autofarm:
        console.log_farm(message)


def main():
    if not token:
        tprint(f"{C_CORAL}Set the Discord token in config.json first.{R}")
        return 1
    client = Main(guild_subscriptions=False)
    tprint(f"{C_MINT}Starting Sniper{R}")
    try:
        client.run(token)
    finally:
        console.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
