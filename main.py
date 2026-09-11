"""Dank Memer Flipping Bot — CLI entry point.

Usage:
    python main.py                 # run with config.yaml (headless)
    python main.py --gui           # launch the PySide6 GUI
    python main.py --backtest      # replay DB history through the strategy
    python main.py --source mock   # override market source (mock|discord|both)
    python main.py --mode paper    # override mode (paper|live)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from loguru import logger

from dankflipper.config import load_config


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # route stdlib logging through loguru for nicer output + rotation
    import logging as _lg

    class LoguruHandler(_lg.Handler):
        def emit(self, record: _lg.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelname
            logger.opt(depth=6, exception=record.exc_info).log(
                level, record.getMessage())

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
    logger.add("logs/dankflipper_{time:YYYY-MM-DD}.log",
               rotation="10 MB", retention="14 days", level="DEBUG",
               enqueue=True)
    _lg.getLogger().addHandler(LoguruHandler())


async def run_headless(args: argparse.Namespace) -> None:
    from dankflipper.runtime import Runtime

    cfg = load_config(args.config)
    if args.source:
        cfg.market_source = args.source
    if args.mode:
        cfg.mode = args.mode
    if args.account:
        cfg.accounts = [a for a in cfg.accounts if a.name == args.account] or \
            cfg.accounts[:1]

    rt = Runtime(cfg)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    def on_log(msg) -> None:
        print(f"[bus] {msg}")

    rt.bus.subscribe("log", on_log)

    async def status_loop() -> None:
        while True:
            await asyncio.sleep(60)
            inv = rt.executor.inv(rt.engine.account)
            total = rt.risk.portfolio_value(
                inv, lambda i: rt.prices.get_fair_price(i, refresh=False))
            stats = rt.db.stats_summary()
            print(f"[status] coins={inv.coins:,} items={len(inv.items)} "
                  f"equity~{total:,.0f} trades={stats.get('n', 0)} "
                  f"realized_pnl={rt.db.realized_pnl(rt.engine.account):,.0f} "
                  f"halted={rt.risk.state.halted}")

    tasks = [asyncio.create_task(rt.start()),
             asyncio.create_task(status_loop())]
    await stop.wait()
    print("shutting down...")
    for t in tasks:
        t.cancel()
    await rt.stop()


async def run_backtest(args: argparse.Namespace) -> None:
    from dankflipper.runtime import Runtime

    cfg = load_config(args.config)
    rt = Runtime(cfg)
    try:
        result = rt.run_backtest(start_coins=args.coins)
        print("=" * 60)
        print(f"Backtest result ({args.coins:,} starting coins)")
        print(f"  Final equity : {result.final_equity:,.0f}")
        print(f"  Return       : {result.return_pct:+.2f}%")
        print(f"  Benchmark    : {result.benchmark_return_pct:+.2f}% (hold coins)")
        print(f"  Trades       : {result.n_trades}")
        print(f"  Win rate     : {result.win_rate:.0%}")
        print("=" * 60)
        if not result.n_trades:
            print("No trades were generated. Run the bot with a market source "
                  "first to collect observations, or use --source mock.")
    finally:
        rt.db.close()


def run_gui(args: argparse.Namespace) -> None:
    try:
        import qasync  # noqa: F401
        from PySide6 import QtWidgets  # noqa: F401
    except ImportError as exc:
        print("GUI dependencies missing (PySide6/qasync). Install with:\n"
              "  uv pip install PySide6 qasync\n"
              f"Details: {exc}")
        sys.exit(1)

    from dankflipper.gui.app import main as gui_main
    gui_main(config_path=args.config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dank Memer Flipping Bot")
    parser.add_argument("--gui", action="store_true", help="launch the GUI")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--source", choices=["mock", "discord", "both"],
                        help="override market source")
    parser.add_argument("--mode", choices=["paper", "live"],
                        help="override trading mode")
    parser.add_argument("--account", default=None, help="account name to run")
    parser.add_argument("--backtest", action="store_true",
                        help="run backtest over collected data and exit")
    parser.add_argument("--coins", type=int, default=100_000,
                        help="starting coins for backtest")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.backtest:
        asyncio.run(run_backtest(args))
    elif args.gui:
        run_gui(args)
    else:
        try:
            asyncio.run(run_headless(args))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
