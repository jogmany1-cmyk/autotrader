"""시세 공급자 인터페이스.

백테스트·모의매매·실매매가 모두 같은 인터페이스를 쓰기 때문에,
데이터 소스를 바꿔도 전략/리스크/실행 코드는 손대지 않는다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Sequence

from ..models import Bar


class DataError(RuntimeError):
    pass


class DataProvider(ABC):
    @abstractmethod
    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        """가장 오래된 봉부터 최신 봉까지, 최대 limit 개."""

    def universe(self) -> List[str]:
        """이 공급자가 알고 있는 전체 종목. 스크리너의 기본 후보군."""
        return []

    def history_many(self, symbols: Sequence[str], limit: int = 500) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for s in symbols:
            try:
                bars = self.history(s, limit)
            except DataError:
                continue
            if bars:
                out[s] = bars
        return out

    def last_price(self, symbol: str) -> float:
        bars = self.history(symbol, limit=2)
        if not bars:
            raise DataError(f"{symbol}: 시세 없음")
        return bars[-1].close
