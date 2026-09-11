"""Tests for Discord message parsing."""
from dankflipper.models import Side
from dankflipper.sources import parse_message, parse_price


def test_parse_price_plain():
    assert parse_price("450000") == 450000
    assert parse_price("450,000") == 450000


def test_parse_price_suffixes():
    assert parse_price("450k") == 450_000
    assert parse_price("1.5m") == 1_500_000
    assert parse_price("2b") == 2_000_000_000


def test_parse_market_sell_command():
    msgs = parse_message("/market sell 3 pepe_frog for 450,000 each")
    assert len(msgs) == 1
    ev = msgs[0]
    assert ev.side is Side.SELL
    assert ev.quantity == 3
    assert ev.item == "pepe_frog"
    assert ev.price == 450_000


def test_parse_wtb_wts():
    msgs = parse_message("WTS 5 stonks_note @ 90k")
    assert len(msgs) == 1
    assert msgs[0].side is Side.SELL
    assert msgs[0].price == 90_000

    msgs = parse_message("WTB 10 red_coins 8k each")
    assert len(msgs) == 1
    assert msgs[0].side is Side.BUY
    assert msgs[0].price == 8_000


def test_parse_multiple_lines():
    content = "WTB 2 fishing_pole for 11k each\nWTS 1 rifle @ 21,000"
    msgs = parse_message(content)
    assert len(msgs) == 2


def test_garbage_returns_empty():
    assert parse_message("hello world, nothing here") == []
    assert parse_message("") == []
