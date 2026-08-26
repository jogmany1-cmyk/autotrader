"""뉴스·공시를 결정론적으로 분류한다. AI의 자유로운 해석은 주문에 쓰지 않는다."""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable, List

from .models import MarketEvent


HIGH_RISK_TERMS = (
    "거래정지", "상장폐지", "회생절차", "파산", "부도", "횡령", "배임",
    "감사의견 거절", "감사의견거절", "유상증자", "무상감자", "관리종목",
)
WATCH_TERMS = (
    "전환사채", "신주인수권", "소송", "영업손실", "적자전환", "최대주주 변경",
    "실적", "잠정", "합병", "분할", "8-k", "6-k",
)


def classify_event(event: MarketEvent) -> MarketEvent:
    """원문 제목 기반의 보수적 분류.

    같은 단어라도 일반 뉴스는 오보·재전송 가능성이 있어 자동 차단 근거로
    승격하지 않는다. 공식 공시일 때만 high 를 유지한다.
    """
    text = f"{event.title} {event.summary}".casefold()
    matched_high = [term for term in HIGH_RISK_TERMS if term.casefold() in text]
    matched_watch = [term for term in WATCH_TERMS if term.casefold() in text]
    tags = list(dict.fromkeys([*event.tags, *matched_high, *matched_watch]))
    if matched_high:
        severity = "high" if event.official else "watch"
    elif matched_watch or any(tag in ("8-K", "6-K") for tag in event.tags):
        severity = "watch"
    else:
        severity = "info"
    return replace(event, severity=severity, tags=tags)


def classify_all(events: Iterable[MarketEvent]) -> List[MarketEvent]:
    return [classify_event(event) for event in events]
