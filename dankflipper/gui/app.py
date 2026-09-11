"""GUI application entry: qasync-integrated PySide6 main window."""
from __future__ import annotations

import asyncio
import logging

from PySide6 import QtCore, QtWidgets

from dankflipper.config import load_config

from .widgets import (AccountsPanel, ChartsPanel, ConfigPanel, DashboardPanel,
                      HistoryPanel, ManualPanel, WatchlistPanel)

log = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        self.setWindowTitle("Dank Flipper — Dank Memer Flipping Bot")
        self.resize(1250, 800)

        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)

        self.dashboard = DashboardPanel(runtime)
        self.watchlist = WatchlistPanel(runtime)
        self.config_panel = ConfigPanel(runtime)
        self.history = HistoryPanel(runtime)
        self.manual = ManualPanel(runtime)
        self.charts = ChartsPanel(runtime)
        self.accounts = AccountsPanel(runtime)

        self.tabs.addTab(self.dashboard, "Dashboard")
        self.tabs.addTab(self.watchlist, "Watchlist")
        self.tabs.addTab(self.charts, "Charts")
        self.tabs.addTab(self.history, "Trade History")
        self.tabs.addTab(self.config_panel, "Configuration")
        self.tabs.addTab(self.accounts, "Accounts")
        self.tabs.addTab(self.manual, "Manual Controls")

        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

    def _update_status(self) -> None:
        inv = self.rt.executor.inv(self.rt.engine.account)
        halted = self.rt.risk.state.halted
        self.status.showMessage(
            f"mode={self.rt.cfg.mode} | coins={inv.coins:,} | "
            f"halted={halted} | trading={self.rt.engine.enabled}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.rt.bus.publish("log", "GUI closing; stopping runtime")
        finally:
            super().closeEvent(event)


def main(config_path: str | None = None) -> None:
    """Create the Qt app, runtime and main window; run with qasync."""
    import qasync

    from dankflipper.runtime import Runtime

    cfg = load_config(config_path)
    rt = Runtime(cfg)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow(rt)
    win.show()

    async def start_runtime() -> None:
        await rt.start()

    task = loop.create_task(start_runtime())

    with loop:
        try:
            loop.run_forever()
        finally:
            task.cancel()
            loop.run_until_complete(rt.stop())
