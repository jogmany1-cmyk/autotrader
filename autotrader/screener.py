"""팩터 기반 종목 스크리너.

시스템 전체가 매일 처리하는 종목 수를 통제 가능한 크기로 만드는 관문이다.
여러 팩터 점수를 z-score 로 정규화해 합산하고, 최소 유동성·최소 가격 등 하드
필터를 통과한 종목만 top-N 을 남긴다.

**조건식 3-티어 (블로그 참고글 §4)**:
    ① 시세부(price)     : 당일 시가/고가/저가/현재가 — 가장 저렴
    ② 지표부(indicator) : 일봉 시계열 필요 (이평·RSI·MACD 등) — 중간
    ③ 순위부(ranking)   : 전 종목 대비 상대 순위 — 개별로는 판정 불가, 가장 비쌈
싼 판정을 먼저 돌려 후보를 줄이면 API 조회 한도를 넘겨 연결이 끊기는 사고를
예방하고, 어느 단계에서 몇 개가 걸러졌는지 감사 로그로 남길 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from . import indicators as ind
from .config import Universe
from .data.base import DataProvider
from .models import Bar, ScreenResult


@dataclass
class ScreenStageStats:
    """각 티어를 통과한 종목 수와 (지면 절약을 위해) 앞 몇 개 탈락 사유.

    수만 종목을 스크리닝할 때 어느 단계에서 후보가 얼마나 줄었는지 눈에 보이는
    것이 개발·디버깅에 결정적이다."""
    input_count: int
    tier1_price_pass: int = 0
    tier2_indicator_pass: int = 0
    tier3_ranking_pass: int = 0
    reject_samples: Dict[str, str] = field(default_factory=dict)

    def as_line(self) -> str:
        return (f"[SCREEN] in={self.input_count} "
                f"tier1(price)={self.tier1_price_pass} "
                f"tier2(indicator)={self.tier2_indicator_pass} "
                f"tier3(ranking)={self.tier3_ranking_pass}")


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
        self.last_stats: ScreenStageStats | None = None

    def rank(self, symbols: Sequence[str] | None = None,
             lookback: int | None = None) -> List[ScreenResult]:
        symbols = list(symbols) if symbols else self.universe.symbols or self.provider.universe()
        lookback = lookback or self.universe.lookback_days
        stats = ScreenStageStats(input_count=len(symbols))
        raw: Dict[str, Dict[str, float]] = {}
        rejects: Dict[str, str] = {}
        bars_cache: Dict[str, List[Bar]] = {}

        # -- Tier 1 : 시세부 (당일 종가·기본 유동성·데이터 존재성) --------------------
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
            bars_cache[sym] = bars
        stats.tier1_price_pass = len(bars_cache)

        # -- Tier 2 : 지표부 (일봉 시계열이 필요한 팩터들) --------------------------
        for sym, bars in bars_cache.items():
            try:
                raw[sym] = _compute_factors(bars)
            except Exception as exc:  # pragma: no cover
                rejects[sym] = f"indicator: {exc}"
        stats.tier2_indicator_pass = len(raw)

        # 감사 로그용 대표 사유 몇 개만
        for i, (sym, reason) in enumerate(rejects.items()):
            if i >= 5:
                break
            stats.reject_samples[sym] = reason

        self.last_stats = stats

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
        stats.tier3_ranking_pass = len(top)
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
