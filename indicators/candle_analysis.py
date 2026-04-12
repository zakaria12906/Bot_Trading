"""Candle-structure helpers for breakout / impulse detection."""

from __future__ import annotations
from typing import List
from broker.base_broker import Bar


def is_impulse_candle(bar: Bar, atr_value: float, range_mult: float = 1.8) -> bool:
    """A candle is considered an impulse candle when its range exceeds
    ATR * range_mult AND it closes near its extreme (body > 60 % of range)."""
    bar_range = bar.high - bar.low
    if atr_value <= 0 or bar_range <= 0:
        return False
    if bar_range < atr_value * range_mult:
        return False
    body = abs(bar.close - bar.open)
    return body / bar_range > 0.60


def consecutive_impulse_candles(
    bars: List[Bar], atr_value: float, range_mult: float = 1.8,
) -> int:
    """Count how many of the *most recent* bars are same-direction impulse
    candles in a row (looking backwards from the last bar)."""
    if len(bars) < 2:
        return 0

    direction = None
    count = 0
    for bar in reversed(bars):
        if not is_impulse_candle(bar, atr_value, range_mult):
            break
        candle_dir = 1 if bar.close > bar.open else -1
        if direction is None:
            direction = candle_dir
        elif candle_dir != direction:
            break
        count += 1
    return count


def bars_show_overlap(bars: List[Bar], lookback: int = 5) -> bool:
    """Return True if recent candles show price overlap — a proxy for
    range-bound / mean-reverting structure rather than clean expansion."""
    recent = bars[-lookback:] if len(bars) >= lookback else bars
    if len(recent) < 3:
        return True
    overlap_count = 0
    for i in range(1, len(recent)):
        prev_range = (recent[i - 1].low, recent[i - 1].high)
        curr_range = (recent[i].low, recent[i].high)
        if curr_range[0] <= prev_range[1] and curr_range[1] >= prev_range[0]:
            overlap_count += 1
    return overlap_count >= len(recent) // 2
