"""Database layer tests."""
import time

from dankflipper.database import Database
from dankflipper.models import Position, Side, Trade


def test_roundtrip_observation(tmp_path):
    db = Database(tmp_path / "db.db")
    db.add_observation("shovel", 12_500, 3, Side.BUY, source="test",
                       author="u1", channel_id=123, ts=1_000.0)
    rows = db.observations("shovel")
    assert len(rows) == 1
    assert rows[0]["price"] == 12_500
    assert rows[0]["side"] == str(Side.BUY)
    db.close()


def test_observations_max_age(tmp_path):
    db = Database(tmp_path / "db.db")
    now = time.time()
    db.add_observation("x", 10, 1, Side.SELL, ts=now - 10_000)
    db.add_observation("x", 20, 1, Side.SELL, ts=now - 10)
    assert len(db.observations("x", max_age_s=60)) == 1
    db.close()


def test_trades_and_pnl(tmp_path):
    db = Database(tmp_path / "db.db")
    db.add_trade(Trade(ts=time.time(), account="a", item="x", side=Side.BUY,
                       qty=1, price=100, fees=0, mode="paper"))
    db.add_trade(Trade(ts=time.time(), account="a", item="x", side=Side.SELL,
                       qty=1, price=150, fees=37.5, mode="paper", pnl=12.5))
    assert db.realized_pnl("a") == 12.5
    assert len(db.trades()) == 2
    db.close()


def test_positions_upsert_delete(tmp_path):
    db = Database(tmp_path / "db.db")
    db.upsert_position("a", Position("x", 5, 100.0))
    db.upsert_position("a", Position("x", 8, 110.0))
    db.upsert_position("a", Position("y", 1, 50.0))
    positions = db.positions("a")
    assert len(positions) == 2
    px = next(p for p in positions if p.item == "x")
    assert px.qty == 8 and px.avg_cost == 110.0
    db.delete_position("a", "x")
    assert len(db.positions("a")) == 1
    db.close()


def test_cooldowns_and_settings(tmp_path):
    db = Database(tmp_path / "db.db")
    db.set_cooldown("a", "x", time.time() + 60)
    assert db.on_cooldown("a", "x")
    db.set_cooldown("a", "x", time.time() - 1)
    assert not db.on_cooldown("a", "x")

    db.set_setting("watchlist", "a,b,c")
    assert db.get_setting("watchlist") == "a,b,c"
    assert db.get_setting("missing", "dflt") == "dflt"
    db.close()
