from datetime import datetime, timezone

from autotrader.intelligence.models import MarketEvent
from autotrader.intelligence.risk import classify_event
from autotrader.intelligence.shadow import ShadowPolicy


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _event(title, *, official, symbol="AAA", source="test"):
    return MarketEvent(source, "KR", title, "https://example.com", NOW,
                       symbol=symbol, official=official)


def test_only_official_high_risk_event_becomes_would_block():
    official = classify_event(_event("유상증자 결정", official=True))
    news = classify_event(_event("유상증자 가능성 보도", official=False,
                                 source="naver-news"))
    assert official.severity == "high"
    assert news.severity == "watch"
    policy = ShadowPolicy()
    assert policy.evaluate("AAA", "BUY", [official], now=NOW).shadow_action == "would_block"
    assert policy.evaluate("AAA", "BUY", [news], now=NOW).shadow_action == "review"
    assert policy.evaluate("BBB", "BUY", [official], now=NOW).shadow_action == "allow"


def test_non_buy_baseline_is_never_blocked_by_shadow_layer():
    event = classify_event(_event("거래정지", official=True))
    decision = ShadowPolicy().evaluate("AAA", "HOLD", [event], now=NOW)
    assert decision.shadow_action == "allow"
    assert decision.reasons == []
