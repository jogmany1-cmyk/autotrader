"""DAY-01 · 거래대금 돌파형.

Donchian 상단 돌파 + 거래량 스파이크 + 최소 변동성. 실전 매매에서 자주 쓰이는
가장 단순한 추세 진입 규칙 중 하나이며, 파라미터가 적어 과최적화 위험이 낮다.
"""
from __future__ import annotations

from typing import Optional

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class DayBreakout(Strategy):
    name = "day_breakout"

    def __init__(self, breakout_period: int = 20, vol_period: int = 20,
                 vol_multiplier: float = 1.5, atr_period: int = 14,
                 atr_mult_stop: float = 1.5, atr_mult_target: float = 3.0):
        self.breakout_period = breakout_period
        self.vol_period = vol_period
        self.vol_multiplier = vol_multiplier
        self.atr_period = atr_period
        self.atr_mult_stop = atr_mult_stop
        self.atr_mult_target = atr_mult_target
        self.warmup = max(breakout_period, vol_period, atr_period) + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        bars = list(ctx.bars[: ctx.at + 1])
        cur: Bar = bars[-1]
        highs = [b.high for b in bars]
        vols = [b.volume for b in bars]
        # 직전 breakout_period 봉의 최고가 (당일 제외)
        prior_high = max(highs[-1 - self.breakout_period:-1])
        avg_vol = sum(vols[-1 - self.vol_period:-1]) / self.vol_period
        atr_series = ind.atr(bars, self.atr_period)
        atr_val = atr_series[-1]
        if atr_val is None or atr_val <= 0:
            return StrategyResult.hold("no-atr")

        broke_out = cur.close > prior_high
        vol_ok = cur.volume >= avg_vol * self.vol_multiplier if avg_vol > 0 else False
        if not (broke_out and vol_ok):
            return StrategyResult.hold("no-breakout")

        # 강도: 돌파 폭을 ATR 로 정규화. 큰 폭 돌파일수록 확신도 상승.
        edge = (cur.close - prior_high) / atr_val
        strength = ind.clip(0.4 + edge * 0.4, 0.4, 0.95)
        stop = cur.close - self.atr_mult_stop * atr_val
        target = cur.close + self.atr_mult_target * atr_val
        return StrategyResult(
            Signal(Side.BUY, strength, f"breakout>{prior_high:.2f} v*{cur.volume/max(avg_vol,1):.1f}"),
            stop_hint=stop, target_hint=target,
        )
