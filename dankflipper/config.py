"""Configuration loading: defaults <- config.yaml <- environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()  # no-op if .env missing


@dataclass(slots=True)
class EstimatorCfg:
    half_life_hours: float = 12.0
    min_observations: int = 4
    iqr_k: float = 1.5
    confidence_volume: int = 40


@dataclass(slots=True)
class TradingCfg:
    profit_threshold: float = 0.06
    min_confidence: float = 0.55
    max_trade_coins: int = 50_000
    max_trade_pct: float = 0.20
    max_item_exposure_pct: float = 0.35
    cooldown_sec: float = 600.0
    twap_enabled: bool = True
    twap_slices: int = 5
    twap_interval_sec: float = 90.0
    auto_sell_target: float = 0.02
    stop_loss: float = 0.15
    auto_buy_tools: bool = True


@dataclass(slots=True)
class RiskCfg:
    max_consecutive_losses: int = 4
    max_portfolio_drop_pct: float = 0.20
    equity_check_interval_sec: float = 60.0


@dataclass(slots=True)
class InventoryCfg:
    target_cash_pct: float = 0.35
    skew_tolerance: float = 0.15
    skew_check_interval_sec: float = 90.0
    essential_tools: list[str] = field(
        default_factory=lambda: ["shovel", "fishing_pole", "rifle"]
    )
    tool_rebuy_cooldown_sec: float = 900.0


@dataclass(slots=True)
class BriefingCfg:
    enabled: bool = True
    hour_utc: int = 12


@dataclass(slots=True)
class AlertsCfg:
    min_profit_alert: float = 0.25


@dataclass(slots=True)
class AccountCfg:
    name: str = "main"
    token_env: str = "DISCORD_TOKEN"

    @property
    def token(self) -> str:
        return os.getenv(self.token_env, "").strip()


@dataclass(slots=True)
class Config:
    mode: str = "paper"                      # paper | live
    accounts: list[AccountCfg] = field(default_factory=lambda: [AccountCfg()])
    market_source: str = "mock"              # mock | discord | both
    channel_ids: list[int] = field(default_factory=list)
    scan_interval_sec: float = 5.0
    backfill: bool = True
    estimator: EstimatorCfg = field(default_factory=EstimatorCfg)
    trading: TradingCfg = field(default_factory=TradingCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    inventory: InventoryCfg = field(default_factory=InventoryCfg)
    briefing: BriefingCfg = field(default_factory=BriefingCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    db_path: str = "data/dankflipper.db"
    dankalert_enabled: bool = False
    dankalert_base_url: str = ""
    dankalert_api_key: str = ""
    daily_channel_id: Optional[int] = None

    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    """Build a Config from defaults + yaml file + DANKFLIP_* env overrides."""
    cfg = Config()
    path = Path(path or os.getenv("DANKFLIP_CONFIG", "config.yaml"))
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        accounts = data.pop("accounts", None)
        _apply(cfg, data)
        if accounts:
            cfg.accounts = [
                AccountCfg(
                    name=str(a.get("name", f"acct{i}")),
                    token_env=str(a.get("token_env", "DISCORD_TOKEN")),
                )
                for i, a in enumerate(accounts)
            ]
    else:
        cfg.accounts = [AccountCfg()]

    # Environment overrides
    if src := os.getenv("DANKFLIP_MODE"):
        cfg.mode = src
    if src := os.getenv("DANKFLIP_SOURCE"):
        cfg.market_source = src
    if channels := os.getenv("MARKET_CHANNEL_IDS"):
        cfg.channel_ids = [int(c) for c in channels.replace(" ", "").split(",") if c]
    if ch := os.getenv("DAILY_BRIEFING_CHANNEL_ID"):
        cfg.daily_channel_id = int(ch)
    if db := os.getenv("DANKFLIP_DB"):
        cfg.db_path = db
    cfg.dankalert_enabled = (
        os.getenv("DANKALERT_ENABLED", "").lower() in {"1", "true", "yes"}
    )
    cfg.dankalert_base_url = os.getenv("DANKALERT_BASE_URL", "")
    cfg.dankalert_api_key = os.getenv("DANKALERT_API_KEY", "")
    return cfg
