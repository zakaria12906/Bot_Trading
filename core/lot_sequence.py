"""Lot-size progression for the hedged grid.

Reverse-engineered from live bot screenshots (XAUUSDs, 2026-04-13):

    Level:  0     1     2     3     4     5     6     7     8
    Lot:    0.01  0.01  0.02  0.03  0.05  0.07  0.11  0.17  0.25

The HEDGE side always stays at base_lot (0.01).
The RECOVERY side follows the scaling sequence.

The default sequence is hardcoded to match the observed bot exactly.
A custom sequence can be provided via config if desired.
"""

from __future__ import annotations

import math
from typing import List

# Exact sequence observed in the live bot (in units of base_lot)
_DEFAULT_MULTIPLIERS = [1, 1, 2, 3, 5, 7, 11, 17, 25]


def build_lot_sequence(
    base_lot: float = 0.01,
    max_levels: int = 9,
    custom_multipliers: List[int] = None,
) -> List[float]:
    """Return the lot for each grid level.

    If custom_multipliers is None, uses the exact sequence from the live bot.
    Otherwise, multipliers[i] × base_lot gives the lot at level i.
    """
    mults = custom_multipliers or _DEFAULT_MULTIPLIERS

    seq: List[float] = []
    for i in range(max_levels):
        if i < len(mults):
            lot = round(base_lot * mults[i], 2)
        else:
            # Beyond the table: continue with ~1.5× the previous
            prev = seq[-1] if seq else base_lot
            lot = round(math.ceil(prev * 1.5 * 100) / 100, 2)
        seq.append(max(lot, base_lot))

    return seq


def build_lot_sequence_formula(
    base_lot: float = 0.01,
    max_levels: int = 9,
    multiplier: float = 1.5,
) -> List[float]:
    """Alternative: compute lots using a ×1.5 multiplier per level.

    Slightly different from the observed bot but useful for experimentation.
    """
    seq: List[float] = []
    cur = base_lot
    for i in range(max_levels):
        if i <= 1:
            seq.append(base_lot)
            cur = base_lot
        else:
            cur = cur * multiplier
            rounded = math.ceil(cur * 100) / 100
            seq.append(max(rounded, base_lot))
            cur = rounded
    return seq


def total_exposure(sequence: List[float]) -> float:
    return round(sum(sequence), 2)
