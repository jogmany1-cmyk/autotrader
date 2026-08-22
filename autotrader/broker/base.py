"""브로커 인터페이스. 페이퍼/실전을 같은 코드에서 스위치할 수 있게 한다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from ..models import Fill, Order, Position


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    @abstractmethod
    def submit(self, order: Order, price_hint: float) -> Fill: ...

    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def positions(self) -> Dict[str, Position]: ...
