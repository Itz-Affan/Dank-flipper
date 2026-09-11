"""Risk management: position sizing, diversification, circuit breaker,
stress testing, and paper balance utilities."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .database import Database
from .models import Inventory, Position

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskState:
    halted: bool = False
    halt_reason: str = ""
    consecutive_losses: int = 0
    peak_equity: float = 0.0
    peak_ts: float = 0.0


class RiskManager:
    """Guard rails around the trading engine."""

    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.state = RiskState()

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    def max_position_coins(self, inventory: Inventory) -> int:
        """Per-trade cap: absolute cap and % of bank, whichever is lower."""
        bank = max(inventory.coins, 0)
        pct_cap = int(bank * self.cfg.trading.max_trade_pct)
        return max(0, min(self.cfg.trading.max_trade_coins, pct_cap))

    def item_exposure_ok(
        self, account: str, item: str, extra_value: float,
        total_value: float,
    ) -> bool:
        """Would adding extra_value of `item` exceed the single-item cap?

        Conservative: the projected fraction divides by the *current*
        portfolio total (before the new coins are converted to items).
        """
        if total_value <= 0:
            return True
        current = next(
            (p.qty * p.avg_cost for p in self.db.positions(account)
             if p.item == item), 0.0
        )
        projected = (current + extra_value) / max(total_value, 1)
        return projected <= self.cfg.trading.max_item_exposure_pct

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    def record_trade_result(self, pnl: float) -> None:
        if pnl > 0:
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                self.halt(
                    f"circuit breaker: {self.state.consecutive_losses} "
                    "consecutive losses"
                )

    def check_equity(self, total_value: float) -> None:
        """Track peak equity; halt if drawdown exceeds the configured limit."""
        st = self.state
        if total_value > st.peak_equity:
            st.peak_equity = total_value
            st.peak_ts = time.time()
        if st.peak_equity <= 0:
            return
        drawdown = (st.peak_equity - total_value) / st.peak_equity
        if drawdown >= self.cfg.risk.max_portfolio_drop_pct:
            self.halt(
                f"circuit breaker: portfolio drawdown {drawdown:.0%} exceeds "
                f"{self.cfg.risk.max_portfolio_drop_pct:.0%}"
            )

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            log.error("TRADING HALTED: %s", reason)

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_losses = 0
        log.info("Trading resumed by operator")

    # ------------------------------------------------------------------
    # Portfolio helpers
    # ------------------------------------------------------------------
    @staticmethod
    def portfolio_value(inventory: Inventory, price_of) -> float:
        """Total value = coins + items valued at price_of(item) (fair price fn)."""
        return inventory.coins + sum(
            qty * price_of(item) for item, qty in inventory.items.items()
        )

    # ------------------------------------------------------------------
    # Stress testing / scenario analysis
    # ------------------------------------------------------------------
    def stress_test(
        self,
        positions: list[Position],
        coins: int,
        price_of,
        shocks: Optional[dict[str, float]] = None,
    ) -> dict[str, float]:
        """Simulate portfolio value under market shocks.

        shocks maps item -> pct drop (e.g. {"pepe_frog": -0.3}); a global
        "market" key applies to every position. Returns value per scenario.
        """
        base = float(coins)
        scenarios = {"base": base}
        if not positions:
            scenarios["all_items_-20pct"] = base
            scenarios["all_items_-50pct"] = base
            return scenarios

        for name, global_shock in (
            ("market_-10pct", -0.10),
            ("market_-30pct", -0.30),
            ("liquidity_crisis_-50pct", -0.50),
        ):
            value = float(coins)
            for p in positions:
                value += p.qty * price_of(p.item) * (1 + global_shock)
            scenarios[name] = round(value, 2)

        for item, shock in (shocks or {}).items():
            value = float(coins)
            for p in positions:
                factor = shock if p.item == item else 0.0
                value += p.qty * price_of(p.item) * (1 + factor)
            scenarios[f"shock_{item}_{int(shock * 100)}pct"] = round(value, 2)
        return scenarios
