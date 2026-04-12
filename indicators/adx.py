"""Average Directional Index — measures trend strength.

Used by the regime filter to block entries during strong directional
expansion (ADX above a configurable ceiling).
"""

from __future__ import annotations
from typing import List
from broker.base_broker import Bar


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    result = [sum(values[:period])]
    for v in values[period:]:
        result.append(result[-1] - result[-1] / period + v)
    return result


def adx(bars: List[Bar], period: int = 14) -> float:
    """Return the latest ADX value.  Returns 0.0 if insufficient data."""
    if len(bars) < period * 2 + 1:
        return 0.0

    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr_list: List[float] = []

    for i in range(1, len(bars)):
        high_diff = bars[i].high - bars[i - 1].high
        low_diff = bars[i - 1].low - bars[i].low

        plus_dm.append(max(high_diff, 0.0) if high_diff > low_diff else 0.0)
        minus_dm.append(max(low_diff, 0.0) if low_diff > high_diff else 0.0)

        hl = bars[i].high - bars[i].low
        hc = abs(bars[i].high - bars[i - 1].close)
        lc = abs(bars[i].low - bars[i - 1].close)
        tr_list.append(max(hl, hc, lc))

    sm_plus = _wilder_smooth(plus_dm, period)
    sm_minus = _wilder_smooth(minus_dm, period)
    sm_tr = _wilder_smooth(tr_list, period)

    min_len = min(len(sm_plus), len(sm_minus), len(sm_tr))
    if min_len == 0:
        return 0.0

    dx_list: List[float] = []
    for i in range(min_len):
        if sm_tr[i] == 0:
            dx_list.append(0.0)
            continue
        plus_di = 100.0 * sm_plus[i] / sm_tr[i]
        minus_di = 100.0 * sm_minus[i] / sm_tr[i]
        di_sum = plus_di + minus_di
        dx_list.append(100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

    if len(dx_list) < period:
        return dx_list[-1] if dx_list else 0.0

    adx_val = sum(dx_list[:period]) / period
    for d in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + d) / period
    return adx_val
