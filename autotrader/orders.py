"""주문 상태 머신 — 접수와 체결은 같은 것이 아니다.

실계좌에서 `submit()` 이 곧바로 `Fill` 을 반환하면, 다음이 전부 "체결됨" 으로
기록된다:

  - 아직 호가창에 걸려만 있는 미체결 주문
  - 100주 중 30주만 나간 부분체결
  - 증거금 부족으로 **거부된** 주문

그러면 포트폴리오는 있지도 않은 포지션을 들고 있다고 믿고, RiskEngine 은 그
허구를 기준으로 다음 진입을 판단한다. 백테스트에서는 이 구분이 필요 없지만
(같은 봉에서 즉시 체결로 모델링한다) 실계좌에서는 이것이 곧 손실이다.

여기서 다루는 것은 **주문의 생애**이고, 체결(`Fill`)은 그 생애에서 일어나는
사건이다. 둘을 분리하면 부분체결·취소·거부가 각자의 이름을 갖게 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set

from .market import now_kst
from .models import Fill, Side


class OrderStatus(str, Enum):
    CREATED = "CREATED"                    # 우리가 만들었고 아직 안 보냄
    SUBMITTED = "SUBMITTED"                # 보냈고 응답 대기
    ACCEPTED = "ACCEPTED"                  # 브로커가 접수함 (체결 아님)
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"  # 취소 요청 보냄 (취소 아님)
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


#: 더 이상 변하지 않는 상태. 여기 도달하면 재고 대상에서 뺀다.
TERMINAL: Set[OrderStatus] = {
    OrderStatus.FILLED, OrderStatus.CANCELLED,
    OrderStatus.REJECTED, OrderStatus.EXPIRED,
}

#: 아직 체결될 수 있는 상태. 재시작 복구 때 브로커에 되물어야 하는 것들.
OPEN: Set[OrderStatus] = {
    OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_REQUESTED,
}

#: 허용되는 전이. 표에 없는 전이는 버그이므로 조용히 넘기지 않고 거부한다.
#: 특히 종료 상태에서 나가는 길은 없다 — 체결된 주문이 나중에 도착한 낡은
#: "접수" 통보로 되돌아가면 포지션이 유령처럼 되살아난다.
_ALLOWED: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED,
                          OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.ACCEPTED, OrderStatus.REJECTED,
                            OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                            OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.ACCEPTED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                           OrderStatus.CANCEL_REQUESTED, OrderStatus.CANCELLED,
                           OrderStatus.REJECTED, OrderStatus.EXPIRED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED,
                                   OrderStatus.FILLED,
                                   OrderStatus.CANCEL_REQUESTED,
                                   OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.CANCEL_REQUESTED: {OrderStatus.CANCELLED,
                                   OrderStatus.PARTIALLY_FILLED,
                                   OrderStatus.FILLED, OrderStatus.EXPIRED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


class OrderStateError(RuntimeError):
    """허용되지 않는 상태 전이. 삼키지 말 것 — 대개 진짜 버그의 첫 신호다."""


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    return dst in _ALLOWED.get(src, set())


@dataclass(frozen=True)
class ExecutionReport:
    """브로커가 보내오는 주문 관련 사건 하나.

    체결통보(WebSocket)든 주문조회 응답이든 같은 모양으로 정규화해서 받는다.
    그래야 상태 갱신 경로가 하나가 되고, "스트림으로 온 체결" 과 "조회로 본
    체결" 이 서로 다른 결과를 내는 일이 없다.
    """
    broker_order_id: str
    status: OrderStatus
    #: 이 사건에서 **새로** 체결된 수량 (누적이 아니다)
    filled_qty: int = 0
    price: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    ts: Optional[datetime] = None
    reason: str = ""
    #: 브로커가 주는 체결 고유번호. 같은 값이 두 번 오면 두 번째는 무시한다.
    #: 재연결 직후 브로커가 놓친 통보를 다시 밀어주는 것이 정상 동작이라,
    #: 이 방어가 없으면 재연결 한 번에 보유수량이 두 배가 된다.
    exec_id: str = ""


@dataclass
class BrokerOrder:
    """주문 하나의 생애. 접수·부분체결·거부가 각자의 이름을 갖는다."""
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: Optional[str] = None
    filled_qty: int = 0
    #: 체결 금액 누적 / 체결 수량 = 평균 체결가
    _filled_notional: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    reject_reason: str = ""
    tag: str = ""
    created_at: datetime = field(default_factory=now_kst)
    updated_at: datetime = field(default_factory=now_kst)
    _seen_exec_ids: Set[str] = field(default_factory=set)

    # ---- 파생 값 -------------------------------------------------------

    @property
    def remaining(self) -> int:
        return max(self.qty - self.filled_qty, 0)

    @property
    def avg_fill_price(self) -> float:
        return self._filled_notional / self.filled_qty if self.filled_qty else 0.0

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    # ---- 상태 전이 -----------------------------------------------------

    def transition(self, dst: OrderStatus, *, reason: str = "") -> None:
        if dst is self.status and dst is not OrderStatus.PARTIALLY_FILLED:
            return                      # 같은 상태 재통보는 무해하다
        if not can_transition(self.status, dst):
            raise OrderStateError(
                f"{self.client_order_id}: {self.status.value} → {dst.value} 은 "
                f"허용되지 않는 전이입니다")
        self.status = dst
        if reason:
            self.reject_reason = reason
        self.updated_at = now_kst()

    def apply(self, report: ExecutionReport) -> bool:
        """체결통보를 반영한다. 반영했으면 True, 중복이라 무시했으면 False.

        **멱등해야 한다.** 재연결 직후 브로커가 놓친 통보를 다시 밀어주는 것은
        정상 동작이고, 그때 중복을 걸러내지 못하면 보유수량이 두 배가 된다.
        """
        if report.exec_id and report.exec_id in self._seen_exec_ids:
            return False
        if self.is_terminal and report.filled_qty <= 0:
            # 이미 끝난 주문에 대한 낡은 통보. 상태를 되돌리지 않는다.
            return False

        if report.filled_qty > 0:
            add = min(report.filled_qty, self.remaining)
            if add <= 0:
                return False            # 이미 다 채웠는데 더 왔다 — 중복으로 본다
            self.filled_qty += add
            self._filled_notional += add * report.price
            self.fills.append(Fill(
                ts=report.ts or now_kst(), symbol=self.symbol, side=self.side,
                qty=add, price=report.price, fee=report.fee, tax=report.tax,
                tag=self.tag,
            ))
            dst = (OrderStatus.FILLED if self.remaining == 0
                   else OrderStatus.PARTIALLY_FILLED)
            self.transition(dst)
        else:
            self.transition(report.status, reason=report.reason)

        if report.exec_id:
            self._seen_exec_ids.add(report.exec_id)
        if report.broker_order_id:
            self.broker_order_id = report.broker_order_id
        self.updated_at = now_kst()
        return True

    # ---- 직렬화 (재시작 복구용) ----------------------------------------

    def as_dict(self) -> Dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": round(self.avg_fill_price, 4),
            "reject_reason": self.reject_reason,
            "tag": self.tag,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
