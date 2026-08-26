"""수집→중복제거→위험분류→그림자판단→보고서의 독립 파이프라인."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Sequence

from ..notify import Notifier
from .briefing import render_briefing
from .dedupe import deduplicate
from .models import MarketEvent, ShadowDecision
from .risk import classify_all
from .shadow import ShadowPolicy
from .store import IntelligenceStore


Collector = Callable[[], Iterable[MarketEvent]]


@dataclass
class BriefingReport:
    generated_at: datetime
    events: List[MarketEvent]
    decisions: List[ShadowDecision]
    body: str
    failed_collectors: List[str]


class MorningIntelligencePipeline:
    def __init__(self, collectors: Sequence[tuple[str, Collector]], *,
                 store: Optional[IntelligenceStore] = None,
                 notifier: Optional[Notifier] = None,
                 shadow_policy: Optional[ShadowPolicy] = None):
        self.collectors = list(collectors)
        self.store = store
        self.notifier = notifier
        self.shadow_policy = shadow_policy or ShadowPolicy()

    def run(self, *, now: datetime, holdings: Sequence[str] = (),
            baseline_buy_symbols: Sequence[str] = ()) -> BriefingReport:
        raw: List[MarketEvent] = []
        failed = []
        for name, collect in self.collectors:
            try:
                raw.extend(collect())
            except Exception:
                # 정보 수집 실패가 매매 엔진까지 전파되면 안 된다. 이름만 남기고
                # 비밀값·요청 URL·토큰은 기록하지 않는다.
                failed.append(name)
        events = classify_all(deduplicate(raw))
        decisions = self.shadow_policy.evaluate_many(
            baseline_buy_symbols, "BUY", events, now=now)
        body = render_briefing(events, decisions=decisions,
                               generated_at=now, holdings=holdings)
        if failed:
            body += "\n\n수집 실패(매매 영향 없음): " + ", ".join(failed)
        if self.store:
            self.store.append_events(events)
            self.store.append_decisions(decisions)
        if self.notifier:
            self.notifier.info("아침 시장 요약", body)
        return BriefingReport(now, events, decisions, body, failed)
