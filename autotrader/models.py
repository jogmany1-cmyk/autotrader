"""시스템 전체가 공유하는 기본 자료구조.

의도적으로 외부 의존성이 없다. pandas/numpy 없이도 백테스트와 모의매매가
그대로 돌아가야 어떤 환경에서든 검증할 수 있기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional, Dict, List


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class Bar:
    """일봉(또는 임의 주기) 한 개."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def day(self) -> date:
        return self.ts.date()

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class Signal:
    """전략이 내놓는 매매 판단.

    strength 는 0..1 로 정규화된 확신도이며, 앙상블 가중 합산과
    포지션 크기 조절 양쪽에 쓰인다.
    """
    side: Side
    strength: float = 0.0
    reason: str = ""

    @staticmethod
    def hold(reason: str = "") -> "Signal":
        return Signal(Side.HOLD, 0.0, reason)

    def clamped(self) -> "Signal":
        s = min(1.0, max(0.0, self.strength))
        return Signal(self.side, s, self.reason)


@dataclass
class Order:
    symbol: str
    side: Side
    qty: int
    type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    tag: str = ""

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 합니다: {self.qty}")
        if self.type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("지정가 주문에는 limit_price 가 필요합니다")


@dataclass
class Fill:
    ts: datetime
    symbol: str
    side: Side
    qty: int
    price: float
    fee: float = 0.0
    tax: float = 0.0
    tag: str = ""

    @property
    def gross(self) -> float:
        return self.qty * self.price

    @property
    def cost(self) -> float:
        """현금 흐름(매수는 +지출, 매도는 -지출). 수수료·세금 포함."""
        if self.side is Side.BUY:
            return self.gross + self.fee
        return -(self.gross - self.fee - self.tax)


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    opened_at: datetime
    stop_price: Optional[float] = None
    take_price: Optional[float] = None
    highest_close: float = 0.0
    bars_held: int = 0
    meta: Dict[str, float] = field(default_factory=dict)

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.qty

    def return_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    cash: float
    exposure: float


@dataclass
class Trade:
    """진입~청산이 끝난 한 라운드 트립."""
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    qty: int
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    exit_reason: str
    bars_held: int


@dataclass
class ScreenResult:
    symbol: str
    score: float
    factors: Dict[str, float] = field(default_factory=dict)
    passed: bool = True
    reject_reason: str = ""


__all__ = [
    "Side", "OrderType", "Bar", "Signal", "Order", "Fill",
    "Position", "EquityPoint", "Trade", "ScreenResult",
]
