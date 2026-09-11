"""GUI panels for the Dank Flipper application."""
from __future__ import annotations

import asyncio
import time

from PySide6 import QtCore, QtGui, QtWidgets

from dankflipper.events import Topics
from dankflipper.models import Side, Trade
from dankflipper.price_engine import Estimate

COLOR_GREEN = "#7CFC98"
COLOR_RED = "#FF6B6B"


def _fmt(n) -> str:
    try:
        return f"{n:,.0f}"
    except (TypeError, ValueError):
        return str(n)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class DashboardPanel(QtWidgets.QWidget):
    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        layout = QtWidgets.QVBoxLayout(self)

        # stats row -----------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.stat_labels: dict[str, QtWidgets.QLabel] = {}
        for name in ("Equity", "Coins", "Open positions", "Win rate",
                     "Realized P/L", "Status"):
            box = QtWidgets.QVBoxLayout()
            title = QtWidgets.QLabel(name)
            title.setStyleSheet("color:#9aa4b2; font-size:11px;")
            val = QtWidgets.QLabel("—")
            val.setStyleSheet("font-size:20px; font-weight:600;")
            box.addWidget(title)
            box.addWidget(val)
            wrap = QtWidgets.QWidget()
            wrap.setLayout(box)
            row.addWidget(wrap)
            self.stat_labels[name] = val
        layout.addLayout(row)

        # live trade log -------------------------------------------------
        self.log_view = QtWidgets.QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Live trade/market log...")
        layout.addWidget(self.log_view, 1)

        runtime.bus.subscribe(Topics.TRADE_EXECUTED, self._on_trade)
        runtime.bus.subscribe(Topics.OPPORTUNITY, self._on_opportunity)
        runtime.bus.subscribe(Topics.RISK_HALT, self._on_halt)
        runtime.bus.subscribe(Topics.ALERT, self._on_alert)
        runtime.bus.subscribe(Topics.EQUITY_UPDATE, self._on_equity)
        runtime.bus.subscribe(Topics.POSITIONS_UPDATE, self._on_positions)

        self._positions: list = []
        self._trades: list[Trade] = []

        refresh = QtCore.QTimer(self)
        refresh.setInterval(2000)
        refresh.timeout.connect(self.refresh)
        refresh.start()

    # ------------------------------------------------------------------
    def _append_log(self, text: str, color: str | None = None) -> None:
        if color:
            self.log_view.appendHtml(f'<span style="color:{color}">{text}</span>')
        else:
            self.log_view.appendPlainText(text)

    def _on_trade(self, t: Trade) -> None:
        arrow = "▲" if t.side is Side.BUY else "▼"
        color = COLOR_GREEN if t.side is Side.BUY else COLOR_RED
        pnl = f"  pnl={t.pnl:,.0f}" if t.pnl else ""
        self._append_log(
            f"{time.strftime('%H:%M:%S')} {arrow} {t.side.value.upper()} "
            f"{t.qty}x {t.item} @ {t.price:,}{pnl} [{t.mode}]", color)
        self._trades.append(t)

    def _on_opportunity(self, opp: dict) -> None:
        self._append_log(
            f"opportunity: {opp['item']} edge={opp['edge_pct']:.0%} "
            f"conf={opp['confidence']:.0%}", "#8ab4ff")

    def _on_halt(self, payload: dict) -> None:
        self._append_log(f"⚠ RISK HALT: {payload.get('reason', '')}",
                         COLOR_RED)

    def _on_alert(self, payload: dict) -> None:
        self._append_log(f"alert: {payload.get('message', '')}", "#ffd479")

    def _on_equity(self, payload: dict) -> None:
        self.stat_labels["Equity"].setText(_fmt(payload.get("total", 0)))
        self.stat_labels["Coins"].setText(_fmt(payload.get("coins", 0)))

    def _on_positions(self, positions: list) -> None:
        self._positions = positions

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        acct = self.rt.engine.account
        stats = self.rt.db.stats_summary()
        wins = stats.get("wins") or 0
        losses = stats.get("losses") or 0
        total = wins + losses
        wr = f"{wins / total:.0%}" if total else "—"
        self.stat_labels["Open positions"].setText(str(len(self._positions)))
        self.stat_labels["Win rate"].setText(wr)
        self.stat_labels["Realized P/L"].setText(
            _fmt(self.rt.db.realized_pnl(acct)))
        self.stat_labels["Status"].setText(
            "HALTED" if self.rt.risk.state.halted
            else ("Paused" if not self.rt.engine.enabled else "Trading"))
        color = COLOR_RED if self.rt.risk.state.halted else COLOR_GREEN
        self.stat_labels["Status"].setStyleSheet(
            f"font-size:20px; font-weight:600; color:{color};")


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
class WatchlistPanel(QtWidgets.QWidget):
    headers = ("Item", "Fair", "Conf", "Trend", "Bid", "Ask", "Edge",
               "Auto-trade")

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        layout = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.item_input = QtWidgets.QLineEdit()
        self.item_input.setPlaceholderText("add item to watchlist...")
        add_btn = QtWidgets.QPushButton("Add")
        rm_btn = QtWidgets.QPushButton("Remove selected")
        bar.addWidget(self.item_input, 1)
        bar.addWidget(add_btn)
        bar.addWidget(rm_btn)
        layout.addLayout(bar)

        self.table = QtWidgets.QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        add_btn.clicked.connect(self._add)
        rm_btn.clicked.connect(self._remove)
        self.item_input.returnPressed.connect(self._add)

        self._load_saved()
        refresh = QtCore.QTimer(self)
        refresh.setInterval(1500)
        refresh.timeout.connect(self.refresh)
        refresh.start()

    # ------------------------------------------------------------------
    def _load_saved(self) -> None:
        raw = self.rt.db.get_setting("watchlist", "")
        for item in [x for x in raw.split(",") if x]:
            self._add_item_row(item)

    def _save(self) -> None:
        items = [self.table.item(r, 0).text()
                 for r in range(self.table.rowCount())]
        self.rt.db.set_setting("watchlist", ",".join(items))

    def _add(self) -> None:
        text = self.item_input.text().strip().lower().replace(" ", "_")
        if text:
            self._add_item_row(text)
            self.item_input.clear()
            self._save()

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._save()

    def _add_item_row(self, item: str) -> None:
        if any(self.table.item(r, 0) and self.table.item(r, 0).text() == item
               for r in range(self.table.rowCount())):
            return
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(item))
        check = QtWidgets.QCheckBox()
        check.setChecked(True)
        self.table.setCellWidget(r, 7, check)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0).text()
            est = self.rt.prices.get_estimate(item)
            ev = self.rt.engine.evaluate(item)
            bid, ask = ev["bid"], ev["ask"]
            edge = ev["opportunity"]["edge_pct"] if ev["opportunity"] else None

            def setv(col, text, color=None):
                it = QtWidgets.QTableWidgetItem(text)
                if color:
                    it.setForeground(QtGui.QColor(color))
                self.table.setItem(r, col, it)

            setv(1, _fmt(est.fair) if est.fair == est.fair else "—")
            setv(2, f"{est.confidence:.0%}")
            setv(3, f"{est.trend:+.1%}" if est.trend else "—",
                 COLOR_GREEN if est.trend > 0 else
                 (COLOR_RED if est.trend < 0 else None))
            setv(4, _fmt(bid) if bid else "—")
            setv(5, _fmt(ask) if ask else "—")
            setv(6, f"{edge:+.1%}" if edge is not None else "—",
                 COLOR_GREEN if (edge or 0) > 0 else
                 (COLOR_RED if (edge or 0) < 0 else None))
            # auto-trade toggle drives engine.enabled per-row
            chk = self.table.cellWidget(r, 7)
            if chk is not None and not chk.isChecked():
                # dim non-auto rows (engine-level toggle is global in this build)
                self.table.item(r, 0).setForeground(
                    QtGui.QColor("#666666"))


# ---------------------------------------------------------------------------
# Charts (lightweight QPainter — no matplotlib dependency)
# ---------------------------------------------------------------------------
class ChartWidget(QtWidgets.QWidget):
    """Simple line chart drawn with QPainter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.series: list[tuple[float, float]] = []
        self.color = QtGui.QColor("#4f8cff")

    def set_series(self, pts: list[tuple[float, float]], color: str | None = None) -> None:
        self.series = pts or []
        if color:
            self.color = QtGui.QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#161b22"))
        if len(self.series) < 2:
            painter.setPen(QtGui.QColor("#8b949e"))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                             "no data yet")
            return
        xs = [p[0] for p in self.series]
        ys = [p[1] for p in self.series]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if max_x == min_x:
            max_x = min_x + 1
        pad = (max_y - min_y) * 0.1 or max_y * 0.05 or 1
        min_y, max_y = min_y - pad, max_y + pad
        w, h = self.width(), self.height()

        def tx(x):  # noqa: ANN001
            return int((x - min_x) / (max_x - min_x) * (w - 20)) + 10

        def ty(y):  # noqa: ANN001
            return int(h - 10 - (y - min_y) / (max_y - min_y) * (h - 20))

        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(self.color, 2))
        for i in range(1, len(self.series)):
            painter.drawLine(tx(xs[i - 1]), ty(ys[i - 1]), tx(xs[i]), ty(ys[i]))
        painter.setPen(QtGui.QColor("#8b949e"))
        painter.drawText(10, 16, f"min {min(ys):,.0f}   max {max(ys):,.0f}")


class ChartsPanel(QtWidgets.QWidget):
    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        layout = QtWidgets.QVBoxLayout(self)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Item:"))
        self.item_combo = QtWidgets.QComboBox()
        self.item_combo.setEditable(True)
        refresh_btn = QtWidgets.QPushButton("Refresh")
        row.addWidget(self.item_combo, 1)
        row.addWidget(refresh_btn)
        layout.addLayout(row)

        self.price_chart = ChartWidget()
        self.equity_chart = ChartWidget()
        self.equity_chart.color = QtGui.QColor("#2ea043")
        layout.addWidget(QtWidgets.QLabel("Price history"))
        layout.addWidget(self.price_chart, 1)
        layout.addWidget(QtWidgets.QLabel("Portfolio value"))
        layout.addWidget(self.equity_chart, 1)

        refresh_btn.clicked.connect(self.refresh)
        refresh = QtCore.QTimer(self)
        refresh.setInterval(5000)
        refresh.timeout.connect(self.refresh)
        refresh.start()

    def refresh(self) -> None:
        items = self.rt.prices.known_items()
        current = self.item_combo.currentText()
        if items:
            self.item_combo.blockSignals(True)
            self.item_combo.clear()
            self.item_combo.addItems(items)
            if current in items:
                self.item_combo.setCurrentText(current)
            self.item_combo.blockSignals(False)
        item = self.item_combo.currentText() or (items[0] if items else "")
        if item:
            est = self.rt.prices.get_estimate(item)
            self.price_chart.set_series(est.history)
        acct = self.rt.engine.account
        self.equity_chart.set_series(
            self.rt.db.equity_history(acct, since_ts=time.time() - 24 * 3600))


# ---------------------------------------------------------------------------
# Trade history
# ---------------------------------------------------------------------------
class HistoryPanel(QtWidgets.QWidget):
    headers = ("Time", "Account", "Item", "Side", "Qty", "Price", "Fees",
               "P/L", "Mode", "Note")

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        layout = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("search trades...")
        reload_btn = QtWidgets.QPushButton("Reload")
        bar.addWidget(self.search, 1)
        bar.addWidget(reload_btn)
        layout.addLayout(bar)

        self.table = QtWidgets.QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        reload_btn.clicked.connect(self.reload)
        self.search.textChanged.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        q = self.search.text().lower()
        rows = self.rt.db.trades(limit=500)
        self.table.setRowCount(0)
        for row in rows:
            text = " ".join(str(row[k]) for k in
                            ("ts", "account", "item", "side", "mode", "note"))
            if q and q not in text.lower():
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = (time.strftime("%m-%d %H:%M", time.localtime(row["ts"])),
                    row["account"], row["item"], row["side"], str(row["qty"]),
                    _fmt(row["price"]), _fmt(row["fees"]), _fmt(row["pnl"]),
                    row["mode"], row["note"])
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(str(v))
                if row["side"] == "buy":
                    it.setForeground(QtGui.QColor(COLOR_GREEN))
                elif row["side"] == "sell":
                    it.setForeground(QtGui.QColor(COLOR_RED))
                self.table.setItem(r, c, it)


# ---------------------------------------------------------------------------
# Configuration panel
# ---------------------------------------------------------------------------
class ConfigPanel(QtWidgets.QWidget):
    FIELDS: list[tuple[str, str, str, float]] = [
        # (label, section, key, default)
        ("Profit threshold", "trading", "profit_threshold", 0.06),
        ("Min confidence", "trading", "min_confidence", 0.55),
        ("Max trade (coins)", "trading", "max_trade_coins", 50000),
        ("Max trade (% of bank)", "trading", "max_trade_pct", 0.20),
        ("Item exposure cap", "trading", "max_item_exposure_pct", 0.35),
        ("Cooldown (s)", "trading", "cooldown_sec", 600),
        ("Auto-sell target", "trading", "auto_sell_target", 0.02),
        ("Stop loss", "trading", "stop_loss", 0.15),
        ("TWAP enabled", "trading", "twap_enabled", 1),
        ("TWAP slices", "trading", "twap_slices", 5),
        ("Auto-buy tools", "trading", "auto_buy_tools", 1),
        ("Est. half-life (h)", "estimator", "half_life_hours", 12),
        ("IQR k", "estimator", "iqr_k", 1.5),
        ("Confidence volume", "estimator", "confidence_volume", 40),
        ("Target cash %", "inventory", "target_cash_pct", 0.35),
        ("Skew tolerance", "inventory", "skew_tolerance", 0.15),
        ("Max consec. losses", "risk", "max_consecutive_losses", 4),
        ("Max drawdown", "risk", "max_portfolio_drop_pct", 0.20),
    ]

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        form = QtWidgets.QFormLayout(self)
        self._editors: dict[str, QtWidgets.QWidget] = {}

        for label, section, key, default in self.FIELDS:
            editor: QtWidgets.QWidget
            if isinstance(default, int) and default in (0, 1):
                editor = QtWidgets.QCheckBox()
                current = getattr(getattr(runtime.cfg, section), key)
                editor.setChecked(bool(current))
            else:
                editor = QtWidgets.QLineEdit(str(
                    getattr(getattr(runtime.cfg, section), key)))
            form.addRow(label, editor)
            self._editors[f"{section}.{key}"] = editor

        save_btn = QtWidgets.QPushButton("Save configuration")
        save_btn.clicked.connect(self.save)
        form.addRow(save_btn)

        info = QtWidgets.QLabel(
            "Values are applied at runtime and persisted to the database; "
            "config.yaml remains the base file.")
        info.setWordWrap(True)
        form.addRow(info)

    def save(self) -> None:
        import json

        for dotted, editor in self._editors.items():
            section, key = dotted.split(".")
            obj = getattr(self.rt.cfg, section)
            if isinstance(editor, QtWidgets.QCheckBox):
                setattr(obj, key, editor.isChecked())
            else:
                raw = editor.text().strip()
                cur = getattr(obj, key)
                try:
                    setattr(obj, key, type(cur)(raw))
                except (ValueError, TypeError):
                    pass
            self.rt.db.set_setting(
                f"cfg.{dotted}",
                json.dumps(getattr(obj, key), default=str))
        QtWidgets.QMessageBox.information(
            self, "Saved", "Configuration applied and persisted.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class AccountsPanel(QtWidgets.QWidget):
    headers = ("Name", "Token env var", "Token present", "Mode")

    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        layout = QtWidgets.QVBoxLayout(self)

        self.table = QtWidgets.QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        bar = QtWidgets.QHBoxLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("account name")
        self.env_edit = QtWidgets.QLineEdit()
        self.env_edit.setPlaceholderText("token env var (e.g. DISCORD_TOKEN)")
        add_btn = QtWidgets.QPushButton("Add account")
        bar.addWidget(self.name_edit)
        bar.addWidget(self.env_edit)
        bar.addWidget(add_btn)
        layout.addLayout(bar)

        add_btn.clicked.connect(self._add)
        self.reload()

    def reload(self) -> None:
        self.table.setRowCount(0)
        for acct in self.rt.cfg.accounts:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(acct.name))
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(acct.token_env))
            self.table.setItem(r, 2, QtWidgets.QTableWidgetItem(
                "yes" if acct.token else "NO — set env var"))
            self.table.setItem(r, 3, QtWidgets.QTableWidgetItem(
                self.rt.cfg.mode))

    def _add(self) -> None:
        name = self.name_edit.text().strip()
        env = self.env_edit.text().strip() or "DISCORD_TOKEN"
        if not name:
            return
        from dankflipper.config import AccountCfg
        self.rt.cfg.accounts.append(AccountCfg(name=name, token_env=env))
        self.reload()


# ---------------------------------------------------------------------------
# Manual controls
# ---------------------------------------------------------------------------
class ManualPanel(QtWidgets.QWidget):
    def __init__(self, runtime, parent=None) -> None:
        super().__init__(parent)
        self.rt = runtime
        form = QtWidgets.QFormLayout(self)

        self.pause_btn = QtWidgets.QPushButton("Pause trading")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)

        panic_btn = QtWidgets.QPushButton("⚠ PANIC SELL ALL")
        panic_btn.setStyleSheet("color:#fff; background:#b3261e; font-weight:bold;")
        panic_btn.clicked.connect(self._panic)

        self.twap_item = QtWidgets.QLineEdit()
        self.twap_item.setPlaceholderText("item name")
        twap_btn = QtWidgets.QPushButton("TWAP sell")
        twap_btn.clicked.connect(self._twap)

        self.stress_btn = QtWidgets.QPushButton("Run stress test")
        self.stress_btn.clicked.connect(self._stress)
        self.bt_btn = QtWidgets.QPushButton("Run backtest")
        self.bt_btn.clicked.connect(self._backtest)

        self.output = QtWidgets.QPlainTextEdit(readOnly=True)
        self.output.setPlaceholderText("tool output...")

        form.addRow("Trading toggle:", self.pause_btn)
        form.addRow(panic_btn)
        form.addRow("TWAP item:", self.twap_item)
        form.addRow(twap_btn)
        form.addRow(self.stress_btn)
        form.addRow(self.bt_btn)
        form.addRow(self.output)

    def _toggle_pause(self, checked: bool) -> None:
        self.rt.engine.enabled = not checked
        self.pause_btn.setText("Resume trading" if checked else "Pause trading")
        self.rt.bus.publish(Topics.LOG,
                            "trading paused" if checked else "trading resumed")

    def _panic(self) -> None:
        confirm = QtWidgets.QMessageBox.question(
            self, "Confirm panic sell",
            "Sell EVERYTHING at the best available bid?")
        if confirm == QtWidgets.QMessageBox.StandardButton.Yes:
            asyncio.ensure_future(self.rt.engine.flash_sell_all())

    def _twap(self) -> None:
        item = self.twap_item.text().strip().lower().replace(" ", "_")
        if item:
            asyncio.ensure_future(self.rt.engine.twap_sell(item))

    def _stress(self) -> None:
        acct = self.rt.engine.account
        inv = self.rt.executor.inv(acct)
        result = self.rt.risk.stress_test(
            self.rt.db.positions(acct), inv.coins,
            lambda i: self.rt.prices.get_fair_price(i, refresh=False))
        lines = [f"{k}: {v:,.0f}" for k, v in result.items()]
        self.output.setPlainText("Stress scenarios:\n" + "\n".join(lines))

    def _backtest(self) -> None:
        result = self.rt.run_backtest()
        self.output.setPlainText(
            f"Backtest: return {result.return_pct:+.2f}% "
            f"(benchmark {result.benchmark_return_pct:+.2f}%), "
            f"{result.n_trades} trades, win rate {result.win_rate:.0%}")
