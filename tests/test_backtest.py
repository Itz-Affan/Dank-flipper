"""Backtester + paper executor tests."""
import asyncio

import pytest

from dankflipper.config import Config, TradingCfg
from dankflipper.database import Database
from dankflipper.events import EventBus
from dankflipper.executors import PaperExecutor
from dankflipper.models import Side
from dankflipper.analytics import Backtester


def test_backtest_profitable_sniping():
    cfg = Config()
    cfg.trading = TradingCfg(profit_threshold=0.05, max_trade_pct=0.5,
                             max_trade_coins=1_000_000, auto_sell_target=0.05)
    # Realistic flip scenario: a liquid market clearing at T with constant
    # bids at T, plus occasional panic-dump asks at 0.68*T. The bid-dominated
    # sample makes fair ~= T, so dumped asks carry a ~7% net-of-tax edge
    # (0.75*T resale vs 0.68*T cost) and exits hit bids well above cost.
    # T steps upward over time so profits compound.
    import random

    rng = random.Random(42)
    obs = []
    true_price = 100.0
    for i in range(600):
        if i % 30 == 29:
            true_price += 1.0
        obs.append({"ts": 1_000_000 + i * 60, "item": "snipe",
                    "price": true_price, "side": str(Side.BUY)})
        if rng.random() < 0.15:
            obs.append({"ts": 1_000_000 + i * 60 + 30, "item": "snipe",
                        "price": true_price * 0.68, "side": str(Side.SELL)})
    result = Backtester(cfg).run(obs, start_coins=100_000)
    assert result.n_trades > 0
    assert result.return_pct > 0


def test_backtest_flat_market_no_trades():
    cfg = Config()
    cfg.trading = TradingCfg(profit_threshold=0.90)  # impossibly high
    obs = [{"ts": 1_000_000 + i * 60, "item": "flat", "price": 100.0,
            "side": "sell"} for i in range(100)]
    result = Backtester(cfg).run(obs, start_coins=100_000)
    assert result.n_trades == 0
    assert result.return_pct == 0


def test_paper_executor_buy_sell(tmp_path):
    cfg = Config()
    db = Database(tmp_path / "paper.db")
    bus = EventBus()
    ex = PaperExecutor(cfg, db, bus)
    ex.inv("main").coins = 100_000

    ok = asyncio.run(ex.buy("main", "shovel", 2, 10_000))
    assert ok
    inv = ex.inv("main")
    assert inv.coins == 80_000
    assert inv.qty("shovel") == 2

    ex.update_quote("shovel", Side.BUY, 12_000)
    ok = asyncio.run(ex.sell("main", "shovel", 2, 12_000))
    assert ok
    # gross 24,000, tax 25% -> net 18,000; total 98,000
    assert inv.coins == 98_000
    assert inv.qty("shovel") == 0
    db.close()
