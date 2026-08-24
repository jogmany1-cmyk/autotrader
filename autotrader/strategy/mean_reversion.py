"""보조 · 평균회귀.

과매도 반등만 노리는 축소 전략. 추세 전략과 상관성이 낮아 앙상블 분산에 기여한다.
장기 상승추세 안에서만 발동하도록 제한.
"""
from __future__ import annotations

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(self, rsi_p: int = 5, rsi_buy: float = 22.0,
                 slow: int = 200, atr_p: int = 14):
        self.rsi_p = rsi_p
        self.rsi_buy = rsi_buy
        self.slow = slow
        self.atr_p = atr_p
        self.warmup = slow + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        bars = list(ctx.bars[: ctx.at + 1])
        ma_s = ind.sma_at(ctx, self.slow)
        atr_val = ind.atr_at(ctx, self.atr_p)
        rsi = ind.rsi_at(ctx, self.rsi_p)
        cur = bars[-1]
        if None in (ma_s, atr_val, rsi) or atr_val <= 0:
            return StrategyResult.hold("nan")
        if not (cur.close > ma_s and rsi <= self.rsi_buy):
            return StrategyResult.hold("no-mr")
        strength = ind.clip(0.3 + (self.rsi_buy - rsi) / 40, 0.3, 0.7)
        stop = cur.close - 1.5 * atr_val
        target = cur.close + 1.5 * atr_val
        return StrategyResult(
            Signal(Side.BUY, strength, f"mr rsi{rsi:.1f}"),
            stop_hint=stop, target_hint=target,
        )
