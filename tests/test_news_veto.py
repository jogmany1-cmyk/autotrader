"""공시 수비 필터 고정 — 뉴스가 매매에 닿는 유일한 경로.

방향이 한쪽뿐이라는 것이 이 필터의 전부다. 살 수 있던 것을 못 사게 만들 뿐,
사지 않던 것을 사게 만들면 안 된다. 그리고 근거는 통계가 아니라 제도다:
거래정지 종목은 어떤 가격에도 팔 수 없으므로 `exits.py` 의 어떤 청산 규칙도
작동하지 않는다.
"""
from datetime import datetime, timedelta

import pytest

from autotrader.config import RiskLimits
from autotrader.intelligence import (apply_to_risk_engine, build_block_list,
                                     matched_terms)
from autotrader.intelligence.models import MarketEvent
from autotrader.risk import RiskEngine

NOW = datetime(2026, 8, 26, 7, 30)


def ev(title, *, symbol="005930", official=True, days_ago=1, summary=""):
    return MarketEvent(
        source="opendart", region="KR", title=title, url="http://x",
        published_at=NOW - timedelta(days=days_ago),
        symbol=symbol, summary=summary, official=official,
    )


# ---- 무엇을 막는가 ---------------------------------------------------------

@pytest.mark.parametrize("title", [
    "주권매매거래정지",
    "상장적격성 실질심사 대상 결정",
    "관리종목 지정",
    "감사의견 거절",
    "자본잠식 발생",
    "횡령·배임 혐의 발생",
    "회생절차 개시신청",
    "불성실공시법인 지정",
    "무상감자 결정",
])
def test_hard_events_block(title):
    blocks = build_block_list([ev(title)], now=NOW)
    assert "005930" in blocks
    # 사유에 무엇이 걸렸는지와 날짜가 남아야 리포트에서 원인을 읽을 수 있다.
    assert "2026-08-25" in blocks["005930"]


def test_release_notice_must_not_block():
    """실제 DART 피드에 '주권매매거래정지해제' 가 흔하다. 이걸 정지로 읽으면
    정상 종목을 영구히 배제하게 된다 — 조용히 기회를 없애는 종류의 버그다."""
    assert build_block_list([ev("주권매매거래정지해제")], now=NOW) == {}
    assert build_block_list([ev("거래정지 해제 안내")], now=NOW) == {}
    assert matched_terms(ev("주권매매거래정지해제")) == []


def test_ordinary_news_does_not_block():
    for title in ("3분기 실적 발표", "신제품 출시", "목표주가 상향"):
        assert build_block_list([ev(title)], now=NOW) == {}


def test_third_party_allotment_is_not_treated_as_bad_news():
    """한국에서 제3자배정 유상증자는 CAR +7.14% 로 오히려 호재로 읽힌다.
    '유상증자 = 악재' 로 넓히면 틀린다 — 그래서 목록에 넣지 않았다."""
    assert build_block_list([ev("제3자배정 유상증자 결정")], now=NOW) == {}


# ---- 언제 막는가 -----------------------------------------------------------

def test_unofficial_news_does_not_block_by_default():
    """일반 뉴스는 오보·재전송 가능성이 있어 자동 차단으로 승격하지 않는다."""
    e = ev("거래정지 위기", official=False)
    assert build_block_list([e], now=NOW) == {}
    assert "005930" in build_block_list([e], now=NOW, official_only=False)


def test_stale_events_expire():
    assert build_block_list([ev("거래정지", days_ago=400)], now=NOW) == {}
    assert "005930" in build_block_list([ev("거래정지", days_ago=10)], now=NOW)


def test_events_without_symbol_are_ignored():
    """어느 종목을 막을지 특정할 수 없으면 막지 않는 쪽이 안전하다."""
    assert build_block_list([ev("거래정지", symbol="")], now=NOW) == {}


def test_summary_is_searched_too():
    assert "005930" in build_block_list(
        [ev("주요사항보고서", summary="감사의견 거절 사유 발생")], now=NOW)


# ---- RiskEngine 배선 -------------------------------------------------------

def _engine(**kw):
    return RiskEngine(RiskLimits(**kw))


def _entry(engine, symbol="005930"):
    return engine.evaluate_entry(symbol=symbol, price=10_000, stop_price=9_000,
                                 equity=10_000_000, cash=10_000_000, positions={})


def test_blocked_symbol_cannot_enter():
    e = _engine()
    assert _entry(e).allowed
    e.block("005930", "거래정지(2026-08-25)")
    d = _entry(e)
    assert not d.allowed
    assert "news-block" in d.reason and "거래정지" in d.reason


def test_block_is_checked_before_account_level_gates():
    """자리가 없든 쿨다운이든, 거래정지 종목은 사면 안 된다. 사유가 다른
    게이트로 가려지면 리포트에서 원인을 못 읽는다."""
    e = _engine(max_positions=1)
    e.block("005930", "상장폐지")
    d = e.evaluate_entry(symbol="005930", price=10_000, stop_price=9_000,
                         equity=10_000_000, cash=10_000_000,
                         positions={"OTHER": object()})   # 이미 만석
    assert "news-block" in d.reason


def test_unblock_restores_entry():
    e = _engine()
    e.block("005930", "거래정지")
    e.unblock("005930")
    assert _entry(e).allowed


def test_other_symbols_unaffected():
    e = _engine()
    e.block("005930", "거래정지")
    assert _entry(e, symbol="000660").allowed


def test_apply_merges_instead_of_replacing():
    """다른 경로로 넣은 수동 차단을 지우면 안 된다."""
    e = _engine()
    e.block("MANUAL", "수동 차단")
    apply_to_risk_engine(e, [ev("거래정지")], now=NOW)
    assert "MANUAL" in e.blocked_symbols
    assert "005930" in e.blocked_symbols


def test_filter_generates_no_trades():
    """필터의 경제적 근거: 거래를 만들지 않으므로 회전율 비용이 0 이다.
    거부만 할 수 있고 진입을 만들어낼 수는 없다는 것을 구조로 고정한다."""
    e = _engine()
    apply_to_risk_engine(e, [ev("거래정지")], now=NOW)
    # 차단 목록은 오직 거부에만 쓰인다 — 허용 쪽 분기가 없다.
    assert not _entry(e).allowed
    e.blocked_symbols.clear()
    before = _entry(e)
    apply_to_risk_engine(e, [], now=NOW)      # 빈 목록을 얹어도
    after = _entry(e)
    assert before.allowed == after.allowed    # 허용 여부가 달라지지 않는다
