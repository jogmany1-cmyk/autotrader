"""실제 주문과 분리된 뉴스 위험 그림자 판단."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional, Sequence

from .models import MarketEvent, ShadowDecision


class ShadowPolicy:
    """현재는 기록만 한다. 반환값을 주문 허용 여부로 사용하면 안 된다."""

    def evaluate(self, symbol: str, baseline_action: str,
                 events: Sequence[MarketEvent], *,
                 now: Optional[datetime] = None) -> ShadowDecision:
        related = [e for e in events if e.symbol.upper() == symbol.upper()]
        official_high = [e for e in related if e.official and e.severity == "high"]
        watched = [e for e in related if e.severity in ("watch", "high")]
        if baseline_action.upper() != "BUY":
            action = "allow"
            chosen: List[MarketEvent] = []
        elif official_high:
            action, chosen = "would_block", official_high
        elif watched:
            action, chosen = "review", watched
        else:
            action, chosen = "allow", []
        return ShadowDecision(
            symbol=symbol, baseline_action=baseline_action.upper(),
            shadow_action=action,
            reasons=[f"{e.source}: {e.title}" for e in chosen[:5]],
            event_ids=[e.id for e in chosen], evaluated_at=now,
        )

    def evaluate_many(self, symbols: Iterable[str], baseline_action: str,
                      events: Sequence[MarketEvent], *,
                      now: Optional[datetime] = None) -> List[ShadowDecision]:
        return [self.evaluate(symbol, baseline_action, events, now=now)
                for symbol in symbols]
