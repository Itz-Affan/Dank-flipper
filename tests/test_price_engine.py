"""Tests for the price estimation engine."""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from dankflipper.config import EstimatorCfg
from dankflipper.database import Database
from dankflipper.models import Side
from dankflipper.price_engine import PriceEngine


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def _seed(db, prices, side, item="widget", spread_hours=0.0, author="u1"):
    now = time.time()
    for i, p in enumerate(prices):
        db.add_observation(item, p, 1, side, source="test", author=author,
                           ts=now - i * spread_hours * 3600)


def test_weighted_median_basic():
    eng = PriceEngine.__new__(PriceEngine)
    eng.cfg = EstimatorCfg()
    values = np.array([10.0, 20.0, 30.0, 40.0])
    weights = np.array([1.0, 1.0, 1.0, 1.0])
    # standard discrete definition: smallest v with cumweight >= half total
    assert eng._weighted_median(values, weights) == 20.0


def test_weighted_median_skewed_by_weights():
    eng = PriceEngine.__new__(PriceEngine)
    eng.cfg = EstimatorCfg()
    values = np.array([10.0, 100.0])
    weights = np.array([10.0, 1.0])
    assert eng._weighted_median(values, weights) == 10.0


def test_iqr_filter_removes_outliers():
    eng = PriceEngine.__new__(PriceEngine)
    eng.cfg = EstimatorCfg()
    # heavy contamination: 4/15 garbage points would break plain IQR
    data = np.array([100.0] * 10 + [1_000_000.0] * 4 + [105.0])
    kept = eng._filter_outliers_iqr(data)
    assert kept.max() < 1_000_000
    # mild contamination: standard IQR fences handle it
    data2 = np.array([100.0] * 20 + [1_000_000.0] * 2)
    kept2 = eng._filter_outliers_iqr(data2)
    assert kept2.max() < 1_000_000


def test_fair_price_uses_recent_data(db):
    # old price 100, recent price 200 -> fair should lean toward 200
    now = time.time()
    for i in range(20):
        db.add_observation("x", 100, 1, Side.SELL, ts=now - 48 * 3600 + i)
    for i in range(20):
        db.add_observation("x", 200, 1, Side.SELL, ts=now - i)
    eng = PriceEngine(db, EstimatorCfg(half_life_hours=12))
    fair = eng.get_fair_price("x")
    assert 150 < fair <= 200


def test_confidence_grows_with_volume(db):
    eng = PriceEngine(db, EstimatorCfg())
    _seed(db, [100] * 3, Side.SELL)
    low = eng.compute("widget").confidence  # compute() bypasses the TTL cache
    _seed(db, [100] * 60, Side.SELL)
    high = eng.compute("widget").confidence
    assert high > low


def test_trend_detects_uptrend(db):
    eng = PriceEngine(db, EstimatorCfg(half_life_hours=1000))
    now = time.time()
    for i in range(40):
        db.add_observation("trendy", 100 + i, 1, Side.SELL, ts=now - (40 - i) * 3600)
    assert eng.get_price_trend("trendy") > 0.05


def test_unknown_item_returns_nan(db):
    eng = PriceEngine(db, EstimatorCfg())
    assert math.isnan(eng.get_fair_price("nope"))


def test_bid_ask_evidence(db):
    _seed(db, [90, 95, 100], Side.BUY)
    _seed(db, [110, 115, 120], Side.SELL)
    eng = PriceEngine(db, EstimatorCfg())
    est = eng.get_estimate("widget")
    assert est.bid is not None and est.ask is not None
    assert est.bid < est.ask
