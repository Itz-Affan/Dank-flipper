"""Trading engine: scans the market for profitable flips and executes them.

Strategy
--------
BUY  when the net-of-tax resale value of the best ask exceeds the ask by more
than ``profit_threshold``:

    edge = fair * (1 - tax) - ask
    buy  if edge / ask >= profit_threshold  and confidence >= min_confidence

SELL when the best bid offers a premium over fair value (or on stop-loss /
panic / inventory-skew triggers).
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .database import Database
from .events import EventBus, Topics
from .executors import BaseExecutor, PaperExecutor
from .inventory import InventoryManager
from .models import Inventory, MarketEvent, Side
from .price_engine import PriceEngine
from .risk import RiskManager

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        bus: EventBus,
        prices: PriceEngine,
        executor: BaseExecutor,
        risk: RiskManager,
        inv_mgr: InventoryManager,
        account: str = "main",
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.prices = prices
        self.executor = executor
        self.risk = risk
        self.inv_mgr = inv_mgr
        self.account = account

        self.enabled = True          # soft toggle (Pause Trading in GUI)
        self.tax_rate = 0.25
        self.last_equity_log = 0.0
        self._paper_book = isinstance(executor, PaperExecutor)

        self.bus.subscribe(Topics.MARKET_EVENT, self._on_market_event)

    # ------------------------------------------------------------------
    # Market intake
    # ------------------------------------------------------------------
    def _on_market_event(self, ev: MarketEvent) -> None:
        self.db.add_observation(ev.item, ev.price, ev.quantity, ev.side,
                                ev.source, ev.author, ev.channel_id, ev.ts)
        if self._paper_book:
            self.executor.update_quote(ev.item, ev.side, ev.price)

    def ingest(self, ev: MarketEvent) -> None:
        """Publish an event through the bus (called by sources)."""
        self.bus.publish(Topics.MARKET_EVENT, ev)

    # ------------------------------------------------------------------
    # Opportunity scanning
    # ------------------------------------------------------------------
    def best_offer(self, item: str, side: Side) -> int:
        """Best price observed recently for a given side of the book."""
        rows = self.db.observations(item, max_age_s=3600, side=side)
        if not rows:
            return 0
        prices = [r["price"] for r in rows]
        if side is Side.BUY:
            return max(prices)                      # best bid
        return min(prices)                          # best ask

    def evaluate(self, item: str) -> dict:
        """Full evaluation of an item: fair price, best offers, opportunity."""
        est = self.prices.get_estimate(item)
        bid = self.best_offer(item, Side.BUY)
        if not bid and self._paper_book:
            bid = self.executor.best_bid(item)
        ask = self.best_offer(item, Side.SELL)
        if not ask and self._paper_book:
            ask = self.executor.best_ask(item)
        fair = est.fair
        opportunity = None
        if fair == fair and ask > 0:  # fair is not NaN
            net_resale = fair * (1 - self.tax_rate)
            edge = net_resale - ask
            edge_pct = edge / ask if ask else 0.0
            opportunity = {
                "item": item,
                "buy_price": ask,
                "fair": round(fair, 2),
                "edge_pct": round(edge_pct, 4),
                "confidence": est.confidence,
                "trend": est.trend,
            }
        return {
            "item": item,
            "fair": fair,
            "confidence": est.confidence,
            "trend": est.trend,
            "bid": bid,
            "ask": ask,
            "opportunity": opportunity,
        }

    def scan(self) -> list[dict]:
        """Scan all known items; returns sorted opportunity list."""
        if self.risk.state.halted or not self.enabled:
            return []
        opportunities = []
        for item in self.prices.known_items():
            try:
                evaluation = self.evaluate(item)
            except Exception:  # noqa: BLE001
                continue
            opp = evaluation["opportunity"]
            if not opp:
                continue
            cfg = self.cfg.trading
            if (opp["edge_pct"] >= cfg.profit_threshold
                    and opp["confidence"] >= cfg.min_confidence
                    and not self.db.on_cooldown(self.account, item)):
                opportunities.append(opp)
                self.bus.publish(Topics.OPPORTUNITY, opp)
                if opp["edge_pct"] >= self.cfg.alerts.min_profit_alert:
                    self.bus.publish(Topics.ALERT, {
                        "level": "high",
                        "message": (f"High-profit opportunity: {item} "
                                    f"edge {opp['edge_pct']:.0%} "
                                    f"(conf {opp['confidence']:.0%})"),
                    })
        opportunities.sort(key=lambda o: o["edge_pct"], reverse=True)
        return opportunities

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute_opportunity(self, opp: dict) -> bool:
        item = opp["item"]
        ask = opp["buy_price"]
        inv = self.executor.inv(self.account)

        # risk checks ---------------------------------------------------
        max_spend = self.risk.max_position_coins(inv)
        budget = min(max_spend, inv.coins)
        if budget < ask:
            log.info("skip %s: budget %d < ask %d", item, budget, ask)
            return False
        qty = max(1, int(budget // ask))

        # exposure check
        fair = opp["fair"]
        total_value = self.risk.portfolio_value(
            inv, lambda i: self.prices.get_fair_price(i, refresh=False))
        if not self.risk.item_exposure_ok(self.account, item, qty * ask,
                                          total_value):
            log.info("skip %s: item exposure cap", item)
            return False

        # buy (TWAP split if enabled and order is large)
        n_slices = 1
        cfg = self.cfg.trading
        if cfg.twap_enabled and qty >= cfg.twap_slices * 2:
            n_slices = cfg.twap_slices
        per_slice = max(1, qty // n_slices)
        bought = 0
        for i in range(n_slices):
            slice_qty = per_slice if i < n_slices - 1 else qty - bought
            if slice_qty <= 0:
                break
            ok = await self.executor.buy(self.account, item, slice_qty, ask)
            if not ok:
                break
            bought += slice_qty
            if n_slices > 1:
                await asyncio.sleep(cfg.twap_interval_sec)

        if bought == 0:
            self.db.record_failure(self.account, item, "buy", "execution failed")
            self.db.set_cooldown(self.account, item,
                                 time.time() + cfg.cooldown_sec)
            return False

        # register position (weighted average cost)
        from .models import Position
        existing = next((p for p in self.db.positions(self.account)
                         if p.item == item), None)
        total_qty = bought + (existing.qty if existing else 0)
        avg_cost = ((bought * ask + (existing.qty * existing.avg_cost
                                     if existing else 0)) / max(total_qty, 1))
        pos = Position(item, total_qty, avg_cost, time.time())
        self.db.upsert_position(self.account, pos)
        self.db.set_cooldown(self.account, item, time.time() + cfg.cooldown_sec)

        log.info("BOUGHT %dx %s @ %s (pos %d @ avg %.0f)",
                 bought, item, f"{ask:,}", total_qty, avg_cost)
        self._publish_positions()
        return True

    # ------------------------------------------------------------------
    async def sell_position(self, item: str, qty: int | None = None,
                            mode: str = "regular") -> bool:
        pos = next((p for p in self.db.positions(self.account)
                    if p.item == item), None)
        if pos is None:
            inv = self.executor.inv(self.account)
            if inv.qty(item) <= 0:
                return False
            qty = qty or inv.qty(item)
            avg_cost = 0.0
        else:
            qty = qty or pos.qty
            avg_cost = pos.avg_cost

        bid = self.best_offer(item, Side.BUY)
        price = bid or int((avg_cost or 0) * 1.05) or 1
        ok = await self.executor.sell(self.account, item, qty, price, mode=mode)
        if ok:
            self.db.set_cooldown(self.account, item,
                                 time.time() + self.cfg.trading.cooldown_sec)
            self.risk.record_trade_result(
                (price * (1 - self.tax_rate) - avg_cost) * qty
                if avg_cost else 0.0)
            self._publish_positions()
        return ok

    # ------------------------------------------------------------------
    async def twap_sell(self, item: str, slices: int | None = None) -> int:
        """Split a sell over time to reduce price impact."""
        pos = next((p for p in self.db.positions(self.account)
                    if p.item == item), None)
        if pos is None:
            return 0
        slices = slices or self.cfg.trading.twap_slices
        per = max(1, pos.qty // slices)
        sold = 0
        for i in range(slices):
            q = per if i < slices - 1 else pos.qty - sold
            if q <= 0:
                break
            if await self.sell_position(item, q, mode="twap"):
                sold += q
                await asyncio.sleep(self.cfg.trading.twap_interval_sec)
            else:
                break
        return sold

    async def flash_sell_all(self) -> int:
        """Liquidate the entire inventory immediately (FSH)."""
        inv = self.executor.inv(self.account)
        sold_any = 0
        for item in list(inv.items.keys()):
            if await self.sell_position(item, mode="flash"):
                sold_any += inv.qty(item)
        return sold_any

    # ------------------------------------------------------------------
    # Auto-sell / stop-loss / rebuy tools / skew (periodic tasks)
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Main trading loop."""
        log.info("Trading engine started (account=%s, mode=%s)",
                 self.account, self.cfg.mode)
        while True:
            try:
                await asyncio.sleep(self.cfg.scan_interval_sec)
                if not self.enabled or self.risk.state.halted:
                    continue

                # 1. take the best opportunity
                opps = self.scan()
                if opps:
                    await self.execute_opportunity(opps[0])

                # 2. auto-sell holdings at/above target
                await self._auto_sell_pass()

                # 3. stop-loss check
                await self._stop_loss_pass()

                # 4. keep essential tools stocked
                if self.cfg.trading.auto_buy_tools:
                    await self._ensure_tools()

                # 5. inventory skew
                action = self.inv_mgr.skew_action(
                    self.executor.inv(self.account),
                    lambda i: self.prices.get_fair_price(i, refresh=False))
                if action:
                    mode, item = action
                    inv = self.executor.inv(self.account)
                    if mode == "sell":
                        await self.sell_position(item, mode="regular")
                    elif inv.coins > 0:
                        price = self.best_offer(item, Side.SELL) or 0
                        qty = max(1, min(inv.coins // max(price, 1), 5))
                        if price:
                            await self.executor.buy(self.account, item, qty, price)

                # 6. equity tracking
                self._log_equity()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("trading loop iteration failed")

    async def _auto_sell_pass(self) -> None:
        for pos in self.db.positions(self.account):
            bid = self.best_offer(pos.item, Side.BUY)
            if bid <= 0:
                continue
            target = pos.avg_cost * (1 + self.cfg.trading.auto_sell_target)
            if bid >= target:
                log.info("auto-sell %s: bid %s >= target %.0f",
                         pos.item, f"{bid:,}", target)
                await self.sell_position(pos.item, mode="regular")

    async def _stop_loss_pass(self) -> None:
        for pos in self.db.positions(self.account):
            bid = self.best_offer(pos.item, Side.BUY)
            if bid <= 0:
                continue
            stop = pos.avg_cost * (1 - self.cfg.trading.stop_loss)
            if bid < stop:
                log.warning("STOP-LOSS %s: bid %s < %.0f",
                            pos.item, f"{bid:,}", stop)
                await self.sell_position(pos.item, mode="stop_loss")

    async def _ensure_tools(self) -> None:
        inv = self.executor.inv(self.account)
        for tool in self.inv_mgr.missing_tools(inv):
            if not self.inv_mgr.should_rebuy_tool(tool):
                continue
            ask = self.best_offer(tool, Side.SELL)
            if ask <= 0:
                continue
            if ask <= inv.coins and self.risk.max_position_coins(inv) >= ask:
                ok = await self.executor.buy(self.account, tool, 1, ask)
                if ok:
                    self.inv_mgr.mark_tool_bought(tool)

    def _log_equity(self) -> None:
        inv = self.executor.inv(self.account)
        total = self.risk.portfolio_value(
            inv, lambda i: self.prices.get_fair_price(i, refresh=False))
        self.db.add_equity(self.account, total, inv.coins)
        self.bus.publish(Topics.EQUITY_UPDATE,
                         {"account": self.account, "total": total,
                          "coins": inv.coins})
        self.risk.check_equity(total)
        self.last_equity_log = time.time()

    def _publish_positions(self) -> None:
        self.bus.publish(Topics.POSITIONS_UPDATE,
                         self.db.positions(self.account))
