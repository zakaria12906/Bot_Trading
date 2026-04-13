"""MetaTrader 5 broker adapter.

Wraps the MetaTrader5 Python package behind the BaseBroker interface.
Includes automatic reconnection and symbol_select handling.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Set

from broker.base_broker import (
    BaseBroker, OrderResult, Position, SymbolInfo, BUY, SELL,
)

log = logging.getLogger("hedged_grid.mt5")

_MAX_RECONNECT = 5
_RECONNECT_DELAY = 3


class MT5Connector(BaseBroker):

    def __init__(self, login: int, password: str, server: str, path: str = ""):
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        self._mt5 = None
        self._selected: Set[str] = set()

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> bool:
        import MetaTrader5 as mt5
        self._mt5 = mt5

        kw = {"login": self._login, "password": self._password,
              "server": self._server}
        if self._path:
            kw["path"] = self._path

        if not mt5.initialize(**kw):
            log.error("MT5 init failed: %s", mt5.last_error())
            return False

        info = mt5.account_info()
        if info is None:
            log.error("Cannot get account info")
            return False

        log.info("Connected — %s | acct %d | balance %.2f %s",
                 info.server, info.login, info.balance, info.currency)
        return True

    def _alive(self) -> bool:
        mt5 = self._mt5
        if mt5 is None:
            return False
        if mt5.account_info() is not None:
            return True
        log.warning("MT5 lost — reconnecting")
        for i in range(1, _MAX_RECONNECT + 1):
            mt5.shutdown()
            time.sleep(_RECONNECT_DELAY)
            if self.connect():
                self._selected.clear()
                return True
            log.warning("Reconnect %d/%d failed", i, _MAX_RECONNECT)
        log.critical("All reconnect attempts exhausted")
        return False

    def _select(self, symbol: str) -> bool:
        if symbol in self._selected:
            return True
        if self._mt5 and self._mt5.symbol_select(symbol, True):
            self._selected.add(symbol)
            return True
        return False

    def shutdown(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
            log.info("MT5 connection closed")

    # ── Market data ───────────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        if not self._alive():
            return None
        self._select(symbol)
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        tick = mt5.symbol_info_tick(symbol)
        spread = int((tick.ask - tick.bid) / info.point) if tick else info.spread
        return SymbolInfo(
            name=info.name, point=info.point, digits=info.digits,
            spread=spread,
            trade_tick_size=info.trade_tick_size,
            trade_tick_value=info.trade_tick_value,
            volume_min=info.volume_min, volume_max=info.volume_max,
            volume_step=info.volume_step,
            contract_size=info.trade_contract_size,
        )

    def get_tick(self, symbol: str) -> Optional[dict]:
        if not self._alive():
            return None
        self._select(symbol)
        t = self._mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return {"bid": t.bid, "ask": t.ask, "time": t.time}

    # ── Trading ───────────────────────────────────────────────────────────

    def open_position(
        self, symbol: str, direction: int, volume: float,
        magic: int = 0, comment: str = "",
    ) -> OrderResult:
        if not self._alive():
            return OrderResult(False, message="MT5 disconnected")
        self._select(symbol)
        mt5 = self._mt5
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(False, message="No tick")

        price = tick.ask if direction == BUY else tick.bid
        otype = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": round(volume, 2),
            "type": otype,
            "price": price,
            "deviation": 30,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None:
            return OrderResult(False, message=str(mt5.last_error()))
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"rc={res.retcode} {res.comment}")

        log.info("OPEN %s %s %.2f @ %.2f  ticket=%d",
                 "BUY" if direction == BUY else "SELL",
                 symbol, volume, price, res.order)
        return OrderResult(True, ticket=res.order, price=price)

    def close_position(self, ticket: int) -> OrderResult:
        if not self._alive():
            return OrderResult(False, message="MT5 disconnected")
        mt5 = self._mt5
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return OrderResult(False, message=f"Ticket {ticket} not found")

        p = pos[0]
        ctype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == 0 else tick.ask

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": ctype,
            "position": ticket,
            "price": price,
            "deviation": 30,
            "magic": p.magic,
            "comment": "hg_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None:
            return OrderResult(False, message=str(mt5.last_error()))
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"rc={res.retcode} {res.comment}")

        log.info("CLOSE ticket=%d  %.2f lots  P/L=%.2f", ticket, p.volume, p.profit)
        return OrderResult(True, ticket=ticket, price=price)

    # ── Queries ───────────────────────────────────────────────────────────

    def get_positions(self, symbol: str, magic: int) -> List[Position]:
        if not self._alive():
            return []
        raw = self._mt5.positions_get(symbol=symbol)
        if raw is None:
            return []
        out = []
        for p in raw:
            if p.magic != magic:
                continue
            out.append(Position(
                ticket=p.ticket, symbol=p.symbol, type=p.type,
                volume=p.volume, price_open=p.price_open,
                profit=p.profit, swap=p.swap,
                comment=p.comment, magic=p.magic,
                time=datetime.fromtimestamp(p.time, tz=timezone.utc),
            ))
        return out

    def account_balance(self) -> float:
        if not self._alive():
            return 0.0
        info = self._mt5.account_info()
        return info.balance if info else 0.0

    def account_equity(self) -> float:
        if not self._alive():
            return 0.0
        info = self._mt5.account_info()
        return info.equity if info else 0.0
