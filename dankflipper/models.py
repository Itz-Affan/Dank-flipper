"""Core domain models shared across modules."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


def now_s() -> float:
    """Current unix time in seconds (single point for testability)."""
    return time.time()


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"

    def __str__(self) -> str:
        # serialize as the plain lowercase value ('buy'/'sell') everywhere:
        # DB rows, comparisons and f-strings all stay consistent
        return self.value


@dataclass(slots=True)
class MarketEvent:
    """A single market observation: someone offering to buy or sell an item."""

    item: str
    price: int
    quantity: int
    side: Side              # side of the *offer* (what the poster wants to do)
    source: str = "mock"    # mock | discord | dankalert
    ts: float = field(default_factory=now_s)
    author: str = ""        # poster id/username (for whale analytics)
    channel_id: Optional[int] = None

    @property
    def key(self) -> tuple:
        return (self.item, self.side)


@dataclass(slots=True)
class Inventory:
    """Coins plus item holdings for one account."""

    coins: int = 0
    items: dict[str, int] = field(default_factory=dict)  # item -> qty

    def add(self, item: str, qty: int) -> None:
        self.items[item] = self.items.get(item, 0) + qty
        if self.items[item] <= 0:
            self.items.pop(item, None)

    def qty(self, item: str) -> int:
        return self.items.get(item, 0)


@dataclass(slots=True)
class Position:
    """An open position: item bought and awaiting resale."""

    item: str
    qty: int
    avg_cost: float
    opened_ts: float = field(default_factory=now_s)


@dataclass(slots=True)
class Trade:
    """A recorded execution (paper or live)."""

    ts: float
    account: str
    item: str
    side: Side
    qty: int
    price: int              # execution price per unit
    fees: float             # tax paid (coins)
    mode: str               # paper | live
    pnl: float = 0.0        # realized pnl for sells
    note: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price
