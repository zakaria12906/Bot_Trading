"""Main bot orchestrator — the tick-by-tick decision engine.

For each enabled symbol the bot runs one SymbolEngine instance.  Each engine
manages at most one basket at a time and follows the blueprint's decision
hierarchy on every tick:

    1. Reset daily counters if date changed
    2. If SHUTDOWN → liquidate any open basket, stand down
    3. If no basket open → check whether conditions allow a new cycle
    4. If basket open → check exit hierarchy, then check grid level adds
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from broker.base_broker import BaseBroker
from core.grid_manager import GridManager
from core.basket_manager import BasketManager
from core.risk_manager import RiskManager, RiskMode
from filters.regime_filter import RegimeFilter, Regime
from filters.volatility_filter import VolatilityFilter
from filters.spread_filter import SpreadFilter
from filters.session_filter import SessionFilter
from filters.news_filter import NewsFilter
from filters.breakout_detector import BreakoutDetector
from indicators.moving_average import sma, ema
from utils.helpers import tf_to_mt5

log = logging.getLogger("grid_bot.engine")

BUY = 0
SELL = 1


class SymbolEngine:
    """Manages the full lifecycle for a single symbol."""

    def __init__(
        self,
        symbol: str,
        broker: BaseBroker,
        sym_cfg: dict,
        risk_cfg: dict,
        news_filter: NewsFilter,
        magic: int,
    ):
        self.symbol = symbol
        self.broker = broker
        self.sym_cfg = sym_cfg
        self.risk_cfg = risk_cfg
        self.magic = magic

        # filters
        self.regime_filter = RegimeFilter(broker, sym_cfg)
        self.vol_filter = VolatilityFilter(broker, sym_cfg)
        self.spread_filter = SpreadFilter(broker, sym_cfg, risk_cfg)
        self.session_filter = SessionFilter(sym_cfg)
        self.breakout_det = BreakoutDetector(broker, sym_cfg, risk_cfg)

        # core
        self.risk = RiskManager(
            broker, risk_cfg, sym_cfg,
            self.regime_filter, self.vol_filter, self.spread_filter,
            self.session_filter, news_filter, self.breakout_det,
        )
        self.grid = GridManager(sym_cfg, self.vol_filter)
        self.basket = BasketManager(broker, sym_cfg, magic)

        self._cooldown_until: Optional[datetime] = None
        self._spread_baselined = False

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """Called once per loop iteration."""
        self.risk.reset_daily_if_needed()

        # Baseline spread once per session
        if not self._spread_baselined and self.session_filter.is_in_session():
            self.spread_filter.update_baseline(self.symbol)
            self._spread_baselined = True

        # Cooldown after a losing basket
        if self._cooldown_until:
            if datetime.now(timezone.utc) < self._cooldown_until:
                return
            self._cooldown_until = None
            log.info("%s cooldown expired, resuming", self.symbol)

        if self.basket.has_active_basket:
            self._manage_open_basket()
        else:
            self._try_open_cycle()

    # ------------------------------------------------------------------
    # Open-basket management
    # ------------------------------------------------------------------
    def _manage_open_basket(self) -> None:
        symbol = self.symbol
        mode = self.risk.evaluate_mode(symbol)

        # Priority 1+2: emergency / regime shutdown
        if mode == RiskMode.SHUTDOWN:
            pnl = self.basket.close_basket("SHUTDOWN")
            self._post_close(pnl)
            return

        # Priority 3: time stop
        if self.basket.check_time_stop():
            pnl = self.basket.close_basket("TIME_STOP")
            self._post_close(pnl)
            return

        # Priority 4: basket TP
        if self.basket.check_take_profit():
            pnl = self.basket.close_basket("TAKE_PROFIT")
            self._post_close(pnl)
            return

        # Priority 5: scratch exit if regime deteriorated
        regime_bad = mode == RiskMode.DEFENSIVE
        if self.basket.check_scratch_exit(regime_bad):
            pnl = self.basket.close_basket("SCRATCH_EXIT")
            self._post_close(pnl)
            return

        # Grid add-on check
        if mode == RiskMode.NORMAL and self.risk.can_add_grid_level(symbol):
            self._check_grid_add()

    def _check_grid_add(self) -> None:
        if not self.basket.active:
            return
        plan = self.basket.active.plan
        tick = self.broker.get_tick(self.symbol)
        if tick is None:
            return

        price = tick["bid"] if plan.direction == BUY else tick["ask"]
        level = self.grid.should_fill_next(plan, price)
        if level is None or level.filled:
            return

        direction = plan.direction
        result = self.broker.open_position(
            self.symbol, direction, plan.lot_size,
            magic=self.magic,
            comment=f"GRID_L{level.index}",
        )
        if result.success:
            level.filled = True
            level.ticket = result.ticket
            log.info(
                "%s grid level %d filled @ %.5f | ticket %d",
                self.symbol, level.index, price, result.ticket,
            )

    # ------------------------------------------------------------------
    # New cycle entry
    # ------------------------------------------------------------------
    def _try_open_cycle(self) -> None:
        symbol = self.symbol

        if not self.risk.can_open_new_cycle(symbol):
            return

        direction = self._compute_bias()
        if direction is None:
            return

        info = self.broker.get_symbol_info(symbol)
        if info is None:
            return

        tick = self.broker.get_tick(symbol)
        if tick is None:
            return

        entry_price = tick["ask"] if direction == BUY else tick["bid"]

        # Build grid plan
        plan = self.grid.build_plan(symbol, direction, entry_price, info.point)

        # Open initial position
        result = self.broker.open_position(
            symbol, direction, plan.lot_size,
            magic=self.magic,
            comment="GRID_L0",
        )
        if not result.success:
            log.warning("%s initial entry failed: %s", symbol, result.message)
            return

        plan.levels[0].filled = True
        plan.levels[0].ticket = result.ticket

        self.basket.open_basket(symbol, direction, plan)
        log.info(
            "%s new cycle: %s @ %.5f | ticket %d",
            symbol, "BUY" if direction == BUY else "SELL",
            entry_price, result.ticket,
        )

    def _compute_bias(self) -> Optional[int]:
        """Determine directional bias using higher-TF MA + short-TF pullback.

        Returns BUY, SELL, or None if conditions are not met.
        """
        htf = tf_to_mt5(self.sym_cfg.get("higher_tf", "H1"))
        etf = tf_to_mt5(self.sym_cfg.get("entry_tf", "M15"))
        h_period = self.sym_cfg.get("higher_ma_period", 200)
        s_period = self.sym_cfg.get("short_ma_period", 20)

        bars_h = self.broker.get_bars(self.symbol, htf, h_period + 10)
        bars_e = self.broker.get_bars(self.symbol, etf, s_period + 10)

        if len(bars_h) < h_period or len(bars_e) < s_period:
            return None

        higher_ma = sma(bars_h, h_period)
        short_ma_val = ema(bars_e, s_period)
        last_close = bars_e[-1].close

        if higher_ma == 0 or short_ma_val == 0:
            return None

        # Higher TF bias
        if last_close > higher_ma:
            # Bullish context — enter BUY on pullback toward short MA
            if last_close <= short_ma_val * 1.001:
                return BUY
        else:
            # Bearish context — enter SELL on pullback toward short MA
            if last_close >= short_ma_val * 0.999:
                return SELL

        return None

    # ------------------------------------------------------------------
    # Post-close housekeeping
    # ------------------------------------------------------------------
    def _post_close(self, pnl: float) -> None:
        if pnl < 0:
            self.risk.record_losing_basket()
            cooldown = self.sym_cfg.get("cooldown_after_loss_sec",
                                        self.risk_cfg.get("cooldown_after_loss_sec", 300))
            from datetime import timedelta
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
            log.info(
                "%s cooling down for %ds after loss %.2f",
                self.symbol, cooldown, pnl,
            )
        self.basket.clear()
        self._spread_baselined = False  # re-baseline next session


# ======================================================================
# Top-level bot that manages all symbols
# ======================================================================

class GridRecoveryBot:
    """The top-level bot.  Instantiates one SymbolEngine per enabled symbol
    and runs the main loop."""

    def __init__(self, broker: BaseBroker, cfg: dict, news_filter: NewsFilter):
        self.broker = broker
        self.cfg = cfg
        self.news = news_filter
        self.engines: Dict[str, SymbolEngine] = {}
        self._running = False

        general = cfg.get("general", {})
        base_magic = general.get("magic_number", 777001)
        risk_cfg = cfg.get("risk", {})

        for i, (sym, sym_cfg) in enumerate(cfg.get("symbols", {}).items()):
            if not sym_cfg.get("enabled", False):
                continue
            # Merge risk-level thresholds into sym_cfg for convenience
            merged_sym = {**sym_cfg}
            for key in ("shutdown_atr_ratio", "shutdown_adx"):
                if key in risk_cfg and key not in merged_sym:
                    merged_sym[key] = risk_cfg[key]

            engine = SymbolEngine(
                symbol=sym,
                broker=broker,
                sym_cfg=merged_sym,
                risk_cfg=risk_cfg,
                news_filter=news_filter,
                magic=base_magic + i,
            )
            self.engines[sym] = engine
            log.info("Engine registered: %s (magic %d)", sym, base_magic + i)

    def start(self) -> None:
        """Blocking main loop."""
        interval = self.cfg.get("general", {}).get("tick_interval_sec", 1)
        self._running = True
        log.info("Bot started — %d symbol(s) active", len(self.engines))

        while self._running:
            for engine in self.engines.values():
                try:
                    engine.tick()
                except Exception:
                    log.exception("Tick error on %s", engine.symbol)
            time.sleep(interval)

    def stop(self) -> None:
        log.info("Stop requested — closing all baskets…")
        self._running = False
        for engine in self.engines.values():
            if engine.basket.has_active_basket:
                engine.basket.close_basket("BOT_SHUTDOWN")
                engine.basket.clear()

    # ------------------------------------------------------------------
    # Webhook entry point
    # ------------------------------------------------------------------
    def handle_webhook_signal(self, symbol: str, direction: str) -> dict:
        """Called by the webhook server.  Forces a bias override for the
        next cycle if conditions permit."""
        engine = self.engines.get(symbol)
        if engine is None:
            return {"error": f"Symbol {symbol} not configured"}

        if engine.basket.has_active_basket:
            return {"status": "ignored", "reason": "basket already open"}

        dir_int = BUY if direction.upper() in ("BUY", "LONG") else SELL
        if not engine.risk.can_open_new_cycle(symbol):
            return {"status": "blocked", "reason": "risk filter rejected"}

        info = engine.broker.get_symbol_info(symbol)
        tick = engine.broker.get_tick(symbol)
        if not info or not tick:
            return {"error": "cannot fetch market data"}

        entry_price = tick["ask"] if dir_int == BUY else tick["bid"]
        plan = engine.grid.build_plan(symbol, dir_int, entry_price, info.point)

        result = engine.broker.open_position(
            symbol, dir_int, plan.lot_size,
            magic=engine.magic, comment="GRID_L0_WH",
        )
        if not result.success:
            return {"error": result.message}

        plan.levels[0].filled = True
        plan.levels[0].ticket = result.ticket
        engine.basket.open_basket(symbol, dir_int, plan)

        return {
            "status": "opened",
            "symbol": symbol,
            "direction": direction,
            "ticket": result.ticket,
        }
