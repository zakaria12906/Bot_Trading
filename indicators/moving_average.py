"""Moving averages used for directional bias and mean-reversion detection."""

from __future__ import annotations
from typing import List
from broker.base_broker import Bar


def sma(bars: List[Bar], period: int) -> float:
    """Simple moving average of the last *period* closes."""
    if len(bars) < period:
        return 0.0
    return sum(b.close for b in bars[-period:]) / period


def ema(bars: List[Bar], period: int) -> float:
    """Exponential moving average of closes.  Returns 0.0 if insufficient data."""
    if len(bars) < period:
        return 0.0
    k = 2.0 / (period + 1)
    value = sum(b.close for b in bars[:period]) / period
    for b in bars[period:]:
        value = b.close * k + value * (1 - k)
    return value


def distance_from_ma(bars: List[Bar], period: int, atr_value: float) -> float:
    """Normalized distance between the last close and its SMA, expressed
    in ATR multiples.  Positive = price above MA."""
    ma = sma(bars, period)
    if ma == 0.0 or atr_value == 0.0:
        return 0.0
    return (bars[-1].close - ma) / atr_value
