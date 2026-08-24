"""청산 판정 — 페이퍼와 실계좌가 같은 규칙을 쓰게 하는 곳.

청산 규칙이 `PaperBroker.mark()` 안에만 있었다. 그래서 실브로커로 돌리면
**스탑이 아예 걸리지 않았다.** 백테스트에서 검증한 손절·트레일링·시간청산이
실계좌에서는 하나도 동작하지 않는다는 뜻이다. 승격 경로에서 가장 위험한 종류의
불일치다 — 검증한 것과 다른 것이 돌아가는데 겉으로는 같아 보인다.

여기서는 **판정만** 한다. 판정과 집행을 나누면:

  - 페이퍼는 판정 결과를 자기 포트폴리오에 즉시 적용한다 (기존과 동일).
  - 실계좌는 판정 결과로 청산 **주문**을 낸다. 주문이 체결돼야 청산이다.

브로커가 스탑 주문을 직접 받아 주면 그쪽이 낫다 — 우리 프로세스가 죽어도
살아 있기 때문이다. 다만 그런 주문을 지원하지 않거나 조건이 다를 수 있으므로,
직접 감시해서 청산 주문을 내는 이 경로가 항상 필요하다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .models import Position


class BarLike(Protocol):
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExitSignal:
    """청산해야 한다는 판정 하나."""
    symbol: str
    qty: int
    reason: str
    #: 봉 데이터로 판정할 때의 가정 체결가. 실계좌에서는 참고값일 뿐이며,
    #: 실제 체결가는 브로커가 정한다.
    price: float


#: 우선순위. 같은 시점에 여러 조건이 맞으면 앞의 것이 이긴다.
#:
#: hard_stop 이 맨 앞인 이유: 계좌 보호가 전략 판단보다 우선한다.
#: target 이 stop 보다 뒤인 이유: 한 봉 안에서 둘 다 닿았을 때 어느 쪽이 먼저
#: 닿았는지 일봉으로는 알 수 없다. 손실 쪽을 택하는 것이 보수적이다
#: (`PaperBroker` 의 봉내 가정 — docs/PITFALLS.md 참고).
EXIT_PRIORITY = ("hard_stop", "stop", "trail", "target", "time", "eod_flat")


def evaluate_exit(pos: Position, bar: BarLike, *,
                  max_hold: Optional[int] = None,
                  hard_stop_pct: float = 0.0) -> Optional[ExitSignal]:
    """이 포지션을 지금 청산해야 하는가. 아니면 None.

    봉(고·저)을 쓰므로 백테스트·페이퍼용이다. 실시간에서는 현재가 하나만
    있으므로 `evaluate_exit_live()` 를 쓴다.
    """
    hard_line = pos.avg_price * (1 - hard_stop_pct) if hard_stop_pct > 0 else 0.0
    if hard_line and bar.low <= hard_line:
        return ExitSignal(pos.symbol, pos.qty, "hard_stop", min(hard_line, bar.open))
    if pos.stop_price is not None and bar.low <= pos.stop_price:
        # 트레일링이 끌어올린 스탑과 전략이 정한 초기 손절은 성격이 다르다.
        # 하나로 기록하면 무엇이 포지션을 끊었는지 알 수 없다.
        reason = "trail" if pos.stop_from_trail else "stop"
        return ExitSignal(pos.symbol, pos.qty, reason, min(pos.stop_price, bar.open))
    if pos.take_price is not None and bar.high >= pos.take_price:
        return ExitSignal(pos.symbol, pos.qty, "target", max(pos.take_price, bar.open))
    if max_hold is not None and pos.bars_held >= max_hold:
        return ExitSignal(pos.symbol, pos.qty, "time", bar.close)
    return None


def evaluate_exit_live(pos: Position, price: float, *,
                       max_hold: Optional[int] = None,
                       hard_stop_pct: float = 0.0) -> Optional[ExitSignal]:
    """실시간 현재가 하나로 판정한다.

    봉 판정과 우선순위가 같아야 한다 — 다르면 페이퍼에서 검증한 청산이
    실계좌에서 다른 순서로 일어난다. 고·저가 없으므로 현재가가 선을 넘었는지만
    본다. 그래서 장중에 선을 찍고 되돌아온 움직임은 놓칠 수 있다. 이 경로는
    호출 주기만큼만 촘촘하며, 그보다 정확하려면 브로커 스탑 주문을 써야 한다.
    """
    if price <= 0:
        return None
    hard_line = pos.avg_price * (1 - hard_stop_pct) if hard_stop_pct > 0 else 0.0
    if hard_line and price <= hard_line:
        return ExitSignal(pos.symbol, pos.qty, "hard_stop", price)
    if pos.stop_price is not None and price <= pos.stop_price:
        reason = "trail" if pos.stop_from_trail else "stop"
        return ExitSignal(pos.symbol, pos.qty, reason, price)
    if pos.take_price is not None and price >= pos.take_price:
        return ExitSignal(pos.symbol, pos.qty, "target", price)
    if max_hold is not None and pos.bars_held >= max_hold:
        return ExitSignal(pos.symbol, pos.qty, "time", price)
    return None
