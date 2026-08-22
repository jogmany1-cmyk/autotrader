"""재매수 쿨다운 관리.

블로그 후기 개선판 ③: **익절로 판 종목은 쿨다운 없음, 손절/AI 매도만 N일 쿨다운**.
"쿨다운은 급락을 쫓지 않기 위한 안전 그물이지만, 익절까지 막으면 오히려 기회
를 놓친다"는 관찰에서 나온 규칙.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, Set


COOLDOWN_TRIGGERING_REASONS: Set[str] = {"stop", "hard_stop", "ai_sell", "time"}
COOLDOWN_EXEMPT_REASONS: Set[str] = {"target", "trail"}


@dataclass
class CooldownRegistry:
    default_bars: int = 3
    entries: Dict[str, date] = field(default_factory=dict)  # symbol → cooldown expires ON this date (inclusive)

    def register_exit(self, symbol: str, exit_reason: str, on: date,
                      bars: int | None = None) -> None:
        if exit_reason in COOLDOWN_EXEMPT_REASONS:
            return
        days = self.default_bars if bars is None else bars
        # 익절 예외 외의 청산에만 쿨다운 부여
        self.entries[symbol] = on + timedelta(days=days)

    def is_blocked(self, symbol: str, today: date) -> bool:
        exp = self.entries.get(symbol)
        return exp is not None and today <= exp

    def clear(self, symbol: str) -> None:
        self.entries.pop(symbol, None)

    def purge_expired(self, today: date) -> None:
        stale = [s for s, exp in self.entries.items() if exp < today]
        for s in stale:
            self.entries.pop(s, None)

    def as_dict(self) -> Dict[str, str]:
        return {s: exp.isoformat() for s, exp in self.entries.items()}
