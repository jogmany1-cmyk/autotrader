"""브로커 인터페이스. 페이퍼/실전을 같은 코드에서 스위치할 수 있게 한다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from ..models import Order, Position
from ..orders import BrokerOrder


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    @abstractmethod
    def submit(self, order: Order, price_hint: float) -> BrokerOrder:
        """주문을 낸다. 반환값은 **체결이 아니라 주문**이다.

        실계좌에서 접수(ACCEPTED)와 체결(FILLED)은 다른 사건이다. 접수 응답을
        체결로 취급하면 미체결·부분체결·거부가 전부 "체결됨" 으로 기록되고,
        포트폴리오가 있지도 않은 포지션을 들고 있다고 믿게 된다.

        페이퍼는 즉시 체결로 모델링하므로 FILLED 를 담아 돌려준다.
        실브로커는 대개 ACCEPTED 이며, 체결은 체결통보나 조회로 따로 온다.
        """

    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> Dict[str, Position]: ...

    def list_stocks(self, market_code: str = "0") -> List[dict]:
        """종목 마스터. 구현체가 지원 시 대체. 기본은 빈 리스트."""
        return []
