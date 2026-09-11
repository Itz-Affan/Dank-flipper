"""Market data sources.

- MockMarket: generates a realistic synthetic two-sided market for demos,
  paper trading and tests (no Discord connection needed).
- DankAlertClient: optional polling of a DankAlert-style REST API for item
  metadata (marketValue, rarity). Tolerates missing service.
- DiscordMarketSource: discord.py-self user-account client that monitors
  market channels for Dank Memer market postings and trades.

All sources implement: ``async start()``, ``async stop()`` and publish
:class:`~dankflipper.models.MarketEvent` objects into ``on_event``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Awaitable, Callable, Optional

import aiohttp

from .config import Config
from .models import MarketEvent, Side

log = logging.getLogger(__name__)

EventCB = Callable[[MarketEvent], Awaitable[None]]

# --------------------------------------------------------------------------
# Item catalogue used by the mock market (roughly Dank-flavored)
# --------------------------------------------------------------------------
MOCK_ITEMS: dict[str, dict] = {
    "shovel":         {"base": 12500,  "vol": 0.05, "rarity": "common"},
    "fishing_pole":   {"base": 11000,  "vol": 0.05, "rarity": "common"},
    "rifle":          {"base": 21000,  "vol": 0.06, "rarity": "common"},
    "fake_id":        {"base": 32000,  "vol": 0.07, "rarity": "uncommon"},
    "bolt_cutters":   {"base": 28000,  "vol": 0.06, "rarity": "uncommon"},
    "red_coins":      {"base": 8500,   "vol": 0.10, "rarity": "collectible"},
    "green_coins":    {"base": 9000,   "vol": 0.10, "rarity": "collectible"},
    "pepe_frog":      {"base": 150000, "vol": 0.12, "rarity": "rare"},
    "stonks_note":    {"base": 95000,  "vol": 0.14, "rarity": "rare"},
    "diamond_ring":   {"base": 60000,  "vol": 0.09, "rarity": "uncommon"},
}


class MockMarket:
    """Synthetic two-sided market with regime drift and occasional whales."""

    def __init__(self, cfg: Config, on_event: EventCB) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._prices: dict[str, float] = {
            k: float(v["base"]) for k, v in MOCK_ITEMS.items()
        }
        self._drift: dict[str, float] = {k: 0.0 for k in MOCK_ITEMS}
        self._authors = [f"user{i}" for i in range(1, 26)] + ["whale_1", "whale_2"]
        self._step = 0

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="mock-market")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    def _tick(self) -> list[MarketEvent]:
        self._step += 1
        events: list[MarketEvent] = []
        for item, info in MOCK_ITEMS.items():
            # slow random-walk drift + occasional regime jumps
            self._drift[item] += random.uniform(-0.004, 0.004)
            self._drift[item] = max(-0.08, min(0.08, self._drift[item]))
            if random.random() < 0.005:  # regime jump
                self._drift[item] += random.choice([-0.05, 0.05])
            spread = random.uniform(-info["vol"], info["vol"])
            self._prices[item] = max(
                100.0, info["base"] * (1 + self._drift[item]) * (1 + spread)
            )

            n_events = 1 + (random.random() < 0.35) + (random.random() < 0.08)
            for _ in range(int(n_events)):
                side = Side.BUY if random.random() < 0.5 else Side.SELL
                whale = random.random() < 0.06
                qty = random.choice([1, 1, 1, 2, 3, 5, 10, 25]) * (8 if whale else 1)
                price = int(self._prices[item] * random.uniform(0.93, 1.07))
                events.append(MarketEvent(
                    item=item, price=price, quantity=qty, side=side,
                    source="mock",
                    author=random.choice(self._authors) if not whale
                    else random.choice(["whale_1", "whale_2"]),
                ))

            # occasional mispriced offers, like real markets: a panic-dump
            # ask far below fair (snipeable after the 25% tax) or an
            # overpaying whale bid. Keeps paper trading lively.
            roll = random.random()
            if roll < 0.02:
                events.append(MarketEvent(
                    item=item,
                    price=int(self._prices[item] * random.uniform(0.55, 0.68)),
                    quantity=random.choice([1, 1, 2]), side=Side.SELL,
                    source="mock", author="panic_seller",
                ))
            elif roll < 0.035:
                events.append(MarketEvent(
                    item=item,
                    price=int(self._prices[item] * random.uniform(1.25, 1.45)),
                    quantity=random.choice([1, 2]), side=Side.BUY,
                    source="mock", author="whale_1",
                ))
        return events

    async def _loop(self) -> None:
        try:
            while self._running:
                for ev in self._tick():
                    await self.on_event(ev)
                await asyncio.sleep(max(0.5, self.cfg.scan_interval_sec))
        except asyncio.CancelledError:
            raise


# --------------------------------------------------------------------------
# DankAlert-style REST client (optional metadata + market feed)
# --------------------------------------------------------------------------
class DankAlertClient:
    """Polls a DankAlert-style REST API. Missing service is non-fatal."""

    def __init__(self, cfg: Config, on_event: EventCB) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.items: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.dankalert_enabled and self.cfg.dankalert_base_url)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._running = True
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.cfg.dankalert_api_key}"} if
            self.cfg.dankalert_api_key else {}
        )
        self._task = asyncio.create_task(self._loop(), name="dankalert")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    async def _loop(self) -> None:
        backoff = 5.0
        while self._running:
            try:
                await self._poll()
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001
                log.warning("DankAlert poll failed: %s; retrying in %.0fs",
                            exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    async def _poll(self) -> None:
        assert self._session
        base = self.cfg.dankalert_base_url.rstrip("/")
        async with self._session.get(f"{base}/items", timeout=aiohttp.ClientTimeout(15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                self.items = data if isinstance(data, dict) else {}
                log.info("DankAlert: fetched metadata for %d items", len(self.items))
        async with self._session.get(f"{base}/market", timeout=aiohttp.ClientTimeout(15)) as resp:
            if resp.status == 200:
                offers = await resp.json()
                for offer in (offers if isinstance(offers, list) else [])[:200]:
                    try:
                        await self.on_event(MarketEvent(
                            item=str(offer["item"]),
                            price=int(offer["price"]),
                            quantity=int(offer.get("quantity", 1)),
                            side=Side(offer.get("side", "sell")),
                            source="dankalert",
                            author=str(offer.get("author", "")),
                        ))
                    except Exception:  # noqa: BLE001
                        continue


# --------------------------------------------------------------------------
# Discord self-bot source
# --------------------------------------------------------------------------
MARKET_PATTERNS = [
    # "/market sell 3 pepe_frog for 450,000 each" style postings;
    # the for/@/at connector is optional ("WTB 10 red_coins 8k each")
    re.compile(
        r"(?:/market\s+)?(?P<side>buy|sell(?:ing)?)\s+(?P<qty>[\d,]+)\s+"
        r"(?P<item>[a-z0-9_'\- ]+?)\s+(?:(?:for|@|at)\s+)?(?:`)?(?P<price>[\d,.]+[kKmMbB]?)"
        r"(?:\s*(?:each|ea|per))?", re.IGNORECASE),
    # "WTS 5 stonks_note @ 90k" / "WTB 10 red_coins 8k each"
    re.compile(
        r"\bW(?P<side>T[SB])\b\s+(?P<qty>[\d,]+)\s+"
        r"(?P<item>[a-z0-9_'\- ]+?)\s+(?:@|at|for)\s+(?:`)?(?P<price>[\d,.]+[kKmM]?)",
        re.IGNORECASE),
]

NUM_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_price(text: str) -> int:
    text = text.replace(",", "").strip("` ")
    m = re.fullmatch(r"([\d.]+)\s*([kKmMbB]?)", text)
    if not m:
        return int(float(text or 0))
    value = float(m.group(1))
    mult = NUM_SUFFIX.get(m.group(2).lower(), 1)
    return int(value * mult)


def parse_message(content: str) -> list[MarketEvent]:
    """Extract market offers from a Discord message body."""
    events: list[MarketEvent] = []
    # normalize common trading abbreviations so both patterns understand them
    content = re.sub(r"\bWTS\b", "sell", content, flags=re.IGNORECASE)
    content = re.sub(r"\bWTB\b", "buy", content, flags=re.IGNORECASE)
    for pattern in MARKET_PATTERNS:
        for m in pattern.finditer(content):
            side_txt = m.group("side").lower()
            side = Side.BUY if side_txt in {"buy", "tsb", "buying"} else Side.SELL
            try:
                qty = int(m.group("qty").replace(",", "")) or 1
                price = parse_price(m.group("price"))
                item = m.group("item").strip().strip(".,!").replace(" ", "_")
            except (ValueError, AttributeError):
                continue
            if price <= 0 or not item:
                continue
            events.append(MarketEvent(item=item, price=price, quantity=qty,
                                      side=side, source="discord"))
    return events


class DiscordMarketSource:
    """User-account (self-bot) Discord client monitoring market channels.

    Notes
    -----
    discord.py-self logs in as *your* account. Use a dedicated low-value
    alt, expect automation of user accounts to violate Discord ToS, and
    keep action rates low.
    """

    def __init__(self, cfg: Config, token: str, on_event: EventCB) -> None:
        self.cfg = cfg
        self.token = token
        self.on_event = on_event
        self.client = None  # discord client, created lazily
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if not self.token:
            log.warning("Discord source enabled but no token configured; skipping")
            return
        try:
            import discord  # discord.py-self
        except ImportError:
            log.error("discord.py-self not installed; cannot use discord source")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready():  # noqa: ANN202
            log.info("Discord source ready as %s", self.client.user)
            self._ready.set()
            if self.cfg.backfill:
                await self._backfill()

        @self.client.event
        async def on_message(message):  # noqa: ANN001, ANN202
            if message.channel.id not in set(self.cfg.channel_ids):
                return
            for ev in parse_message(message.content or ""):
                ev.channel_id = message.channel.id
                ev.author = str(message.author.id)
                await self.on_event(ev)

        self._task = asyncio.create_task(
            self.client.start(self.token), name="discord-source"
        )

    async def _backfill(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
            for channel_id in self.cfg.channel_ids:
                channel = self.client.get_channel(channel_id)
                if channel is None:
                    continue
                try:
                    async for msg in channel.history(limit=200):
                        for ev in parse_message(msg.content or ""):
                            ev.channel_id = channel_id
                            ev.author = str(msg.author.id)
                            await self.on_event(ev)
                except Exception as exc:  # noqa: BLE001
                    log.warning("backfill failed for %s: %s", channel_id, exc)
        except asyncio.TimeoutError:
            log.warning("Discord client not ready in time; skipping backfill")

    async def stop(self) -> None:
        if self.client:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass
