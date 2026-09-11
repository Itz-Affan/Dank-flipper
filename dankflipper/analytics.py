"""Market intelligence & analytics.

- WhaleTracker: large-transaction detection and per-author aggregation
- PressureAnalyzer: buy/sell pressure per item over a rolling window
- CorrelationScanner: price-move correlation between items
- DailyBriefing: markdown summary of portfolio, P/L, movers, opportunities
- Backtester: replay historical observations through the estimator + simple
  flip strategy; benchmark against holding coins
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np

from dataclasses import dataclass

from .config import Config
from .database import Database
from .events import EventBus, Topics
from .models import Side
from .price_engine import PriceEngine

log = logging.getLogger(__name__)

WHALE_VALUE_THRESHOLD = 1_000_000  # coins notional per single offer


# ---------------------------------------------------------------------------
# Whale & pressure analytics
# ---------------------------------------------------------------------------
class WhaleTracker:
    def __init__(self, window_s: float = 24 * 3600) -> None:
        self.window_s = window_s
        self._by_author: dict[str, deque] = defaultdict(deque)
        self._recent_whales: deque = deque(maxlen=100)

    def record(self, author: str, item: str, price: float, qty: int,
               side: Side, ts: float | None = None) -> None:
        ts = ts or time.time()
        notional = price * qty
        if not author:
            return
        dq = self._by_author[author]
        dq.append((ts, item, price, qty, side, notional))
        while dq and dq[0][0] < ts - self.window_s:
            dq.popleft()
        if notional >= WHALE_VALUE_THRESHOLD:
            self._recent_whales.append({
                "ts": ts, "author": author, "item": item, "price": price,
                "qty": qty, "side": str(side), "notional": notional,
            })

    def whales(self, limit: int = 10) -> list[dict]:
        return list(self._recent_whales)[-limit:]

    def top_authors(self, limit: int = 5) -> list[tuple[str, float]]:
        totals = {a: sum(x[5] for x in dq) for a, dq in self._by_author.items()}
        return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]


class PressureAnalyzer:
    """Buy/sell pressure per item over a rolling window (score in -1..+1)."""

    def __init__(self, window_s: float = 3600) -> None:
        self.window_s = window_s
        self._events: dict[str, deque] = defaultdict(deque)

    def record(self, item: str, price: float, qty: int, side: Side,
               ts: float | None = None) -> None:
        ts = ts or time.time()
        dq = self._events[item]
        dq.append((ts, str(side), price * qty))
        while dq and dq[0][0] < ts - self.window_s:
            dq.popleft()

    def pressure(self, item: str) -> float:
        dq = self._events.get(item)
        if not dq:
            return 0.0
        buy_vol = sum(x[2] for x in dq if x[1] == str(Side.BUY))
        sell_vol = sum(x[2] for x in dq if x[1] == str(Side.SELL))
        total = buy_vol + sell_vol
        if total == 0:
            return 0.0
        return round((buy_vol - sell_vol) / total, 3)


# ---------------------------------------------------------------------------
# Correlation scanner
# ---------------------------------------------------------------------------
class CorrelationScanner:
    """Correlates per-item median price snapshots through time."""

    def __init__(self, max_points: int = 720) -> None:
        self.max_points = max_points
        self._series: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_points))

    def snapshot(self, prices: dict[str, float]) -> None:
        for item, price in prices.items():
            if price == price and price > 0:  # skip NaN/0
                self._series[item].append(price)

    def correlations(self, min_len: int = 30) -> dict[tuple[str, str], float]:
        items = [i for i, s in self._series.items() if len(s) >= min_len]
        out: dict[tuple[str, str], float] = {}
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                sa, sb = np.diff(np.log(list(self._series[a]))), \
                    np.diff(np.log(list(self._series[b])))
                n = min(len(sa), len(sb))
                if n < min_len - 1:
                    continue
                corr = float(np.corrcoef(sa[-n:], sb[-n:])[0, 1])
                if abs(corr) >= 0.6:
                    out[(a, b)] = round(corr, 3)
        return out

    def top_pairs(self, limit: int = 10) -> list[dict]:
        pairs = sorted(self.correlations().items(),
                       key=lambda kv: abs(kv[1]), reverse=True)[:limit]
        return [{"a": a, "b": b, "corr": c} for (a, b), c in pairs]


# ---------------------------------------------------------------------------
# Daily briefing
# ---------------------------------------------------------------------------
class DailyBriefing:
    def __init__(self, cfg: Config, db: Database, prices: PriceEngine,
                 whales: WhaleTracker, pressure: PressureAnalyzer,
                 correlation: CorrelationScanner) -> None:
        self.cfg = cfg
        self.db = db
        self.prices = prices
        self.whales = whales
        self.pressure = pressure
        self.corr = correlation

    def build(self, account: str, portfolio_value: float,
              opportunities: list[dict]) -> str:
        stats = self.db.stats_summary()
        wins, losses = stats.get("wins") or 0, stats.get("losses") or 0
        total = wins + losses
        win_rate = f"{wins / total:.0%}" if total else "n/a"

        top_movers = []
        for item, est in list(self.prices.all_estimates().items())[:50]:
            if est.trend:
                top_movers.append((item, est.trend))
        top_movers.sort(key=lambda kv: abs(kv[1]), reverse=True)
        movers_txt = "\n".join(
            f"  - {i}: {t:+.1%}" for i, t in top_movers[:5]) or "  - none"

        opp_txt = "\n".join(
            f"  - {o['item']}: edge {o['edge_pct']:.0%} @ {o['buy_price']:,}"
            for o in opportunities[:5]) or "  - none"

        pressure_txt = "\n".join(
            f"  - {item}: {self.pressure.pressure(item):+.2f}"
            for item in list(self.prices.known_items())[:8]) or "  - none"

        pairs = self.corr.top_pairs(3)
        corr_txt = "\n".join(
            f"  - {p['a']} ~ {p['b']}: {p['corr']:+.2f}" for p in pairs) \
            or "  - none"

        return (
            "**📊 Daily Dank Flipper Briefing**\n"
            f"Portfolio value: **{portfolio_value:,.0f} coins**\n"
            f"Realized P/L: **{self.db.realized_pnl(account):,.0f}** "
            f"(win rate {win_rate} over {total} closed trades)\n\n"
            f"**Top movers**\n{movers_txt}\n\n"
            f"**Best opportunities now**\n{opp_txt}\n\n"
            f"**Market pressure**\n{pressure_txt}\n\n"
            f"**Correlated pairs**\n{corr_txt}\n\n"
            f"**Recent whales**\n" + "\n".join(
                f"  - {w['author']} {'bought' if w['side'] == 'buy' else 'sold'} "
                f"{w['qty']}x {w['item']} @ {w['price']:,}"
                for w in self.whales.whales(5)) or "  - none"
        )


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------
@dataclass
class BacktestResult:
    final_equity: float
    start_equity: float
    return_pct: float
    benchmark_return_pct: float
    n_trades: int
    win_rate: float
    equity_curve: list[tuple[float, float]]


class Backtester:
    """Replays historical observations through the estimator + a simple flip
    strategy, benchmarking against simply holding coins.

    Strategy (mirrors the live engine):
      - track fair price from observations seen so far (expanding window)
      - buy when net-of-tax fair value exceeds the observed ask by >= threshold
      - sell when the best bid >= cost * (1 + target)
    """

    def __init__(self, cfg: Config, tax_rate: float = 0.25) -> None:
        self.cfg = cfg
        self.tax = tax_rate

    def run(self, observations: list[dict], start_coins: int = 100_000) -> BacktestResult:
        obs = sorted(observations, key=lambda o: o["ts"])
        coins = float(start_coins)
        holdings: dict[str, tuple[int, float]] = {}   # item -> (qty, avg_cost)
        history: list[tuple[float, float]] = []
        wins = trades = 0
        seen: dict[str, list[tuple[float, float]]] = defaultdict(list)

        def fair_of(item: str, ts: float, half_life_h: float = 6.0) -> float:
            pts = seen.get(item)
            if not pts:
                return float("nan")
            prices = np.array([p for _, p in pts[-200:]])
            ages = np.array([max(ts - t, 0.0) for t, _ in pts[-200:]])
            weights = np.power(0.5, ages / (half_life_h * 3600))
            order = np.argsort(prices)
            v, w = prices[order], weights[order]
            cw = np.cumsum(w)
            return float(v[int(np.searchsorted(cw, 0.5 * cw[-1]))])

        last_price: dict[str, float] = {}
        for o in obs:
            item, price, ts = o["item"], o["price"], o["ts"]
            side = o["side"]
            seen[item].append((ts, price))
            last_price[item] = price

            # sell logic on strong bids
            if side == str(Side.BUY) and item in holdings:
                qty, cost = holdings[item]
                if price >= cost * (1 + self.cfg.trading.auto_sell_target):
                    gross = qty * price
                    net = gross * (1 - self.tax)
                    coins += net
                    if net > qty * cost:
                        wins += 1
                    del holdings[item]
                    trades += 1

            # buy logic on cheap asks
            elif side == str(Side.SELL):
                fair = fair_of(item, ts)
                if fair == fair and fair > 0:
                    edge = fair * (1 - self.tax) - price
                    if price > 0 and edge / price >= self.cfg.trading.profit_threshold:
                        spend = min(coins * self.cfg.trading.max_trade_pct,
                                    self.cfg.trading.max_trade_coins, coins)
                        if spend >= price:
                            qty = int(spend // price)
                            if qty > 0:
                                coins -= qty * price
                                q0, c0 = holdings.get(item, (0, 0.0))
                                tq = q0 + qty
                                holdings[item] = (tq, (q0 * c0 + qty * price) / tq)
                                trades += 1

            # mark-to-market every ~50 observations
            if len(history) == 0 or ts - history[-1][0] > 60:
                mtm = coins + sum(q * last_price.get(i, c)
                                  for i, (q, c) in holdings.items())
                history.append((ts, mtm))

        final_mtm = coins + sum(q * last_price.get(i, c)
                                for i, (q, c) in holdings.items())
        # remaining holdings sold at last price net of tax (conservative exit)
        liquid = coins + sum(q * last_price.get(i, c) * (1 - self.tax)
                             for i, (q, c) in holdings.items())
        start = float(start_coins)
        return BacktestResult(
            final_equity=round(liquid, 2),
            start_equity=start,
            return_pct=round((liquid - start) / start * 100, 2),
            benchmark_return_pct=0.0,   # holding coins is flat by definition
            n_trades=trades,
            win_rate=round(wins / trades, 3) if trades else 0.0,
            equity_curve=history,
        )

    def run_from_db(self, db: Database, start_coins: int = 100_000) -> BacktestResult:
        rows = db.recent_events(limit=200_000)
        obs = [{"ts": r["ts"], "item": r["item"], "price": r["price"],
                "side": r["side"]} for r in rows]
        return self.run(obs, start_coins)
