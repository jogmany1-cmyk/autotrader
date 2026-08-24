"""진입 신호에 우위가 있는지 측정 — 청산 규칙을 끄고 신호만 본다.

## 왜 필요한가

백테스트 성적이 나쁘면 원인이 둘 중 하나다. **진입 신호에 우위가 없거나**,
**청산 규칙이 신호를 죽이거나.** 둘은 정반대의 대응을 요구하는데, 백테스트
숫자만 봐서는 구분되지 않는다.

실제로 이 저장소에서 그 일이 있었다. 20종목 백테스트가 OOS -1.47% (PF 0.59)
였고 손절 청산이 72%를 차지했다. 손절을 조여야 하나 풀어야 하나를 두고
설정을 이리저리 바꿔보는 것은 과최적화로 가는 지름길이다. 대신 신호만 따로
재보니

    지평선 10일: 신호 +2.74% vs 기준선 +0.72%  (우위 +2.02%, t=2.81)

신호에는 우위가 있었다. 청산이 문제였다. 이 측정이 없었다면 진입 로직을
헛되이 고쳤을 것이다.

## 어떻게 재는가

신호가 뜬 다음 봉 시가에 들어가서 N일 뒤 종가까지의 수익률을 잰다. 손절도
목표가도 없다. 체결 모델(오늘 종가 판단 → 다음 봉 시가 체결)은 백테스터와
같게 맞춘다.

**기준선과의 차이만이 우위다.** 시장이 그냥 올랐으면 아무 날에나 들어가도
수익이 난다. 그래서 같은 종목·같은 기간의 **모든 봉**을 기준선으로 삼고,
신호 수익률에서 기준선 수익률을 뺀 값을 우위로 본다.

## 읽는 법

- **t값**: 우위가 우연일 확률의 척도. 2를 넘으면 유의하다고 본다. 표본이
  작으면 우위가 커 보여도 t값은 낮다 — 그 경우 아직 모르는 것이다.
- **점수 구간별 일관성**: 앙상블 점수가 높을수록 수익이 커야 점수가 의미 있다.
  구간별 차이가 없으면 점수로 포지션 크기를 조절할 근거가 없다.
- **역행 비율**: 우위가 있는 신호도 도중에 밀린다. 손절 폭이 이 비율을
  견디지 못하면 이길 자리에서 잘려나간다.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .data.base import DataError, DataProvider
from .models import Side
from .strategy import (DayBreakout, DayMomentum, DayPullback, Ensemble,
                       MeanReversion, SwingTrend)
from .strategy.base import StrategyContext

DEFAULT_HORIZONS: Tuple[int, ...] = (1, 5, 10, 20)
# 손절 폭이 신호를 견디는지 보기 위한 역행 기준선.
DEFAULT_ADVERSE_LEVELS: Tuple[float, ...] = (0.02, 0.03, 0.05, 0.10)
# t값 판정 기준. 2 는 관례적인 유의 수준(약 95%)이다.
T_SIGNIFICANT = 2.0
T_WEAK = 1.0


@dataclass
class HorizonEdge:
    """지평선 하나에 대한 측정 결과."""
    horizon: int
    n_signals: int
    n_baseline: int
    signal_mean: float
    baseline_mean: float
    signal_win_rate: float
    baseline_win_rate: float
    signal_std: float

    @property
    def edge(self) -> float:
        return self.signal_mean - self.baseline_mean

    @property
    def t_stat(self) -> float:
        """우위 / 표준오차. 표본이 작으면 우위가 커도 t 는 작다."""
        if self.n_signals < 2 or self.signal_std <= 0:
            return 0.0
        return self.edge / (self.signal_std / math.sqrt(self.n_signals))

    @property
    def verdict(self) -> str:
        t = abs(self.t_stat)
        if t >= T_SIGNIFICANT:
            return "유의"
        if t >= T_WEAK:
            return "약함"
        return "우연과 구분 불가"

    def as_line(self) -> str:
        return (f"{self.horizon:>4}일 {self.signal_mean:>+9.3%} "
                f"{self.baseline_mean:>+9.3%} {self.edge:>+9.3%} "
                f"{self.t_stat:>7.2f}  {self.verdict}")

    def as_dict(self) -> Dict:
        return {"horizon": self.horizon, "n_signals": self.n_signals,
                "n_baseline": self.n_baseline, "signal_mean": self.signal_mean,
                "baseline_mean": self.baseline_mean, "edge": self.edge,
                "t_stat": self.t_stat, "verdict": self.verdict,
                "signal_win_rate": self.signal_win_rate,
                "baseline_win_rate": self.baseline_win_rate}


@dataclass
class ScoreBucket:
    """앙상블 점수 구간별 평균 수익 — 점수가 의미 있는지 본다."""
    lo: float
    hi: float
    n: int
    means: Dict[int, float] = field(default_factory=dict)


@dataclass
class EdgeReport:
    n_bars: int
    n_signals: int
    threshold: float
    horizons: List[HorizonEdge] = field(default_factory=list)
    buckets: List[ScoreBucket] = field(default_factory=list)
    adverse: Dict[float, float] = field(default_factory=dict)  # 기준 → 비율
    adverse_horizon: int = 0

    @property
    def signal_rate(self) -> float:
        return self.n_signals / self.n_bars if self.n_bars else 0.0

    def horizon(self, h: int) -> Optional[HorizonEdge]:
        for e in self.horizons:
            if e.horizon == h:
                return e
        return None

    def best_t(self) -> float:
        return max((e.t_stat for e in self.horizons), default=0.0)

    def summary(self) -> str:
        return (f"봉 {self.n_bars:,}개 · 신호 {self.n_signals:,}건 "
                f"({self.signal_rate:.2%}) · 최대 t={self.best_t():.2f}")

    def as_dict(self) -> Dict:
        return {"n_bars": self.n_bars, "n_signals": self.n_signals,
                "threshold": self.threshold, "signal_rate": self.signal_rate,
                "horizons": [h.as_dict() for h in self.horizons],
                "buckets": [{"lo": b.lo, "hi": b.hi, "n": b.n,
                             "means": {str(k): v for k, v in b.means.items()}}
                            for b in self.buckets],
                "adverse_horizon": self.adverse_horizon,
                "adverse": {str(k): v for k, v in self.adverse.items()}}


def default_ensemble(config: Config, threshold: float, min_votes: int = 1) -> Ensemble:
    """백테스터와 같은 전략 구성. 둘이 갈라지면 측정이 무의미해진다."""
    return Ensemble([DayBreakout(), DayPullback(), DayMomentum(), SwingTrend(),
                     MeanReversion()], config.weights,
                    threshold=threshold, min_votes=min_votes)


class EdgeAnalyzer:
    def __init__(self, provider: DataProvider, config: Config,
                 ensemble: Optional[Ensemble] = None,
                 threshold: float = 0.55, min_votes: int = 1,
                 horizons: Sequence[int] = DEFAULT_HORIZONS,
                 warmup: int = 250,
                 adverse_levels: Sequence[float] = DEFAULT_ADVERSE_LEVELS):
        self.provider = provider
        self.config = config
        self.threshold = threshold
        self.ensemble = ensemble or default_ensemble(config, threshold, min_votes)
        self.horizons = tuple(sorted(horizons))
        self.warmup = warmup
        self.adverse_levels = tuple(adverse_levels)

    def run(self, symbols: Optional[Sequence[str]] = None,
            bars: int = 0, buckets: int = 3) -> EdgeReport:
        symbols = list(symbols) if symbols else self.provider.universe()
        max_h = max(self.horizons)

        sig: Dict[int, List[float]] = {h: [] for h in self.horizons}
        base: Dict[int, List[float]] = {h: [] for h in self.horizons}
        scored: List[Tuple[float, Dict[int, float]]] = []
        adverse_hits = {lv: 0 for lv in self.adverse_levels}
        n_bars = 0

        for sym in symbols:
            try:
                series = self.provider.history(sym, limit=bars)
            except DataError:
                continue
            # 워밍업(지표 계산) + 지평선만큼은 남겨둬야 미래 수익률을 잴 수 있다.
            last = len(series) - max_h - 1
            for i in range(self.warmup, last):
                entry = series[i + 1].open          # 오늘 종가 판단 → 다음 봉 시가
                if entry <= 0:
                    continue
                n_bars += 1
                for h in self.horizons:
                    base[h].append(series[i + 1 + h].close / entry - 1.0)

                decision = self.ensemble.evaluate(
                    StrategyContext(symbol=sym, bars=series, at=i))
                if decision.signal.side is not Side.BUY:
                    continue
                if decision.score < self.threshold:
                    continue

                rets = {}
                for h in self.horizons:
                    r = series[i + 1 + h].close / entry - 1.0
                    sig[h].append(r)
                    rets[h] = r
                scored.append((decision.score, rets))

                # 역행: 가장 짧지 않은 지평선 안에서 저가가 얼마나 밀렸는가
                ah = self._adverse_horizon()
                worst = min(b.low for b in series[i + 1:i + 1 + ah + 1])
                for lv in self.adverse_levels:
                    if worst / entry - 1.0 <= -lv:
                        adverse_hits[lv] += 1

        rep = EdgeReport(n_bars=n_bars, n_signals=len(scored),
                         threshold=self.threshold,
                         adverse_horizon=self._adverse_horizon())
        for h in self.horizons:
            s, b = sig[h], base[h]
            if not s or not b:
                continue
            rep.horizons.append(HorizonEdge(
                horizon=h, n_signals=len(s), n_baseline=len(b),
                signal_mean=st.mean(s), baseline_mean=st.mean(b),
                signal_win_rate=sum(1 for x in s if x > 0) / len(s),
                baseline_win_rate=sum(1 for x in b if x > 0) / len(b),
                signal_std=st.pstdev(s) if len(s) > 1 else 0.0))
        rep.buckets = self._buckets(scored, buckets)
        if scored:
            rep.adverse = {lv: adverse_hits[lv] / len(scored)
                           for lv in self.adverse_levels}
        return rep

    def _adverse_horizon(self) -> int:
        """역행을 재는 기간. 가장 짧은 지평선은 너무 짧아 두 번째를 쓴다."""
        return self.horizons[1] if len(self.horizons) > 1 else self.horizons[0]

    def _buckets(self, scored, k: int) -> List[ScoreBucket]:
        if not scored or k < 1:
            return []
        rows = sorted(scored, key=lambda r: r[0])
        n = len(rows)
        out: List[ScoreBucket] = []
        for j in range(k):
            g = rows[j * n // k:(j + 1) * n // k]
            if not g:
                continue
            out.append(ScoreBucket(
                lo=g[0][0], hi=g[-1][0], n=len(g),
                means={h: st.mean(r[1][h] for r in g) for h in self.horizons}))
        return out
