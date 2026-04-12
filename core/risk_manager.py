"""Two-mode risk controller: Normal → Defensive → Shutdown.

This is the single most important safety layer.  It aggregates signals from
every filter and decides what the bot is *allowed* to do on each tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from broker.base_broker import BaseBroker
from filters.regime_filter import RegimeFilter, Regime
from filters.volatility_filter import VolatilityFilter
from filters.spread_filter import SpreadFilter
from filters.session_filter import SessionFilter
from filters.news_filter import NewsFilter
from filters.breakout_detector import BreakoutDetector

log = logging.getLogger("grid_bot.risk")


class RiskMode(Enum):
    NORMAL = "normal"
    DEFENSIVE = "defensive"
    SHUTDOWN = "shutdown"


class RiskManager:
    """Determines the current operating mode for a given symbol and tracks
    account-level limits (daily loss, equity stop)."""

    def __init__(
        self,
        broker: BaseBroker,
        risk_cfg: dict,
        sym_cfg: dict,
        regime_filter: RegimeFilter,
        volatility_filter: VolatilityFilter,
        spread_filter: SpreadFilter,
        session_filter: SessionFilter,
        news_filter: NewsFilter,
        breakout_detector: BreakoutDetector,
    ):
        self.broker = broker
        self.risk_cfg = risk_cfg
        self.sym_cfg = sym_cfg
        self.regime = regime_filter
        self.volatility = volatility_filter
        self.spread = spread_filter
        self.session = session_filter
        self.news = news_filter
        self.breakout = breakout_detector

        self._daily_start_balance: float = broker.account_balance()
        self._daily_losing_baskets: int = 0
        self._last_daily_reset: str = ""

    # ------------------------------------------------------------------
    # Daily bookkeeping
    # ------------------------------------------------------------------
    def reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_daily_reset:
            self._daily_start_balance = self.broker.account_balance()
            self._daily_losing_baskets = 0
            self._last_daily_reset = today
            log.info("Daily reset: start balance %.2f", self._daily_start_balance)

    def record_losing_basket(self) -> None:
        self._daily_losing_baskets += 1
        log.info("Losing baskets today: %d", self._daily_losing_baskets)

    # ------------------------------------------------------------------
    # Mode evaluation
    # ------------------------------------------------------------------
    def evaluate_mode(self, symbol: str) -> RiskMode:
        """Compute the strictest risk mode across all dimensions."""

        # 1 — Hard equity stop
        if self._equity_stop_breached():
            return RiskMode.SHUTDOWN

        # 2 — Daily loss limit
        if self._daily_loss_exceeded():
            return RiskMode.SHUTDOWN

        # 3 — Daily losing basket cap
        max_losers = self.risk_cfg.get("max_daily_losing_baskets", 3)
        if self._daily_losing_baskets >= max_losers:
            log.warning("Daily losing basket cap reached (%d)", max_losers)
            return RiskMode.SHUTDOWN

        # 4 — Session window
        if not self.session.is_in_session():
            return RiskMode.SHUTDOWN

        # 5 — Spread explosion
        if self.spread.is_shutdown(symbol):
            log.warning("%s spread explosion → SHUTDOWN", symbol)
            return RiskMode.SHUTDOWN
        if self.spread.is_defensive(symbol):
            log.info("%s spread widened → DEFENSIVE", symbol)
            return RiskMode.DEFENSIVE

        # 6 — News lockout
        lockout = self.sym_cfg.get("news_lockout_minutes", 30)
        if self.news.is_blocked(symbol, lockout):
            return RiskMode.DEFENSIVE

        # 7 — Regime
        regime_val = self.regime.evaluate(symbol)
        if regime_val == Regime.HOSTILE:
            return RiskMode.SHUTDOWN
        if regime_val == Regime.CAUTION:
            return RiskMode.DEFENSIVE

        # 8 — Breakout
        bo = self.breakout.evaluate(symbol)
        if bo.score >= 3:
            return RiskMode.SHUTDOWN
        if bo.score >= 2:
            return RiskMode.DEFENSIVE

        return RiskMode.NORMAL

    # ------------------------------------------------------------------
    # Entry permission (convenience wrapper)
    # ------------------------------------------------------------------
    def can_open_new_cycle(self, symbol: str) -> bool:
        """New basket only in NORMAL mode + volatility + spread OK."""
        mode = self.evaluate_mode(symbol)
        if mode != RiskMode.NORMAL:
            return False
        if not self.volatility.is_acceptable(symbol):
            return False
        if not self.spread.is_acceptable(symbol):
            return False
        return True

    def can_add_grid_level(self, symbol: str) -> bool:
        """Add-on allowed in NORMAL; one final controlled add in DEFENSIVE
        only if spread is still acceptable."""
        mode = self.evaluate_mode(symbol)
        if mode == RiskMode.SHUTDOWN:
            return False
        if mode == RiskMode.DEFENSIVE:
            return False
        if not self.spread.is_acceptable(symbol):
            return False
        return True

    def must_liquidate(self, symbol: str) -> bool:
        return self.evaluate_mode(symbol) == RiskMode.SHUTDOWN

    # ------------------------------------------------------------------
    # Internal checks
    # ------------------------------------------------------------------
    def _equity_stop_breached(self) -> bool:
        balance = self.broker.account_balance()
        equity = self.broker.account_equity()
        if balance == 0:
            return True
        pct = self.risk_cfg.get("equity_stop_pct", 3.0)
        loss = balance - equity
        if loss > balance * pct / 100.0:
            log.critical(
                "EQUITY STOP: loss %.2f > %.2f%% of balance %.2f",
                loss, pct, balance,
            )
            return True
        return False

    def _daily_loss_exceeded(self) -> bool:
        equity = self.broker.account_equity()
        pct = self.risk_cfg.get("max_daily_loss_pct", 5.0)
        if self._daily_start_balance == 0:
            return False
        loss = self._daily_start_balance - equity
        if loss > self._daily_start_balance * pct / 100.0:
            log.critical(
                "DAILY LOSS STOP: loss %.2f > %.2f%% of start balance %.2f",
                loss, pct, self._daily_start_balance,
            )
            return True
        return False
