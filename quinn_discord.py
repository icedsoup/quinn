"""Quinn native DM client. The bridge queues work; SillyTavern owns history.
Only responses attached to incoming Discord jobs are delivered. Protocol 4.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
from pathlib import Path

import aiohttp
import discord


ROOT = Path(__file__).parent
QUINN_CFG = ROOT / "quinn_config.json"
SNIPER_CFG = ROOT / "config.json"
BRIDGE_TOKEN = ROOT / "QuinnBridge" / "bridge" / "bridge_token.txt"

log = logging.getLogger("quinn")


def _load_cfg() -> dict:
    if not QUINN_CFG.exists():
        sys.exit(
            f"[Quinn] Config not found: {QUINN_CFG}\n"
            "  Copy quinn_config_template.json → quinn_config.json and fill it in."
        )
    with QUINN_CFG.open(encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_discord_token(cfg: dict) -> str:
    """Discord token: quinn_config → QUINN_DISCORD_TOKEN env → sniper config.json."""
    for source in (
        cfg.get("discord_token", "").strip(),
        os.environ.get("QUINN_DISCORD_TOKEN", "").strip(),
    ):
        if source:
            return source

    if SNIPER_CFG.exists():
        with SNIPER_CFG.open(encoding="utf-8") as fh:
            tok = json.load(fh).get("token", "").strip()
        if tok:
            log.info("Using Discord token from sniper config.json")
            return tok
    sys.exit(
        "[Quinn] No Discord token found.\n"
        "  Set discord_token in quinn_config.json,\n"
        "  or set QUINN_DISCORD_TOKEN env var,\n"
        "  or make sure config.json exists (shared with sniper)."
    )


def _resolve_bridge_token(cfg: dict) -> str:
    """Bridge token: quinn_config → QuinnBridge/bridge/bridge_token.txt."""
    explicit = cfg.get("bridge_token", "").strip()
    if explicit:
        return explicit
    if BRIDGE_TOKEN.exists():
        return BRIDGE_TOKEN.read_text(encoding="utf-8").strip()
    sys.exit(
        "[Quinn] Bridge token not found.\n"
        "  Start the bridge server first (launcher option [2] or [3]),\n"
        "  or set bridge_token in quinn_config.json."
    )


class Bridge:
    __slots__ = ("_base", "_auth", "_sess")

    def __init__(self, base_url: str, token: str):
        self._base = base_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._sess: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        if self._sess is None or self._sess.closed:
            self._sess = aiohttp.ClientSession(headers=self._auth)
        return self._sess

    async def ping(self) -> bool:
        """Unauthenticated health check."""
        try:
            async with (await self._session()).get(
                f"{self._base}/health",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                return r.status == 200
        except Exception:
            return False

    async def enqueue(
        self,
        user_id: str,
        display_name: str,
        message: str,
        avatar_url: str = "",
        channel_id: str | None = None,
        discord_message_id: str = "",
        dm_channel_id: str = "",
    ) -> str | None:
        """
        POST /v1/messages.
        Returns job_id on success, None on failure.
        A missing channel_id identifies a one-to-one DM.
        """
        body = {
            "user_id"     : user_id,
            "display_name": display_name,
            "message"     : message,
            "avatar_url"  : avatar_url,
            "discord_message_id": discord_message_id,
            "dm_channel_id": dm_channel_id,
        }
        if channel_id is not None:
            body["channel_id"] = channel_id
        try:
            async with (await self._session()).post(
                f"{self._base}/v1/messages",
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status in (200, 202):
                    return (await r.json()).get("job_id")
                log.warning("Bridge enqueue → HTTP %s", r.status)
        except Exception as exc:
            log.error("Bridge enqueue error: %s", exc)
        return None

    async def poll(
        self,
        job_id: str,
        interval: float = 1.2,
    ) -> str | None:
        """
        Wait through worker pauses until ready, failed, canceled or already delivered.
        """
        sess = await self._session()
        url = f"{self._base}/v1/jobs/{job_id}"

        while True:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 404:
                        return None
                    if r.status == 200:
                        data = await r.json()
                        match data.get("status"):
                            case "ready":
                                return data.get("reply")
                            case "failed" | "canceled" | "sending" | "sent":
                                log.warning("Job %s failed: %s", job_id, data.get("error"))
                                return None
            except asyncio.CancelledError:
                return None
            except Exception as exc:
                log.debug("Poll transient error (retrying): %s", exc)
            await asyncio.sleep(interval)

    async def action(self, job_id, operation, body):
        async with (await self._session()).post(
            f"{self._base}/v1/jobs/{job_id}/{operation}", json=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            return await response.json() if response.status == 200 else None

    async def pending(self):
        async with (await self._session()).get(f"{self._base}/v1/pending") as response:
            response.raise_for_status()
            return (await response.json())["jobs"]

    async def close(self) -> None:
        if self._sess and not self._sess.closed:
            await self._sess.close()


async def _human_delay(reply: str, cfg: dict) -> None:
    """
    Simulate Quinn actually typing the reply before we send it.

    Longer replies take more time, with diminishing returns and small jitter.
    """
    if not cfg.get("typing_simulation", True):
        return

    chars = len(reply)
    rate = cfg.get("typing_rate_per_char", 0.040)
    min_s = cfg.get("typing_min_seconds",    1.2)
    max_s = cfg.get("typing_max_seconds",    9.0)

    raw = chars * rate
    curved = max_s * (1 - math.exp(-raw / max_s))
    delay = max(min_s, min(curved, max_s))

    jitter = delay * random.uniform(-0.15, 0.15)
    await asyncio.sleep(max(0.5, delay + jitter))


class QuinnClient(discord.Client):
    def __init__(self, cfg: dict, bridge: Bridge, dry_run: bool = False):
        super().__init__()
        self.cfg = cfg
        self.bridge = bridge
        self.dry_run = dry_run

        self._dms = bool(cfg.get("respond_to_dms",      True))

        self._tasks: dict[int, asyncio.Task] = {}
        self._resumed = False
        self._delivery_locks = {}
        self._draining = False
        self._delivering = 0


    async def on_ready(self):
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        log.info("Connected as %s (%s) [%s]", self.user, self.user.id, mode)
        print(f"[Quinn] {self.user}  [{mode}]")
        ok = await self.bridge.ping()
        if ok and not self._resumed and not self.dry_run:
            self._resumed = True
            asyncio.create_task(self._resume())
        print(f"[Quinn] Bridge: {'OK ✓' if ok else 'NOT REACHABLE — start bridge server first'}")
        if os.environ.get('QUINN_LAUNCH_SESSION') and not getattr(self, '_lifecycle_task', None):
            self._lifecycle_task = asyncio.create_task(self._lifecycle())

    async def _lifecycle(self):
        session = os.environ['QUINN_LAUNCH_SESSION']
        while not self.is_closed():
            try:
                http = await self.bridge._session()
                async with http.get(self.bridge._base + '/v1/launcher?session=' + session) as response:
                    state = await response.json()
                    if response.status != 200 or state['desired'] == 'close' or state.get('disconnect'):
                        await self.close()
                        return
                self._draining = state['desired'] != 'run'
                async with http.post(self.bridge._base + '/v1/launcher', json={
                    'operation': 'report', 'session': session, 'role': 'discord',
                    'state': 'busy' if self._delivering else 'ready',
                }) as response:
                    await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self._draining = True
            await asyncio.sleep(0.5)


    def _gate(self, m: discord.Message) -> bool:
        if m.author.id == self.user.id:
            return False
        return self._dms and isinstance(m.channel, discord.DMChannel)


    async def on_message(self, m: discord.Message):
        if self._draining or not self._gate(m) or m.id in self._tasks:
            return
        task = asyncio.create_task(self._handle(m), name=f"msg-{m.id}")
        self._tasks[m.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(m.id, None))


    async def _handle(self, m: discord.Message):

        uid = str(m.author.id)
        name = m.author.display_name
        text = m.content.strip()
        avatar = str(m.author.display_avatar.url)
        if not text:
            return

        log.info("← [%s] %s: %r", uid, name, text[:100])

        if self.dry_run:
            print(f"[Quinn/dry] {name} ({uid}): {text[:60]!r}")
            return

        job_id = await self.bridge.enqueue(
            uid, name, text, avatar_url=avatar,
            discord_message_id=str(m.id), dm_channel_id=str(m.channel.id),
        )
        if not job_id:
            log.warning("Enqueue failed for %s — bridge down?", name)
            return

        await self._deliver(job_id, m.channel)

    async def _deliver(self, job_id, channel):
        lock = self._delivery_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            await self._deliver_locked(job_id, channel)

    async def _deliver_locked(self, job_id, channel):
        reply = await self.bridge.poll(job_id)
        if not reply:
            return
        self._delivering = getattr(self, '_delivering', 0) + 1
        try:
            await QuinnClient._send_reply(self, job_id, channel, reply)
        finally:
            self._delivering -= 1

    async def _send_reply(self, job_id, channel, reply):
        if self.cfg.get("typing_simulation", True):
            try:
                async with channel.typing():
                    await _human_delay(reply, self.cfg)
            except discord.HTTPException:
                pass
        delivery = await self.bridge.action(job_id, "delivery", {})
        if not delivery:
            return
        try:
            sent = await channel.send(delivery["reply"])
        except Exception:
            log.exception("Delivery uncertain for job %s; automatic retry suppressed", job_id)
            return
        for attempt in range(3):
            try:
                if await self.bridge.action(job_id, "ack", {
                    "delivery_token": delivery["delivery_token"],
                    "discord_message_id": str(sent.id),
                }):
                    return
            except Exception:
                log.warning("Delivery acknowledgement failed for job %s", job_id)
            await asyncio.sleep(1)
        log.error("Message sent but acknowledgement uncertain for job %s", job_id)

    async def _resume(self):
        try:
            jobs = await self.bridge.pending()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("Could not load pending deliveries")
            return

        for job in jobs:
            try:
                channel_id = job.get("dm_channel_id")
                if not channel_id:
                    continue
                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                task = asyncio.create_task(self._deliver(job["job_id"], channel))
                job_id = job["job_id"]
                self._tasks[job_id] = task
                task.add_done_callback(lambda _, key=job_id: self._tasks.pop(key, None))
            except (discord.HTTPException, ValueError):
                log.exception("Could not resume delivery for job %s", job["job_id"])


    async def close(self):
        for t in list(self._tasks.values()):
            t.cancel()
        await self.bridge.close()
        await super().close()


async def _run(cfg: dict, dry_run: bool) -> None:
    token = _resolve_discord_token(cfg)
    bridge_token = _resolve_bridge_token(cfg)
    bridge_url = cfg.get("bridge_url", "http://127.0.0.1:8765")

    bridge = Bridge(bridge_url, bridge_token)
    client = QuinnClient(cfg, bridge, dry_run=dry_run)

    try:
        await client.start(token)
    except discord.LoginFailure:
        sys.exit("[Quinn] Discord login failed — check your token.")
    except KeyboardInterrupt:
        pass
    finally:
        if not client.is_closed():
            await client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Quinn — Discord AI character client")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                      help="Log messages without replying")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="Enable replies")
    ap.add_argument("--log",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="Log level")
    args = ap.parse_args()

    cfg = _load_cfg()
    level = args.log or str(cfg.get("log_level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        ap.error("log_level must be DEBUG, INFO, WARNING, or ERROR")
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    dry_run = cfg.get("dry_run", False) if args.dry_run is None else args.dry_run
    asyncio.run(_run(cfg, dry_run=dry_run))


if __name__ == "__main__":
    main()
