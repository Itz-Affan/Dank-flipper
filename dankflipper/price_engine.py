"""Price estimation: robust fair price with time-decay weighting.

For each item we keep observations of *offers*:
  - BUY offers  (someone wants to buy)  -> evidence of liquidation price (bid)
  - SELL offers (someone wants to sell) -> evidence of acquisition price (ask)

The fair price is a recency-weighted median of the blended sample, giving
bids a small extra weight because bids are what we can actually exit into.
Outliers are removed with the IQR rule before weighting. Confidence scales
with the number of fresh observations; trend compares two recency windows.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .config import EstimatorCfg
from .database import Database
from .models import Side

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Estimate:
    item: str
    fair: float
    confidence: float                 # 0..1
    trend: float                      # pct change recent vs older window
    n_obs: int = 0
    bid: Optional[float] = None       # best bid evidence (p70 of buy offers)
    ask: Optional[float] = None       # best ask evidence (p30 of sell offers)
    updated_ts: float = 0.0
    history: list[tuple[float, float]] = field(default_factory=list)


class PriceEngine:
    """Robust price estimator over observations stored in the Database."""

    def __init__(self, db: Database, cfg: EstimatorCfg) -> None:
        self.db = db
        self.cfg = cfg
        self._cache: dict[str, Estimate] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _weights(self, ages_s: np.ndarray) -> np.ndarray:
        """Exponential time-decay weights."""
        half_life = max(self.cfg.half_life_hours * 3600.0, 60.0)
        return np.power(0.5, ages_s / half_life)

    def _filter_outliers_iqr(self, prices: np.ndarray) -> np.ndarray:
        """Two-stage outlier removal.

        Stage 1 (gross cull): MAD-based fence so that a heavily contaminated
        sample cannot drag the quartiles toward the outliers (classic IQR
        failure mode when >25% of points are garbage).
        Stage 2: standard IQR fences, with a guard that never discards more
        than ~40% of the sample.
        """
        if prices.size < 8:
            return prices

        med = float(np.median(prices))
        mad = float(np.median(np.abs(prices - med)))
        if mad > 0:
            keep = np.abs(prices - med) <= 6 * 1.4826 * mad  # ~4 sigma
        else:
            # zero dispersion: keep anything within 15% of the median
            keep = np.abs(prices - med) <= 0.15 * max(abs(med), 1.0)
        if keep.sum() >= max(4, prices.size // 3):
            prices = prices[keep]

        q1, q3 = np.percentile(prices, [25, 75])
        iqr = q3 - q1
        if iqr <= 0:
            return prices
        lo, hi = q1 - self.cfg.iqr_k * iqr, q3 + self.cfg.iqr_k * iqr
        filtered = prices[(prices >= lo) & (prices <= hi)]
        if filtered.size >= max(4, int(prices.size * 0.6)):
            return filtered
        return prices

    def _weighted_median(self, values: np.ndarray, weights: np.ndarray) -> float:
        """Weighted median: smallest value v with cumulative weight >= half
        of the total weight (the standard discrete definition; conservative
        because it biases the fair estimate slightly low)."""
        if values.size == 0:
            return math.nan
        order = np.argsort(values)
        v, w = values[order], weights[order]
        cw = np.cumsum(w)
        cutoff = 0.5 * cw[-1]
        return float(v[int(np.searchsorted(cw, cutoff))])

    # ------------------------------------------------------------------
    def compute(self, item: str, window_s: float = 7 * 24 * 3600) -> Estimate:
        rows = self.db.observations(item, max_age_s=window_s)
        est = Estimate(item=item, fair=math.nan, confidence=0.0, trend=0.0,
                       updated_ts=time.time())

        if not rows:
            with self._lock:
                self._cache[item] = est
            return est

        now = est.updated_ts
        prices = np.array([r["price"] for r in rows], dtype=float)
        ages = now - np.array([r["ts"] for r in rows], dtype=float)
        sides = [str(r["side"]) for r in rows]

        prices = self._filter_outliers_iqr(prices)
        if prices.size == 0:  # extreme case: everything filtered
            prices = np.array([r["price"] for r in rows], dtype=float)
            ages = now - np.array([r["ts"] for r in rows], dtype=float)

        # map filtered prices back onto observations
        keep_prices = set(prices.tolist())
        keep = np.array([r["price"] in keep_prices for r in rows], dtype=bool)
        ages = ages[keep]
        sides = [s for s, k in zip(sides, keep) if k]

        weights = self._weights(ages)
        # bids are more valuable evidence for *our* exit; nudge their weight
        weights = np.where([s == str(Side.BUY) for s in sides], weights * 1.15, weights)

        fair = self._weighted_median(prices, weights)

        # confidence: volume based, softened by price dispersion
        n = self.db.count_observations(item, max_age_s=window_s)
        vol_conf = min(1.0, n / max(self.cfg.confidence_volume, 1))
        if n >= 4 and float(np.mean(prices)) > 0:
            cv = float(np.std(prices) / np.mean(prices))
            disp_conf = 1.0 / (1.0 + cv)
        else:
            disp_conf = 0.5
        est.confidence = round(min(vol_conf, disp_conf), 3)

        # trend: median of the most recent 20% of the window vs the rest
        if n >= 6:
            recent_mask = ages <= window_s * 0.2
            recent, older = prices[recent_mask], prices[~recent_mask]
            if recent.size >= 2 and older.size >= 2:
                rm, om = float(np.median(recent)), float(np.median(older))
                if om > 0:
                    est.trend = round((rm - om) / om, 4)

        # bid/ask evidence from offer sides
        buy_prices = np.array([r["price"] for r in rows
                               if str(r["side"]) == str(Side.BUY)], dtype=float)
        sell_prices = np.array([r["price"] for r in rows
                                if str(r["side"]) == str(Side.SELL)], dtype=float)
        if buy_prices.size:
            est.bid = float(np.percentile(buy_prices, 70))
        if sell_prices.size:
            est.ask = float(np.percentile(sell_prices, 30))

        est.n_obs = n
        est.fair = fair
        est.history = [
            (float(r["ts"]), float(r["price"])) for r in rows[-250:]
        ]
        with self._lock:
            self._cache[item] = est
        return est

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    _CACHE_TTL_S = 3.0  # cached estimates older than this are recomputed

    def _fresh(self, item: str) -> Estimate:
        est = self._cache.get(item)
        if est is None or (time.time() - est.updated_ts) > self._CACHE_TTL_S:
            return self.compute(item)
        return est

    def get_fair_price(self, item: str, refresh: bool = True) -> float:
        if refresh:
            return self.compute(item).fair
        if item not in self._cache:
            self.compute(item)
        return self._cache[item].fair

    def get_confidence(self, item: str) -> float:
        return self._fresh(item).confidence

    def get_price_trend(self, item: str) -> float:
        return self._fresh(item).trend

    def get_estimate(self, item: str, refresh: bool = True) -> Estimate:
        if refresh:
            return self._fresh(item)
        if item not in self._cache:
            self.compute(item)
        return self._cache[item]

    def known_items(self) -> list[str]:
        return self.db.items(max_age_s=7 * 24 * 3600)

    def all_estimates(self) -> dict[str, Estimate]:
        return {item: self.get_estimate(item) for item in self.known_items()}
