"""DAY-02 · 눌림목형.

상승 추세 안에서 이동평균으로 되돌림 → 다시 상승 반전이 확인될 때만 진입.
'20MA 터치 즉시 매수' 같은 순진한 룰의 함정을 피하도록, 트렌드 확인·반전 확인·
RSI 과열 배제 세 가지 필터를 모두 통과해야 한다.
"""
from __future__ import annotations

from typing import Optional

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class DayPullback(Strategy):
    name = "day_pullback"

    def __init__(self, fast: int = 20, slow: int = 60, rsi_p: int = 14,
                 atr_p: int = 14, pull_atr: float = 1.0):
        self.fast = fast
        self.slow = slow
        self.rsi_p = rsi_p
        self.atr_p = atr_p
        self.pull_atr = pull_atr
        self.warmup = max(fast, slow, rsi_p, atr_p) + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        bars = list(ctx.bars[: ctx.at + 1])
        closes = ind.closes(bars)
        cur: Bar = bars[-1]
        prev: Bar = bars[-2]
        ma_fast = ind.sma(closes, self.fast)[-1]
        ma_slow = ind.sma(closes, self.slow)[-1]
        rsi = ind.rsi(closes, self.rsi_p)[-1]
        atr_val = ind.atr(bars, self.atr_p)[-1]
        if None in (ma_fast, ma_slow, rsi, atr_val) or atr_val <= 0:
            return StrategyResult.hold("nan")

        trend_ok = ma_fast > ma_slow and cur.close > ma_slow
        pulled = prev.low <= ma_fast + self.pull_atr * atr_val and prev.close < ma_fast + 0.3 * atr_val
        reversal = cur.close > prev.high and cur.close > ma_fast
        rsi_ok = 35 <= rsi <= 65
        if not (trend_ok and pulled and reversal and rsi_ok):
            return StrategyResult.hold("no-pullback")

        strength = ind.clip(0.45 + (65 - abs(rsi - 50)) / 130, 0.4, 0.9)
        stop = min(prev.low, ma_fast - 0.2 * atr_val)
        target = cur.close + 2.0 * (cur.close - stop)
        return StrategyResult(
            Signal(Side.BUY, strength, f"pullback ma{self.fast} rsi{rsi:.0f}"),
            stop_hint=stop, target_hint=target,
        )
