"""Abstract broker interface.

Every concrete broker adapter must implement these methods so the rest of
the bot remains broker-agnostic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class SymbolInfo:
    name: str
    point: float
    digits: int
    spread: int            # current spread in points
    trade_tick_size: float
    trade_tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    real_volume: int = 0


@dataclass
class Position:
    ticket: int
    symbol: str
    type: int              # 0 = BUY, 1 = SELL
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    swap: float
    comment: str
    magic: int
    time: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderResult:
    success: bool
    ticket: int = 0
    message: str = ""


class BaseBroker(ABC):

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]: ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: int, count: int) -> List[Bar]: ...

    @abstractmethod
    def get_tick(self, symbol: str) -> Optional[dict]: ...

    @abstractmethod
    def open_position(
        self, symbol: str, direction: int, volume: float,
        sl: float, tp: float, magic: int, comment: str,
    ) -> OrderResult: ...

    @abstractmethod
    def close_position(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    def get_positions(self, symbol: str, magic: int) -> List[Position]: ...

    @abstractmethod
    def account_balance(self) -> float: ...

    @abstractmethod
    def account_equity(self) -> float: ...
