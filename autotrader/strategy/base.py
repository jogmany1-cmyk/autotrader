"""전략(신호 생성기) 인터페이스.

각 전략은 어제까지의 봉만 보고 오늘 진입 여부(그리고 만약 진입한다면 손절가와
목표가 힌트)를 리턴한다. `at` 는 "판단이 확정되는 봉의 인덱스"이며,
백테스트에서는 그 다음 봉 시가에 체결한다 — 이 규칙 덕분에 미래정보가 새지 않는다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from ..models import Bar, Signal


@dataclass
class StrategyContext:
    """전략이 볼 수 있는 정보. `bars` 는 언제나 [0..at] 슬라이스만 봐야 한다."""
    symbol: str
    bars: Sequence[Bar]
    at: int  # 판단이 내려지는 봉 인덱스
    # 지표 시리즈 캐시. 같은 종목의 모든 봉이 하나를 공유한다.
    #
    # 없으면 전략이 매 봉마다 [0..at] 전체에 대해 지표를 다시 계산한다 — 봉 수의
    # 제곱으로 느려진다(측정: 1,000봉 6초 / 5,000봉 159초). 이 지표들은 인과적
    # (index i 값이 i 이하 데이터만 사용)이라, 전체를 한 번 계산해 [at] 을 읽는
    # 값과 앞부분만 잘라 계산한 마지막 값이 **정확히** 같다. 그 성질은
    # tests/test_indicator_cache.py 가 고정한다.
    cache: Optional[Dict[Any, Any]] = None


@dataclass
class StrategyResult:
    signal: Signal
    stop_hint: Optional[float] = None
    target_hint: Optional[float] = None

    @staticmethod
    def hold(reason: str = "") -> "StrategyResult":
        return StrategyResult(Signal.hold(reason))


class Strategy(ABC):
    #: 앙상블에서 이 전략을 참조할 때 쓰는 키. Config.weights 의 필드명과 맞춘다.
    name: str = "base"
    #: 계산에 필요한 최소 봉 개수. 미충족이면 자동으로 HOLD.
    warmup: int = 60

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> StrategyResult: ...

    def _guard(self, ctx: StrategyContext) -> Optional[StrategyResult]:
        if ctx.at < self.warmup:
            return StrategyResult.hold("warmup")
        if len(ctx.bars) <= ctx.at:
            return StrategyResult.hold("no-bar")
        return None
