"""DAY-03 · Opening Momentum 근사.

일봉 백테스트 환경에서는 실제 장초반 분봉을 볼 수 없으므로, "당일 종가가
최근 강한 추세의 연장선에 있고 20봉 신고가 근처에서 마감"하는 것을 근사로 쓴다.
분봉 데이터가 붙는 시점에 same-interface 로 정밀화할 자리를 남겨둔다.
"""
from __future__ import annotations

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class DayMomentum(Strategy):
    name = "day_momentum"

    def __init__(self, roc_period: int = 10, hi_period: int = 20, min_roc: float = 0.05):
        self.roc_period = roc_period
        self.hi_period = hi_period
        self.min_roc = min_roc
        self.warmup = max(roc_period, hi_period) + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        bars = list(ctx.bars[: ctx.at + 1])
        closes = ind.closes(bars)
        cur: Bar = bars[-1]
        r = ind.roc(closes, self.roc_period)[-1]
        hi = max(b.high for b in bars[-self.hi_period:])
        atr_val = ind.atr(bars, 14)[-1]
        if r is None or atr_val is None or atr_val <= 0:
            return StrategyResult.hold("nan")
        near_high = cur.close >= hi * 0.995
        if not (r >= self.min_roc and near_high):
            return StrategyResult.hold("weak-momentum")
        strength = ind.clip(0.4 + r * 2.0, 0.4, 0.9)
        stop = cur.close - 1.5 * atr_val
        target = cur.close + 3.0 * atr_val
        return StrategyResult(
            Signal(Side.BUY, strength, f"mom roc{r*100:.1f}%"),
            stop_hint=stop, target_hint=target,
        )
