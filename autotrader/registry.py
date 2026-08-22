"""StrategyRegistry — "검증된 전략만 실행" 게이트.

블로그 참고글 Q3 의 원칙: **전략 수립 → 전략 검증 → 자동화**. 이 순서가
깨지지 않게, live 에서는 "최근 백테스트가 통과 기준을 만족한" 전략만 활성화
할 수 있게 하는 얇은 레지스트리다.

승인 기준 (기본):
- OOS Profit Factor ≥ 1.20
- OOS 트레이드 수 ≥ 20  (통계적 신뢰)
- OOS Max Drawdown ≥ -0.25  (25% 이내)
파일 포맷은 JSON 이며 각 항목이 하나의 전략 백테스트 결과 스냅샷이다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


@dataclass
class ValidationThresholds:
    min_oos_profit_factor: float = 1.20
    min_oos_trades: int = 20
    max_oos_drawdown: float = -0.25   # 값 자체는 음수. more negative 이면 불합격.
    max_age_days: int = 90            # 이보다 오래된 결과는 만료로 간주


@dataclass
class StrategyRecord:
    name: str
    validated_at: datetime
    oos_profit_factor: float
    oos_trades: int
    oos_max_drawdown: float
    notes: str = ""

    def is_valid(self, th: ValidationThresholds, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        if (now - self.validated_at) > timedelta(days=th.max_age_days):
            return False
        if self.oos_trades < th.min_oos_trades:
            return False
        # profit_factor 가 inf 인 경우도 정상 통과로 본다
        if self.oos_profit_factor < th.min_oos_profit_factor:
            return False
        if self.oos_max_drawdown < th.max_oos_drawdown:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "validated_at": self.validated_at.isoformat(),
            "oos_profit_factor": self.oos_profit_factor,
            "oos_trades": self.oos_trades,
            "oos_max_drawdown": self.oos_max_drawdown,
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
