"""휴대전화에서 빨리 읽을 수 있는 한국어 아침 요약."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Sequence

from .models import MarketEvent, ShadowDecision


SEVERITY_ORDER = {"high": 0, "watch": 1, "info": 2}


def render_briefing(events: Iterable[MarketEvent], *,
                    decisions: Sequence[ShadowDecision] = (),
                    generated_at: datetime,
                    holdings: Sequence[str] = (),
                    max_per_region: int = 5) -> str:
    items = list(events)
    held = {symbol.upper() for symbol in holdings}
    items.sort(key=lambda e: (
        0 if e.symbol.upper() in held else 1,
        SEVERITY_ORDER.get(e.severity, 9),
        -e.published_at.timestamp(),
    ))
    lines = [f"[{generated_at:%Y-%m-%d} 아침 시장 요약]",
             "※ 정보·그림자 판단 전용, 실제 주문에는 미반영"]
    for region, label in (("KR", "한국"), ("US", "미국")):
        selected = [event for event in items if event.region == region]
        lines.extend(("", f"{label} 주요 이슈"))
        if not selected:
            lines.append("- 신규 수집 정보 없음")
            continue
        for event in selected[:max_per_region]:
            mark = "위험" if event.severity == "high" else (
                "주의" if event.severity == "watch" else "정보")
            who = event.symbol or event.company or "시장"
            lines.append(f"- [{mark}] {who}: {event.title}")
            if event.url:
                lines.append(f"  {event.url}")
    risky = [d for d in decisions if d.shadow_action != "allow"]
    lines.extend(("", "그림자 판단"))
    if not risky:
        lines.append("- 위험 보류 후보 없음")
    else:
        for decision in risky:
            label = "가상 차단" if decision.shadow_action == "would_block" else "검토"
            lines.append(f"- {decision.symbol}: {label} (실제 주문 미반영)")
    return "\n".join(lines)
