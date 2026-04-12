from datetime import datetime, time as dtime


def server_time_in_window(start_str: str, end_str: str, now: datetime) -> bool:
    """Return True if *now* falls inside the [start, end) session window."""
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    t = now.time()
    if start <= end:
        return start <= t < end
    # overnight window (e.g. 22:00 → 06:00)
    return t >= start or t < end


TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}


def tf_to_mt5(label: str) -> int:
    """Convert a human-readable timeframe label to the MT5 integer constant."""
    return TIMEFRAME_MAP.get(label.upper(), 15)


def points_to_price(points: float, symbol_info) -> float:
    """Convert a point value to a price delta using the symbol's point size."""
    return points * symbol_info.point
