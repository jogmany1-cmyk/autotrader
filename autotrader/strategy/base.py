"""전략(신호 생성기) 인터페이스.

각 전략은 어제까지의 봉만 보고 오늘 진입 여부(그리고 만약 진입한다면 손절가와
목표가 힌트)를 리턴한다. `at` 는 "판단이 확정되는 봉의 인덱스"이며,
백테스트에서는 그 다음 봉 시가에 체결한다 — 이 규칙 덕분에 미래정보가 새지 않는다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

from ..models import Bar, Signal


@dataclass
class StrategyContext:
    """전략이 볼 수 있는 정보. `bars` 는 언제나 [0..at] 슬라이스만 봐야 한다."""
    symbol: str
    bars: Sequence[Bar]
    at: int  # 판단이 내려지는 봉 인덱스


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
