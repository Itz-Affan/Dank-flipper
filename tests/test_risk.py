"""Tests for the risk manager."""
from dankflipper.config import Config, RiskCfg, TradingCfg
from dankflipper.database import Database
from dankflipper.models import Inventory, Position
from dankflipper.risk import RiskManager


def make_risk(tmp_path, **overrides):
    cfg = Config()
    risk_keys = set(RiskCfg.__dataclass_fields__)
    for k, v in overrides.items():
        if k in risk_keys:
            setattr(cfg.risk, k, v)
        else:
            setattr(cfg.trading, k, v)
    db = Database(tmp_path / "risk.db")
    return RiskManager(cfg, db), db


def test_max_position_coins(tmp_path):
    risk, _ = make_risk(tmp_path, max_trade_coins=50_000, max_trade_pct=0.20)
    inv = Inventory(coins=100_000)
    assert risk.max_position_coins(inv) == 20_000  # pct cap binds

    inv2 = Inventory(coins=1_000_000)
    assert risk.max_position_coins(inv2) == 50_000  # absolute cap binds


def test_circuit_breaker_on_losses(tmp_path):
    risk, _ = make_risk(tmp_path, max_consecutive_losses=3)
    risk.record_trade_result(-1)
    risk.record_trade_result(-1)
    assert not risk.state.halted
    risk.record_trade_result(-1)
    assert risk.state.halted
    risk.resume()
    assert not risk.state.halted


def test_drawdown_halt(tmp_path):
    risk, _ = make_risk(tmp_path, max_portfolio_drop_pct=0.20)
    risk.check_equity(100_000)   # sets peak
    risk.check_equity(95_000)    # 5% dd, ok
    risk.check_equity(75_000)    # 25% dd -> halt
    assert risk.state.halted


def test_stress_test_scenarios(tmp_path):
    risk, _ = make_risk(tmp_path)
    positions = [Position("pepe_frog", 2, 150_000)]
    result = risk.stress_test(positions, coins=100_000,
                              price_of=lambda i: 150_000)
    assert result["base"] == 100_000
    assert result["market_-30pct"] < result["market_-10pct"]


def test_item_exposure_cap(tmp_path):
    risk, db = make_risk(tmp_path, max_item_exposure_pct=0.35)
    db.upsert_position("acct", Position("big", 1, 400_000))
    # conservative denominator: current portfolio total
    # 400k existing + 200k new over 500k portfolio = 120% -> reject
    assert not risk.item_exposure_ok("acct", "big", 200_000, 500_000)
    # 400k + 10k over 500k = 82% -> still over cap, reject
    assert not risk.item_exposure_ok("acct", "big", 10_000, 500_000)
    # fresh small item: 10k over 500k = 2% -> allow
    assert risk.item_exposure_ok("acct", "small", 10_000, 500_000)
