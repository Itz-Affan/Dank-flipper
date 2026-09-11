"""Minimal pub/sub event bus with async handler dispatch.

Decouples GUI from backend: backend components publish events, the GUI
subscribes. Handlers are plain sync callables kept fast, or async ones
scheduled on the running loop.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        try:
            self._subs[topic].remove(handler)
        except ValueError:
            pass

    def publish(self, topic: str, payload: Any = None) -> None:
        for handler in list(self._subs.get(topic, ())):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.get_event_loop_policy().get_event_loop().create_task(result)  # type: ignore[attr-defined]
                    else:
                        loop.create_task(result)
            except Exception:  # noqa: BLE001 - a bad handler must not kill the bus
                log.exception("event handler failed for topic %s", topic)


#: Canonical topic names (avoid string typos across modules).
class Topics:
    MARKET_EVENT = "market.event"          # MarketEvent observed
    PRICE_UPDATE = "price.update"          # {item, fair, confidence, trend}
    OPPORTUNITY = "trade.opportunity"      # {item, buy_price, fair, edge}
    TRADE_EXECUTED = "trade.executed"      # Trade
    TRADE_FAILED = "trade.failed"          # {item, side, reason}
    POSITIONS_UPDATE = "positions.update"  # list[Position]
    EQUITY_UPDATE = "equity.update"        # {coins, items_value, total}
    RISK_HALT = "risk.halt"                # {reason}
    LOG = "log"                            # human readable strings
    BRIEFING = "briefing"                  # markdown briefing text
    ALERT = "alert"                        # {level, message}
