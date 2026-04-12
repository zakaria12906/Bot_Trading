"""MetaTrader 5 broker adapter.

Wraps the MetaTrader5 Python package behind the BaseBroker interface so
the rest of the bot never imports MT5 directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from broker.base_broker import (
    Bar, BaseBroker, OrderResult, Position, SymbolInfo,
)

log = logging.getLogger("grid_bot.mt5")

BUY = 0
SELL = 1


class MT5Connector(BaseBroker):

    def __init__(self, login: int, password: str, server: str, path: str = ""):
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        self._mt5 = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        import MetaTrader5 as mt5
        self._mt5 = mt5

        kwargs = {
            "login": self._login,
            "password": self._password,
            "server": self._server,
        }
        if self._path:
            kwargs["path"] = self._path

        if not mt5.initialize(**kwargs):
            log.error("MT5 initialize failed: %s", mt5.last_error())
            return False

        info = mt5.account_info()
        if info is None:
            log.error("Cannot retrieve account info")
            return False

        log.info(
            "Connected to %s | account %d | balance %.2f %s",
            info.server, info.login, info.balance, info.currency,
        )
        return True

    def shutdown(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
            log.info("MT5 connection closed")

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        tick = mt5.symbol_info_tick(symbol)
        spread = int(tick.ask / info.point - tick.bid / info.point) if tick else info.spread
        return SymbolInfo(
            name=info.name,
            point=info.point,
            digits=info.digits,
            spread=spread,
            trade_tick_size=info.trade_tick_size,
            trade_tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
        )

    def get_bars(self, symbol: str, timeframe: int, count: int) -> List[Bar]:
        rates = self._mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return []
        bars = []
        for r in rates:
            bars.append(Bar(
                time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
                real_volume=int(r["real_volume"]) if "real_volume" in r.dtype.names else 0,
            ))
        return bars

    def get_tick(self, symbol: str) -> Optional[dict]:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {"bid": tick.bid, "ask": tick.ask, "time": tick.time}

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    def open_position(
        self, symbol: str, direction: int, volume: float,
        sl: float = 0.0, tp: float = 0.0,
        magic: int = 0, comment: str = "",
    ) -> OrderResult:
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            return OrderResult(False, message=f"Symbol {symbol} not found")

        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if direction == BUY else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(False, message=str(mt5.last_error()))
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"retcode={result.retcode} {result.comment}")

        log.info(
            "Opened %s %s %.2f lots @ %.5f | ticket %d",
            "BUY" if direction == BUY else "SELL",
            symbol, volume, price, result.order,
        )
        return OrderResult(True, ticket=result.order)

    def close_position(self, ticket: int) -> OrderResult:
        mt5 = self._mt5
        pos = mt5.positions_get(ticket=ticket)
        if pos is None or len(pos) == 0:
            return OrderResult(False, message=f"Position {ticket} not found")

        p = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == 0 else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": p.magic,
            "comment": "grid_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(False, message=str(mt5.last_error()))
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"retcode={result.retcode} {result.comment}")

        log.info("Closed ticket %d | %.2f lots | P/L %.2f", ticket, p.volume, p.profit)
        return OrderResult(True, ticket=ticket)

    # ------------------------------------------------------------------
    # Account / position queries
    # ------------------------------------------------------------------
    def get_positions(self, symbol: str, magic: int) -> List[Position]:
        raw = self._mt5.positions_get(symbol=symbol)
        if raw is None:
            return []
        positions = []
        for p in raw:
            if p.magic != magic:
                continue
            positions.append(Position(
                ticket=p.ticket,
                symbol=p.symbol,
                type=p.type,
                volume=p.volume,
                price_open=p.price_open,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                swap=p.swap,
                comment=p.comment,
                magic=p.magic,
                time=datetime.fromtimestamp(p.time, tz=timezone.utc),
            ))
        return positions

    def account_balance(self) -> float:
        info = self._mt5.account_info()
        return info.balance if info else 0.0

    def account_equity(self) -> float:
        info = self._mt5.account_info()
        return info.equity if info else 0.0
