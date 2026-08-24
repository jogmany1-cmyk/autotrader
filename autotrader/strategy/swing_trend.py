"""SWING-01 · 추세추종.

50/200 이평 정배열 + 200봉 상대적 위치 + 최소 모멘텀. 단타보다 신호가 적지만
한 번 잡히면 오래 들고 갈 수 있는 스타일이며, 실제 실적 요인과 잘 붙는다.
"""
from __future__ import annotations

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class SwingTrend(Strategy):
    name = "swing_trend"

    def __init__(self, fast: int = 50, slow: int = 200, atr_p: int = 14,
                 min_roc_120: float = 0.05):
        self.fast = fast
        self.slow = slow
        self.atr_p = atr_p
        self.min_roc_120 = min_roc_120
        self.warmup = slow + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        cur = ctx.bars[ctx.at]
        ma_f = ind.sma_at(ctx, self.fast)
        ma_s = ind.sma_at(ctx, self.slow)
        atr_val = ind.atr_at(ctx, self.atr_p)
        # 기간은 ctx.at 로 정한다. 전체 시리즈 길이를 쓰면 미래 봉까지 세어
        # 지평선이 달라진다 (PITFALLS #24 에서 실제로 당한 함정).
        r120 = ind.roc_at(ctx, min(120, ctx.at))
        if None in (ma_f, ma_s, atr_val, r120) or atr_val <= 0:
            return StrategyResult.hold("nan")
        if not (ma_f > ma_s and cur.close > ma_f and r120 >= self.min_roc_120):
            return StrategyResult.hold("no-trend")
        strength = ind.clip(0.5 + r120 * 1.0, 0.5, 0.95)
        stop = min(cur.close - 2.5 * atr_val, ma_s)
        target = cur.close + 5.0 * atr_val
        return StrategyResult(
            Signal(Side.BUY, strength, f"trend r120 {r120*100:.1f}%"),
            stop_hint=stop, target_hint=target,
        )
