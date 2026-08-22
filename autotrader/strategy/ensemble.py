"""여러 전략을 가중 합산해 한 종목에 대한 최종 판단을 만드는 앙상블.

각 전략은 STRENGTH (0..1) 를 리턴하며, 앙상블은 가중 평균으로 종합 점수를
계산한다. threshold 를 넘고, 최소 몇 개 이상의 전략이 동시에 매수 의견일 때만
BUY 를 낸다 — 단일 전략의 우연한 신호를 걸러 낸다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ..config import StrategyWeights
from ..models import Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


@dataclass
class EnsembleDecision:
    signal: Signal
    score: float
    votes: int
    stop_hint: float
    target_hint: float
    detail: Dict[str, float]


class Ensemble:
    def __init__(self, strategies: Sequence[Strategy], weights: StrategyWeights,
                 threshold: float = 0.5, min_votes: int = 1):
        self.strategies = list(strategies)
        self.weights = weights
        self.threshold = threshold
        self.min_votes = min_votes

    def evaluate(self, ctx: StrategyContext) -> EnsembleDecision:
        total_w = 0.0
        weighted = 0.0
        votes = 0
        stops: List[Tuple[float, float]] = []
        targets: List[Tuple[float, float]] = []
        detail: Dict[str, float] = {}
        reasons: List[str] = []
        for strat in self.strategies:
            w = getattr(self.weights, strat.name, 0.0) or 0.0
            if w <= 0:
                continue
            res = strat.evaluate(ctx)
            sig = res.signal.clamped()
            detail[strat.name] = sig.strength if sig.side is Side.BUY else 0.0
            total_w += w
            if sig.side is Side.BUY and sig.strength > 0:
                weighted += w * sig.strength
                votes += 1
                if res.stop_hint is not None:
                    stops.append((res.stop_hint, w))
                if res.target_hint is not None:
                    targets.append((res.target_hint, w))
                reasons.append(f"{strat.name}:{sig.strength:.2f}")
        score = weighted / total_w if total_w > 0 else 0.0
        if score < self.threshold or votes < self.min_votes:
            return EnsembleDecision(
                Signal.hold(f"score {score:.2f} votes {votes}"),
                score=score, votes=votes,
                stop_hint=0.0, target_hint=0.0, detail=detail,
            )
        stop = _weighted(stops) if stops else 0.0
        target = _weighted(targets) if targets else 0.0
        return EnsembleDecision(
            Signal(Side.BUY, score, ", ".join(reasons)),
            score=score, votes=votes,
            stop_hint=stop, target_hint=target, detail=detail,
        )


def _weighted(pairs: Sequence[Tuple[float, float]]) -> float:
    total = sum(w for _, w in pairs)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in pairs) / total
