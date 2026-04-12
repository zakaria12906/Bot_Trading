"""Average True Range — used for adaptive grid step and volatility filtering."""

from __future__ import annotations
from typing import List
from broker.base_broker import Bar


def true_range(bars: List[Bar]) -> List[float]:
    """Return the true-range series (length = len(bars) - 1)."""
    tr: List[float] = []
    for i in range(1, len(bars)):
        hl = bars[i].high - bars[i].low
        hc = abs(bars[i].high - bars[i - 1].close)
        lc = abs(bars[i].low - bars[i - 1].close)
        tr.append(max(hl, hc, lc))
    return tr


def atr(bars: List[Bar], period: int = 14) -> float:
    """Wilder-smoothed ATR of the most recent *period* bars.

    Returns 0.0 if not enough data.
    """
    tr = true_range(bars)
    if len(tr) < period:
        return 0.0

    # Seed with SMA of the first *period* true ranges
    value = sum(tr[:period]) / period
    for t in tr[period:]:
        value = (value * (period - 1) + t) / period
    return value


def atr_series(bars: List[Bar], period: int = 14) -> List[float]:
    """Full ATR series aligned with *bars[period:]*.  Useful for baseline
    comparisons (current ATR vs. median ATR over N bars)."""
    tr = true_range(bars)
    if len(tr) < period:
        return []
    series: List[float] = []
    value = sum(tr[:period]) / period
    series.append(value)
    for t in tr[period:]:
        value = (value * (period - 1) + t) / period
        series.append(value)
    return series
