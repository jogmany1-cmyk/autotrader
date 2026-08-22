"""AI 예측 정확도 추적기.

블로그 후기 개선판 ⑦: "AI가 이거 사세요, 목표가 12% 상승" 이라고 했을 때
실제로 맞았는지 틀렸는지 추적.

기록:
- 진입 시점의 앙상블 예측 (신뢰도·목표가·손절가·전략별 스코어).
- 청산 시점의 실제 수익률·목표가 도달 여부·청산 사유.
집계:
- 승률, 평균 수익률, 목표가 도달률, 신뢰도 구간별 성과.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Prediction:
    symbol: str
    entry_ts: datetime
    entry_price: float
    confidence: float          # 0..1 (앙상블 score)
    votes: int
    target_price: float
    stop_price: float
    reason: str
    factor_detail: Dict[str, float] = field(default_factory=dict)


@dataclass
class Outcome:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    return_pct: float
    hit_target: bool
    hit_stop: bool
    exit_reason: str
    confidence: float


@dataclass
class AccuracyReport:
    n: int
    win_rate: float
    avg_return: float
    target_hit_rate: float
    stop_hit_rate: float
    by_confidence_bucket: Dict[str, Dict[str, float]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PredictionTracker:
    """진입 예측을 저장하고 청산 시 실측 결과와 대조."""

    def __init__(self) -> None:
        self._open: Dict[str, Prediction] = {}
        self._outcomes: List[Outcome] = []

    # ------------------------------------------------------------------ API
    def record_entry(self, pred: Prediction) -> None:
        self._open[pred.symbol] = pred

    def record_exit(self, *, symbol: str, exit_ts: datetime, exit_price: float,
                    exit_reason: str) -> Optional[Outcome]:
        pred = self._open.pop(symbol, None)
        if pred is None:
            return None
        entry = pred.entry_price
        ret = exit_price / entry - 1.0 if entry > 0 else 0.0
        hit_target = (
            pred.target_price > entry and exit_price >= pred.target_price
        )
        hit_stop = (
            pred.stop_price > 0 and exit_price <= pred.stop_price
        )
        out = Outcome(
            symbol=symbol, entry_ts=pred.entry_ts, exit_ts=exit_ts,
            entry_price=entry, exit_price=exit_price, return_pct=round(ret, 6),
            hit_target=hit_target, hit_stop=hit_stop, exit_reason=exit_reason,
            confidence=pred.confidence,
        )
        self._outcomes.append(out)
        return out

    # ------------------------------------------------------------ analytics
    def report(self) -> AccuracyReport:
        n = len(self._outcomes)
        if not n:
            return AccuracyReport(0, 0.0, 0.0, 0.0, 0.0, {})
        wins = sum(1 for o in self._outcomes if o.return_pct > 0)
        avg_ret = sum(o.return_pct for o in self._outcomes) / n
        tgt = sum(1 for o in self._outcomes if o.hit_target) / n
        stp = sum(1 for o in self._outcomes if o.hit_stop) / n

        buckets: Dict[str, List[Outcome]] = {}
        for o in self._outcomes:
            key = _bucket(o.confidence)
            buckets.setdefault(key, []).append(o)
        by_bucket = {
            k: {
                "n": len(v),
                "win_rate": round(sum(1 for x in v if x.return_pct > 0) / len(v), 4),
                "avg_return": round(sum(x.return_pct for x in v) / len(v), 4),
            }
            for k, v in buckets.items()
        }
        return AccuracyReport(
            n=n,
            win_rate=round(wins / n, 4),
            avg_return=round(avg_ret, 4),
            target_hit_rate=round(tgt, 4),
            stop_hit_rate=round(stp, 4),
            by_confidence_bucket=by_bucket,
        )

    def outcomes(self) -> List[Outcome]:
        return list(self._outcomes)

    # ----------------------------------------------------------- persistence
    def dump_json(self, path: str) -> None:
        payload = {
            "open": {s: _pred_json(p) for s, p in self._open.items()},
            "outcomes": [_out_json(o) for o in self._outcomes],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)


def _bucket(conf: float) -> str:
    edges = [0.5, 0.6, 0.7, 0.8, 0.9]
    labels = ["<50", "50-60", "60-70", "70-80", "80-90", ">=90"]
    for i, e in enumerate(edges):
        if conf < e:
            return labels[i]
    return labels[-1]


def _pred_json(p: Prediction) -> Dict[str, Any]:
    d = asdict(p)
    d["entry_ts"] = p.entry_ts.isoformat()
    return d


def _out_json(o: Outcome) -> Dict[str, Any]:
    d = asdict(o)
    d["entry_ts"] = o.entry_ts.isoformat()
    d["exit_ts"] = o.exit_ts.isoformat()
    return d
