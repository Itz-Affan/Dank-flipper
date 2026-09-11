# Dank Flipper — Dank Memer Flipping Bot

A modular Python application that watches the Dank Memer player market,
estimates fair prices, and automates item flipping with risk controls —
with a PySide6 desktop GUI for monitoring and configuration.

```
┌─────────────┐   MarketEvents   ┌──────────────┐   observations   ┌────────────┐
│   Sources   │ ───────────────▶ │   EventBus   │ ───────────────▶ │  Database  │
│ mock /      │                  │  (pub/sub)   │                  │  (SQLite)  │
│ discord /   │                  └──────┬───────┘                  └─────┬──────┘
│ dankalert   │                         │                                │
└─────────────┘                         ▼                                │
                               ┌────────────────┐   fair/confidence      │
                               │  PriceEngine   │ ◀──────────────────────┘
                               └───────┬────────┘
                                       ▼
   ┌───────────┐   checks    ┌──────────────────┐   orders   ┌───────────┐
   │   Risk    │ ◀─────────▶ │  TradingEngine   │ ─────────▶ │ Executors │
   │  Manager  │             │  scan/exec loop  │            │ paper /   │
   └───────────┘             └──────────────────┘            │ discord   │
                                                             └───────────┘
```

## ⚠️ Disclaimer — read first

- **Live mode automates a *user* account** (via `discord.py-self`, a.k.a.
  self-botting). This **violates the Discord Terms of Service** and can get
  the account **banned**. It can also violate Dank Memer's rules and get the
  account wiped. Use a disposable alt with coins you can afford to lose.
- This project is for **educational purposes**. The authors take no
  responsibility for lost coins, lost accounts, or anything else.
- **Paper mode is the default.** Keep it that way until you fully trust your
  configuration.

## Features

| Area | What you get |
|---|---|
| Data collection | Mock synthetic market, Discord channel monitor (regex parser with `k/m/b` prices, WTB/WTS), optional DankAlert REST client; rate-limited Discord commands with jitter |
| Price engine | Recency-weighted (exponential time-decay) median, MAD + IQR outlier filtering, volume×dispersion confidence, trend detection, bid/ask evidence |
| Trading | Opportunity scanner (net-of-tax edge), TWAP splitting, flash sell-all, auto-sell at target, stop-loss, trade cooldowns, failure tracking |
| Risk | Per-trade caps (absolute + % of bank), item exposure caps, consecutive-loss circuit breaker, drawdown circuit breaker, stress testing |
| Inventory | Auto-rebuy essential tools (shovel/fishing pole/rifle), coin/item skew rebalancing |
| Analytics | Whale tracking, buy/sell pressure, correlation scanner, daily briefing, backtester with coin-holding benchmark |
| GUI | Dashboard, watchlist, live price/portfolio charts, trade history search, runtime config panel, multi-account view, pause & panic-sell controls |

## Project layout

```
dankflipper/
  config.py         # dataclass config: defaults <- config.yaml <- env
  database.py       # SQLite: transactions, trades, positions, cooldowns, ...
  events.py         # pub/sub EventBus + Topics
  models.py         # MarketEvent, Inventory, Position, Trade
  price_engine.py   # fair price, confidence, trend
  sources.py        # MockMarket, DankAlertClient, DiscordMarketSource
  executors.py      # PaperExecutor, DiscordExecutor (live commands)
  risk.py           # sizing, caps, circuit breaker, stress test
  inventory.py      # tool auto-buy, skew manager
  trading_engine.py # scanner + execution loop
  analytics.py      # whales, pressure, correlations, briefing, backtester
  runtime.py        # wires everything together (used by GUI and headless)
  gui/              # PySide6 app + panels
main.py             # CLI: headless run, --gui, --backtest
tests/              # pytest suite (27 tests)
```

## Setup

Requires Python 3.10+ (3.13 recommended — PySide6 wheels are most reliable
there).

### With [uv](https://docs.astral.sh/uv/) (recommended)

```bash
uv venv --python 3.13
uv pip install -r requirements.txt
```

### With pip

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows (bash: source .venv/Scripts/activate)
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env          # tokens & channel IDs (never committed)
cp config.example.yaml config.yaml
```

Defaults are safe: `mode: paper`, `market.source: mock` — the bot will
simulate trading against a synthetic market immediately. No Discord account
required.

## Usage

```bash
# GUI (recommended): dashboard, watchlist, charts, manual controls
python main.py --gui

# Headless paper trading on the mock market
python main.py --source mock --mode paper

# Headless against real Discord market channels (see "Live mode" below)
python main.py --source discord --mode paper   # observe only, paper fills
python main.py --source discord --mode live    # REAL orders — ToS risk!

# Replay collected history through the strategy
python main.py --backtest --coins 100000

# Tests
python -m pytest tests/ -v
```

Useful overrides: `--config path/to/config.yaml`, `--account main`, `-v`
(debug logs). Logs rotate into `logs/`.

## Configuration guide

`config.yaml` (see `config.example.yaml` for every knob):

- **trading.profit_threshold** — minimum net-of-tax edge to buy. Dank
  Memer's market tax is ~25%, so a 10% discount vs fair price is only a
  ~5% net edge. Do not set this below ~0.05.
- **trading.min_confidence** — only trade when the estimator is confident
  (enough fresh, consistent observations).
- **trading.max_trade_coins / max_trade_pct** — per-trade caps; the lower
  applies.
- **trading.stop_loss / auto_sell_target** — exit rules relative to cost.
- **risk.\*** — circuit breaker thresholds.
- **inventory.\*** — target cash %, essential tools to keep stocked.
- **estimator.\*** — time-decay half-life, IQR strictness, confidence volume.

Environment (`.env`): `DISCORD_TOKEN`, `MARKET_CHANNEL_IDS`,
`DANKALERT_*`, `DANKFLIP_CONFIG`, `DANKFLIP_DB`.

## How the price engine works

Every observed offer (someone posting a buy *or* sell intent) is one
observation. The engine:

1. culls gross outliers with a MAD fence (survives heavy contamination),
   then applies standard IQR fences;
2. weights remaining prices exponentially by recency
   (`0.5^(age / half_life)`), giving bids a small boost (that's where you
   exit);
3. takes the **weighted median** as fair price;
4. scores confidence as `min(volume_factor, dispersion_factor)`;
5. computes trend as the recent-vs-older median change.

Backtesting note: fair price is estimated from a *blended* bid/ask sample,
so profitable flips come from sniping mispriced offers (panic sells far
below the prevailing bid) rather than from normal spread — exactly like the
real game.

## Live mode checklist

1. Create a **disposable alt** account; join the trading server.
2. Get the token from the Discord client (devtools → Application → Local
   Storage → `token`) and put it in `.env` as `DISCORD_TOKEN`.
3. Set `MARKET_CHANNEL_IDS` to the market channels' numeric IDs
   (devtools → right-click channel → Copy ID).
4. Start with `mode: paper` and `source: discord` for a few days. Inspect
   `python main.py --backtest` results and the trade history.
5. Only then consider `mode: live`. Keep `max_trade_coins` tiny at first.
   Expect Discord to potentially ban the account at any time.

## Extending

- **Confirmation parsing**: `DiscordExecutor._await_confirmation` currently
  assumes success; subscribe to Dank Memer DMs and parse embed replies to
  confirm fills and balances for real.
- **Item metadata**: wire `DankAlertClient.items` into category filters and
  dynamic tax fetching.
- **Web dashboard**: the backend is GUI-agnostic (everything flows through
  `EventBus`); a FastAPI + WebSocket layer can mirror the same topics.
- **More execution modes**: the `sell(mode=...)` plumbing already carries
  `regular | twap | flash | stop_loss`.
