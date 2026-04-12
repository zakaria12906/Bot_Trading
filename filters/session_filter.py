"""Session filter — ensures the bot only trades during its configured window."""

from __future__ import annotations

import logging
from datetime import datetime

from utils.helpers import server_time_in_window

log = logging.getLogger("grid_bot.session")


class SessionFilter:

    def __init__(self, sym_cfg: dict):
        self.start = sym_cfg.get("session_start", "00:00")
        self.end = sym_cfg.get("session_end", "23:59")

    def is_in_session(self, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        ok = server_time_in_window(self.start, self.end, now)
        if not ok:
            log.debug("Outside session window %s–%s", self.start, self.end)
        return ok
