"""팩터 기반 종목 스크리너.

시스템 전체가 매일 처리하는 종목 수를 통제 가능한 크기로 만드는 관문이다.
여러 팩터 점수를 z-score 로 정규화해 합산하고, 최소 유동성·최소 가격 등 하드
필터를 통과한 종목만 top-N 을 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

from . import indicators as ind
from .config import Universe
from .data.base import DataProvider
from .models import Bar, ScreenResult


@dataclass
class FactorWeights:
    momentum_120: float = 1.2       # 6개월 수익률
    momentum_20: float = 1.0        # 1개월 수익률
    trend: float = 1.0              # 200일 이평 대비 위치
    liquidity: float = 0.6          # 평균 거래대금 (로그화)
    low_vol: float = 0.4            # 저변동성 프리미엄 (음의 부호)


class Screener:
    def __init__(self, provider: DataProvider, universe: Universe,
                 weights: FactorWeights | None = None, top_n: int = 20):
        self.provider = provider
        self.universe = universe
        self.weights = weights or FactorWeights()
        self.top_n = top_n

    def rank(self, symbols: Sequence[str] | None = None,
             lookback: int | None = None) -> List[ScreenResult]:
        symbols = list(symbols) if symbols else self.universe.symbols or self.provider.universe()
        lookback = lookback or self.universe.lookback_days
        raw: Dict[str, Dict[str, float]] = {}
        rejects: Dict[str, str] = {}

        for sym in symbols:
            try:
                bars = self.provider.history(sym, limit=lookback)
            except Exception as exc:  # pragma: no cover — 데이터 오류는 조용히 스킵
                rejects[sym] = f"data: {exc}"
                continue
            if len(bars) < 210:
                rejects[sym] = "insufficient-history"
                continue
            last = bars[-1]
            if last.close < self.universe.min_price:
                rejects[sym] = "low-price"
                continue
            adv = _avg_dollar_vol(bars, 20)
            if adv < self.universe.min_avg_dollar_vol:
                rejects[sym] = f"low-liquidity {adv:.0f}"
                continue
            raw[sym] = _compute_factors(bars)

        if not raw:
            return [ScreenResult(s, 0.0, {}, False, r) for s, r in rejects.items()]

        # 각 팩터 컬럼을 단면 z-score 로 표준화한 뒤 가중 합산.
        keys = ["momentum_120", "momentum_20", "trend", "liquidity", "low_vol"]
        cols = {k: [raw[s][k] for s in raw] for k in keys}
        zcols = {k: ind.zscore(cols[k]) for k in keys}
        results: List[ScreenResult] = []
        for i, sym in enumerate(raw):
            score = 0.0
            factors_out: Dict[str, float] = {}
            for k in keys:
                z = zcols[k][i] or 0.0
                w = getattr(self.weights, k)
                if k == "low_vol":
                    z = -z  # 낮을수록 좋음
                score += w * z
                factors_out[k] = z
            results.append(ScreenResult(sym, round(score, 4), factors_out, True, ""))

        results.sort(key=lambda r: r.score, reverse=True)
        top = results[: self.top_n]
        # 탈락 정보도 함께 넘겨 감사 로그에 남긴다.
        top.extend(ScreenResult(s, 0.0, {}, False, r) for s, r in rejects.items())
        return top


def _avg_dollar_vol(bars: Sequence[Bar], period: int) -> float:
    win = bars[-period:]
    if not win:
        return 0.0
    return sum(b.close * b.volume for b in win) / len(win)


def _compute_factors(bars: Sequence[Bar]) -> Dict[str, float]:
    import math
    closes = [b.close for b in bars]
    mom120 = closes[-1] / closes[-121] - 1.0
    mom20 = closes[-1] / closes[-21] - 1.0
    ma200 = sum(closes[-200:]) / 200
    trend = closes[-1] / ma200 - 1.0
    adv = _avg_dollar_vol(bars, 20)
    liquidity = math.log1p(max(adv, 1.0))
    vol = ind.realized_vol(closes, 20)[-1] or 1.0
    return {
        "momentum_120": mom120,
        "momentum_20": mom20,
        "trend": trend,
        "liquidity": liquidity,
        "low_vol": vol,
    }
