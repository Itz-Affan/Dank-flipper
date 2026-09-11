"""Inventory management: auto-buy essential tools and coin/item skew control."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .config import Config
from .models import Inventory

log = logging.getLogger(__name__)


class InventoryManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tool_last_buy: dict[str, float] = {}
        self._last_skew_trade: float = 0.0

    # ------------------------------------------------------------------
    # Essential tools
    # ------------------------------------------------------------------
    def missing_tools(self, inv: Inventory) -> list[str]:
        return [t for t in self.cfg.inventory.essential_tools if inv.qty(t) == 0]

    def should_rebuy_tool(self, tool: str) -> bool:
        last = self._tool_last_buy.get(tool, 0)
        return (time.time() - last) >= self.cfg.inventory.tool_rebuy_cooldown_sec

    def mark_tool_bought(self, tool: str) -> None:
        self._tool_last_buy[tool] = time.time()

    # ------------------------------------------------------------------
    # Coin / item skew
    # ------------------------------------------------------------------
    def skew_action(
        self, inv: Inventory, price_of
    ) -> Optional[tuple[str, str]]:
        """Return ('buy'|'sell', item) to move toward target cash ratio.

        Buys the item with the largest positive trend-adjusted gap when cash
        is overweight, or sells the largest holding when cash is underweight.
        Returns None when within tolerance.
        """
        total = 0.0
        for item, qty in inv.items.items():
            total += qty * max(price_of(item) or 0, 0)
        total += inv.coins
        if total <= 0:
            return None

        cash_pct = inv.coins / total
        target = self.cfg.inventory.target_cash_pct
        tol = self.cfg.inventory.skew_tolerance

        if abs(cash_pct - target) <= tol:
            return None
        if (time.time() - self._last_skew_trade) < self.cfg.inventory.skew_check_interval_sec:
            return None

        if cash_pct > target + tol:
            # too much cash -> buy the cheapest liquid item to rebalance
            item = min(
                (i for i in inv.items or ["shovel"]),  # prefer existing holdings
                key=lambda i: price_of(i) or 1e18,
                default=None,
            )
            if item is None:
                return None
            self._last_skew_trade = time.time()
            return ("buy", item)
        else:
            # too little cash -> sell the largest position
            if not inv.items:
                return None
            item = max(inv.items, key=lambda i: inv.qty(i) * (price_of(i) or 0))
            self._last_skew_trade = time.time()
            return ("sell", item)
