"""StrategyRegistry — "검증된 전략만 실행" 게이트.

블로그 참고글 Q3 의 원칙: **전략 수립 → 전략 검증 → 자동화**. 이 순서가
깨지지 않게, live 에서는 "최근 백테스트가 통과 기준을 만족한" 전략만 활성화
할 수 있게 하는 얇은 레지스트리다.

승인 기준 (기본):
- OOS Profit Factor ≥ 1.20
- OOS 순수익 > 0        (비용 차감 후. 손실 전략은 절대 승인하지 않는다)
- OOS 트레이드 수 ≥ 50  (통계적 신뢰)
- OOS Max Drawdown ≥ -0.25  (25% 이내)
- 90일 이내 재검증

거래 수 하한이 20 이 아니라 50 인 이유는 실측 때문이다. 같은 전략·같은 데이터로
종목 수만 늘려가며 OOS Profit Factor 를 재보니

    거래  4건 → PF 20.93
    거래 10건 → PF  3.25
    거래 29건 → PF  0.84
    거래 40건 → PF  0.59

표본이 20건 안팎일 때의 PF 는 신호가 아니라 잡음이다. 옛 기준(PF≥1.20,
거래≥20)은 손실 전략을 통과시킨다 — 실제로 5종목 5,000봉 결과(PF 1.84,
거래 22건, 연 0.35% 수익, 비용 0.70%)가 통과했다.

순수익 조건이 따로 있는 이유: PF 는 이익합/손실합이라 비용을 반영한 뒤에도
1 을 넘을 수 있지만, 그것이 곧 돈을 벌었다는 뜻은 아니다. 실제로 벌었는지는
순수익으로만 확인된다.

파일 포맷은 JSON 이며 각 항목이 하나의 전략 백테스트 결과 스냅샷이다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from .market import now_kst


@dataclass
class ValidationThresholds:
    min_oos_profit_factor: float = 1.20
    min_oos_trades: int = 50          # 20 은 잡음을 신호로 오인하기에 충분히 작다
    min_oos_net_return: float = 0.0   # 비용 차감 후 순수익. 이 값 초과여야 통과.
    max_oos_drawdown: float = -0.25   # 값 자체는 음수. more negative 이면 불합격.
    max_age_days: int = 90            # 이보다 오래된 결과는 만료로 간주


@dataclass
class StrategyRecord:
    name: str
    validated_at: datetime
    oos_profit_factor: float
    oos_trades: int
    oos_max_drawdown: float
    # 비용 차감 후 OOS 순수익. None 은 "측정하지 않음" 이며 승인되지 않는다 —
    # 모르는 것을 통과시키는 것이 가장 위험하다. 옛 레코드는 이 값이 없으므로
    # 자동으로 재검증 대상이 된다.
    oos_net_return: Optional[float] = None
    notes: str = ""

    def is_valid(self, th: ValidationThresholds, now: Optional[datetime] = None) -> bool:
        now = now or now_kst()
        if (now - self.validated_at) > timedelta(days=th.max_age_days):
            return False
        if self.oos_trades < th.min_oos_trades:
            return False
        # profit_factor 가 inf 인 경우도 정상 통과로 본다
        if self.oos_profit_factor < th.min_oos_profit_factor:
            return False
        if self.oos_max_drawdown < th.max_oos_drawdown:
            return False
        # 순수익을 재지 않았으면 통과시키지 않는다. PF 가 1 을 넘어도 비용까지
        # 반영한 순수익이 음수일 수 있다.
        if self.oos_net_return is None:
            return False
        if self.oos_net_return <= th.min_oos_net_return:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "validated_at": self.validated_at.isoformat(),
            "oos_profit_factor": self.oos_profit_factor,
            "oos_trades": self.oos_trades,
            "oos_max_drawdown": self.oos_max_drawdown,
            "oos_net_return": self.oos_net_return,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategyRecord":
        return cls(
            name=d["name"],
            validated_at=datetime.fromisoformat(d["validated_at"]),
            oos_profit_factor=float(d["oos_profit_factor"]),
            oos_trades=int(d["oos_trades"]),
            oos_max_drawdown=float(d["oos_max_drawdown"]),
            oos_net_return=(None if d.get("oos_net_return") is None
                            else float(d["oos_net_return"])),
            notes=d.get("notes", ""),
        )


class StrategyRegistry:
    def __init__(self, path: Optional[str] = None,
                 thresholds: Optional[ValidationThresholds] = None):
        self.path = path
        self.thresholds = thresholds or ValidationThresholds()
        self._records: Dict[str, StrategyRecord] = {}
        if path and os.path.exists(path):
            self._load()

    # -------------------------------------------------------------- I/O
    def _load(self) -> None:
        assert self.path is not None
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for item in raw:
            rec = StrategyRecord.from_dict(item)
            self._records[rec.name] = rec

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("경로가 지정되지 않았습니다")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in self._records.values()],
                      fh, indent=2, ensure_ascii=False)

    # -------------------------------------------------------------- API
    def upsert(self, record: StrategyRecord) -> None:
        self._records[record.name] = record

    def record(self, name: str) -> Optional[StrategyRecord]:
        return self._records.get(name)

    def all_records(self) -> List[StrategyRecord]:
        return list(self._records.values())

    def is_validated(self, name: str, now: Optional[datetime] = None) -> bool:
        rec = self._records.get(name)
        return rec is not None and rec.is_valid(self.thresholds, now)

    def validated_names(self, now: Optional[datetime] = None) -> List[str]:
        return [r.name for r in self._records.values()
                if r.is_valid(self.thresholds, now)]
