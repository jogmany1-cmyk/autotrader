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
# 쿨다운을 면제할 청산 사유. 다만 **이익으로 끝났을 때만** 면제한다 —
# `register_exit` 의 pnl 인자를 보라.
#
# "trail" 을 면제 목록에 둔 것은 트레일링이 이익을 확정하는 장치라는 전제였다.
# 그런데 트레일링 스탑은 손실로도 걸린다(진입 직후 밀리면 -4%, 갭다운이면 그
# 이상). 사유만 보고 면제하면 떨어지는 종목에 다음 날 바로 재진입한다.
COOLDOWN_EXEMPT_REASONS: Set[str] = {"target", "trail"}


@dataclass
class CooldownRegistry:
    default_bars: int = 3
    entries: Dict[str, date] = field(default_factory=dict)  # symbol → cooldown expires ON this date (inclusive)

    def register_exit(self, symbol: str, exit_reason: str, on: date,
                      bars: int | None = None,
                      pnl: float | None = None) -> None:
        """청산을 기록하고 필요하면 쿨다운을 건다.

        쿨다운의 목적은 "방금 나를 밀어낸 종목에 곧바로 다시 들어가지 않는 것"
        이다. 그래서 **손실로 끝난 청산은 사유와 무관하게** 쿨다운을 건다.
        사유만 보고 면제하면, 손실로 걸린 트레일링 스탑 뒤에 떨어지는 종목으로
        다음 날 바로 되돌아간다.

        `pnl` 을 주지 않으면 예전처럼 사유만으로 판단한다 — 호출부가 손익을
        모르는 경우(수동 청산 등)를 위한 여지다.
        """
        exempt = exit_reason in COOLDOWN_EXEMPT_REASONS
        if exempt and (pnl is None or pnl > 0):
            return
        days = self.default_bars if bars is None else bars
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
