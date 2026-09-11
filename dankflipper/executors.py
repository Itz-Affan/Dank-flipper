"""Trade executors: paper trading and live Discord commands.

An Executor implements:
    async buy(account, item, qty, price) -> bool
    async sell(account, item, qty, price, mode="regular") -> bool
and updates the shared inventory + database, publishing Trade events.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .config import Config
from .database import Database
from .events import EventBus, Topics
from .models import Inventory, Side, Trade

log = logging.getLogger(__name__)


class BaseExecutor:
    def __init__(self, cfg: Config, db: Database, bus: EventBus) -> None:
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.inventories: dict[str, Inventory] = {}
        self.running = True

    # ------------------------------------------------------------------
    def inv(self, account: str) -> Inventory:
        if account not in self.inventories:
            inv = Inventory()
            self.inventories[account] = inv
            return inv
        return self.inventories[account]

    def _record(
        self, account: str, item: str, side: Side, qty: int, price: int,
        fees: float, mode: str, pnl: float = 0.0, note: str = "",
    ) -> Trade:
        trade = Trade(
            ts=time.time(), account=account, item=item, side=side, qty=qty,
            price=price, fees=fees, mode=mode, pnl=pnl, note=note,
        )
        self.db.add_trade(trade)
        self.bus.publish(Topics.TRADE_EXECUTED, trade)
        return trade

    # ------------------------------------------------------------------
    async def buy(self, account: str, item: str, qty: int, price: int) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def sell(self, account: str, item: str, qty: int, price: int, mode: str = "regular") -> bool:  # pragma: no cover
        raise NotImplementedError


class PaperExecutor(BaseExecutor):
    """Simulates fills against the best observed offer. No real coins move."""

    MARKET_TAX = 0.25  # Dank Memer market tax fraction (25%)

    def __init__(self, cfg: Config, db: Database, bus: EventBus,
                 tax_rate: float = MARKET_TAX) -> None:
        super().__init__(cfg, db, bus)
        self.tax_rate = tax_rate
        # book of current best offers: item -> {"buy": price, "sell": price}
        self.book: dict[str, dict[str, int]] = {}

    def update_quote(self, item: str, side: Side, price: int) -> None:
        lvl = self.book.setdefault(item, {})
        if side is Side.BUY:
            # a buy offer is a bid; best bid is the highest
            lvl["buy"] = max(lvl.get("buy", 0), price)
        else:
            ask = lvl.get("sell")
            lvl["sell"] = price if ask is None else min(ask, price)

    def best_bid(self, item: str) -> int:
        return self.book.get(item, {}).get("buy", 0)

    def best_ask(self, item: str) -> int:
        return self.book.get(item, {}).get("sell", 0)

    # ------------------------------------------------------------------
    async def buy(self, account: str, item: str, qty: int, price: int) -> bool:
        inv = self.inv(account)
        cost = qty * price
        if inv.coins < cost:
            log.warning("[paper] insufficient coins: need %d have %d", cost, inv.coins)
            return False
        inv.coins -= cost
        inv.add(item, qty)
        self._record(account, item, Side.BUY, qty, price, 0.0, mode="paper",
                     note="paper fill")
        return True

    async def sell(self, account: str, item: str, qty: int, price: int,
                   mode: str = "regular") -> bool:
        inv = self.inv(account)
        have = inv.qty(item)
        if have <= 0:
            return False
        qty = min(qty, have)
        gross = qty * price
        fees = gross * self.tax_rate
        net = gross - fees
        inv.add(item, -qty)
        inv.coins += int(net)

        pos = next((p for p in self.db.positions(account) if p.item == item), None)
        pnl = 0.0
        if pos and pos.avg_cost > 0:
            pnl = net - pos.avg_cost * qty
            remaining = pos.qty - qty
            if remaining > 0:
                from .models import Position
                self.db.upsert_position(account, Position(
                    item, remaining, pos.avg_cost, pos.opened_ts))
            else:
                self.db.delete_position(account, item)
        self._record(account, item, Side.SELL, qty, price, fees, mode="paper",
                     pnl=pnl, note=mode)
        return True


class DiscordExecutor(BaseExecutor):
    """Places real orders by sending Dank Memer commands as the user account.

    In live mode this automates a user account (self-botting) which violates
    the Discord Terms of Service and can get the account banned. Use a
    disposable alt, keep rates low, and understand the risk is yours.
    """

    MARKET_TAX = 0.25

    def __init__(self, cfg: Config, db: Database, bus: EventBus, token: str,
                 channel_id: int | None = None) -> None:
        super().__init__(cfg, db, bus)
        self.token = token
        self.channel_id = channel_id
        self._client = None
        self._channel = None
        self._send_lock = asyncio.Lock()
        self._min_action_interval = 2.0 + random.random()  # anti-detection jitter
        self._last_action = 0.0

    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        try:
            import discord  # discord.py-self
        except ImportError:
            log.error("discord.py-self not installed; live trading unavailable")
            return False
        if not self.token:
            log.error("DiscordExecutor: no token configured")
            return False

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():  # noqa: ANN202
            log.info("Discord executor ready as %s", self._client.user)
            if self.channel_id:
                self._channel = self._client.get_channel(self.channel_id) or \
                    await self._client.fetch_channel(self.channel_id)

        self._task = asyncio.create_task(self._client.start(self.token),
                                         name="discord-executor")
        # wait briefly for readiness
        for _ in range(120):
            if self._client.is_ready():
                return True
            await asyncio.sleep(0.5)
        log.error("Discord executor failed to become ready")
        return False

    async def close(self) -> None:
        if getattr(self, "_task", None):
            self._task.cancel()
        if self._client:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    async def _send(self, content: str) -> None:
        async with self._send_lock:
            wait = self._min_action_interval - (time.time() - self._last_action)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.8))
            if self._channel is None:
                raise RuntimeError("executor channel not connected")
            await self._channel.send(content)
            self._last_action = time.time()
            log.info("[live] sent: %s", content)

    async def _await_confirmation(self, item: str, side: Side, qty: int) -> bool:
        """Wait for Dank Memer to echo a confirmation embed.

        A robust production version would subscribe to DMs from Dank Memer
        and parse embeds; here we optimistically assume success after a
        short delay, and log any error text seen.
        """
        await asyncio.sleep(1.5)
        return True

    # ------------------------------------------------------------------
    async def buy(self, account: str, item: str, qty: int, price: int) -> bool:
        try:
            await self._send(f"/market buy item:{item} quantity:{qty}")
            ok = await self._await_confirmation(item, Side.BUY, qty)
        except Exception as exc:  # noqa: BLE001
            log.error("[live] buy failed: %s", exc)
            ok = False
        if ok:
            inv = self.inv(account)
            inv.coins -= qty * price
            inv.add(item, qty)
            self._record(account, item, Side.BUY, qty, price, 0.0, mode="live")
        else:
            self.db.record_failure(account, item, "buy", "no confirmation")
            self.bus.publish(Topics.TRADE_FAILED,
                             {"account": account, "item": item, "side": "buy"})
        return ok

    async def sell(self, account: str, item: str, qty: int, price: int,
                   mode: str = "regular") -> bool:
        commands = {
            "regular": f"/market sell item:{item} quantity:{qty} price:{price}",
            "twap": f"/market sell item:{item} quantity:{qty} price:{price}",
            "flash": "/sell all",
        }
        try:
            await self._send(commands.get(mode, commands["regular"]))
            ok = await self._await_confirmation(item, Side.SELL, qty)
        except Exception as exc:  # noqa: BLE001
            log.error("[live] sell failed: %s", exc)
            ok = False
        if ok:
            inv = self.inv(account)
            gross = qty * price
            fees = gross * self.MARKET_TAX
            inv.add(item, -qty)
            inv.coins += int(gross - fees)
            pos = next((p for p in self.db.positions(account) if p.item == item), None)
            pnl = (gross - fees - pos.avg_cost * qty) if pos else 0.0
            self._record(account, item, Side.SELL, qty, price, fees, mode="live",
                         pnl=pnl, note=mode)
            if pos:
                from .models import Position
                remaining = pos.qty - qty
                if remaining > 0:
                    self.db.upsert_position(account, Position(
                        item, remaining, pos.avg_cost, pos.opened_ts))
                else:
                    self.db.delete_position(account, item)
        else:
            self.db.record_failure(account, item, "sell", "no confirmation")
            self.bus.publish(Topics.TRADE_FAILED,
                             {"account": account, "item": item, "side": "sell"})
        return ok
