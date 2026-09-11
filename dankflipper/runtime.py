"""Runtime assembly: builds and wires all backend services.

Both the GUI app and the headless CLI use :class:`Runtime` so behavior is
identical; the GUI only adds Qt widgets on top and subscribes to the bus.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from .analytics import (Backtester, CorrelationScanner, DailyBriefing,
                        PressureAnalyzer, WhaleTracker)
from .config import Config
from .database import Database
from .events import EventBus, Topics
from .executors import DiscordExecutor, PaperExecutor
from .inventory import InventoryManager
from .models import MarketEvent
from .price_engine import PriceEngine
from .risk import RiskManager
from .sources import DankAlertClient, DiscordMarketSource, MockMarket
from .trading_engine import TradingEngine

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bus = EventBus()
        self.db = Database(cfg.db_path)
        self.prices = PriceEngine(self.db, cfg.estimator)
        self.risk = RiskManager(cfg, self.db)
        self.inv_mgr = InventoryManager(cfg)

        account = cfg.accounts[0].name
        if cfg.is_live:
            self.executor: PaperExecutor | DiscordExecutor = DiscordExecutor(
                cfg, self.db, self.bus, cfg.accounts[0].token,
                channel_id=(cfg.channel_ids[0] if cfg.channel_ids else None))
        else:
            self.executor = PaperExecutor(cfg, self.db, self.bus)
            self.executor.inv(account).coins = 500_000  # starting paper bank

        self.engine = TradingEngine(
            cfg, self.db, self.bus, self.prices, self.executor,
            self.risk, self.inv_mgr, account=account)

        # analytics services
        self.whales = WhaleTracker()
        self.pressure = PressureAnalyzer()
        self.corr = CorrelationScanner()
        self.briefing = DailyBriefing(cfg, self.db, self.prices,
                                      self.whales, self.pressure, self.corr)
        self.backtester = Backtester(cfg)
        self.bus.subscribe(Topics.MARKET_EVENT, self._on_event_analytics)

        # sources
        self.sources: list = []
        if cfg.market_source in {"mock", "both"}:
            self.sources.append(MockMarket(cfg, self._emit))
        if cfg.market_source in {"discord", "both"}:
            for acct in cfg.accounts:
                if acct.token:
                    self.sources.append(
                        DiscordMarketSource(cfg, acct.token, self._emit))
        self.dankalert = DankAlertClient(cfg, self._emit)
        if self.dankalert.enabled:
            self.sources.append(self.dankalert)

        # periodic tasks
        self._tasks: list[asyncio.Task] = []
        self._last_corr_snapshot = 0.0
        self._last_briefing_day: str | None = None

    # ------------------------------------------------------------------
    async def _emit(self, ev: MarketEvent) -> None:
        """Callback used by sources to push events into the system."""
        self.bus.publish(Topics.MARKET_EVENT, ev)

    def _on_event_analytics(self, ev: MarketEvent) -> None:
        self.whales.record(ev.author, ev.item, ev.price, ev.quantity, ev.side,
                           ev.ts)
        self.pressure.record(ev.item, ev.price, ev.quantity, ev.side, ev.ts)

    # ------------------------------------------------------------------
    async def start(self) -> None:
        for src in self.sources:
            await src.start()
        self._tasks.append(asyncio.create_task(self.engine.run(),
                                               name="trading-engine"))
        self._tasks.append(asyncio.create_task(self._analytics_loop(),
                                               name="analytics"))
        self._tasks.append(asyncio.create_task(self._briefing_loop(),
                                               name="briefing"))
        if self.cfg.is_live and isinstance(self.executor, DiscordExecutor):
            connected = await self.executor.connect()
            if not connected:
                log.error("live mode requested but executor failed to connect; "
                          "staying in paper behavior")
        log.info("runtime started: source=%s mode=%s db=%s",
                 self.cfg.market_source, self.cfg.mode, self.cfg.db_path)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for src in self.sources:
            try:
                await src.stop()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(self.executor, DiscordExecutor):
            await self.executor.close()
        self.db.close()

    # ------------------------------------------------------------------
    async def _analytics_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                # correlation snapshot every 5 minutes
                now = asyncio.get_event_loop().time()
                if now - self._last_corr_snapshot > 300:
                    self._last_corr_snapshot = now
                    snap = {}
                    for item in self.prices.known_items():
                        est = self.prices.get_estimate(item)
                        if est.fair == est.fair:
                            snap[item] = est.fair
                    self.corr.snapshot(snap)
                self.db.prune_observations()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("analytics loop failed")

    async def _briefing_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if not self.cfg.briefing.enabled:
                    continue
                now = datetime.now(timezone.utc)
                day = now.strftime("%Y-%m-%d")
                if (now.hour == self.cfg.briefing.hour_utc
                        and self._last_briefing_day != day):
                    self._last_briefing_day = day
                    inv = self.executor.inv(self.engine.account)
                    total = self.risk.portfolio_value(
                        inv, lambda i: self.prices.get_fair_price(i, refresh=False))
                    text = self.briefing.build(self.engine.account, total,
                                               self.engine.scan())
                    self.bus.publish(Topics.BRIEFING, text)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("briefing loop failed")

    # ------------------------------------------------------------------
    def run_backtest(self, start_coins: int = 100_000):
        return self.backtester.run_from_db(self.db, start_coins)
