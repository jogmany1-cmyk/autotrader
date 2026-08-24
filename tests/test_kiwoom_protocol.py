"""키움 공식 문서에서 확인한 프로토콜 사실을 코드로 고정한다.

출처: 키움 REST API 공식 문서 (접근토큰발급 au10001, "API 호출 횟수 제한").
추측으로 맞춰오던 것들의 정답이라, 다시 추측으로 되돌아가지 않도록 못박는다.
"""
from datetime import datetime

import pytest

from autotrader.config import (KIWOOM_INTERVAL_PAPER, KIWOOM_INTERVAL_REAL,
                               KiwoomConfig, kiwoom_min_interval,
                               kiwoom_token_ttl)
from tests._optional import requires_requests

NOW = datetime(2026, 8, 24, 15, 46, 0)


# ---- 토큰 만료 -----------------------------------------------------------

def test_expires_dt_is_a_datetime_not_a_duration():
    """문서상 응답 필드는 expires_dt(만료일)뿐이다. 남은 초가 아니다.

    이전 코드는 int("20260825154600") 을 '남은 초' 로 더해서 만료 시각을
    64만 년 뒤로 계산했다 — 토큰이 영원히 유효하다고 착각한다.
    """
    ttl = kiwoom_token_ttl({"expires_dt": "20260825154600"}, now=NOW)
    assert ttl == 86400.0          # 정확히 24시간 뒤


def test_old_bug_would_have_produced_an_absurd_ttl():
    """회귀 방지 — 예전 계산식이 얼마나 틀렸는지 숫자로 남긴다."""
    naive = int("20260825154600")
    assert naive > 20_000_000_000_000
    assert kiwoom_token_ttl({"expires_dt": "20260825154600"}, now=NOW) < 7 * 86400


def test_expires_in_seconds_is_honoured_when_present():
    assert kiwoom_token_ttl({"expires_in": "3600"}, now=NOW) == 3600.0


@pytest.mark.parametrize("payload", [
    {},                                   # 필드 없음
    {"expires_dt": ""},                   # 빈 값
    {"expires_dt": "알 수 없음"},          # 파싱 불가
    {"expires_dt": "20200101000000"},     # 이미 지난 날짜
    {"expires_in": "99999999"},           # 상식 밖(3년)
])
def test_unparseable_or_absurd_values_fall_back_conservatively(payload):
    """늦게 재발급하면 매매가 멈춘다. 의심스러우면 짧게 잡는다."""
    assert kiwoom_token_ttl(payload, now=NOW) == 43200.0


def test_various_datetime_formats():
    for raw in ("20260825154600", "2026-08-25 15:46:00", "202608251546"):
        assert 86000 < kiwoom_token_ttl({"expires_dt": raw}, now=NOW) <= 86400


# ---- 유량 제한 -----------------------------------------------------------

def test_official_rate_limits():
    """실서버 조회 TR 1초당 5회 · 모의투자 TR 1개당 1초 1회."""
    assert KIWOOM_INTERVAL_REAL <= 0.25      # 5회/초 = 0.2초, 여유 포함
    assert KIWOOM_INTERVAL_PAPER >= 1.0      # 1회/초
    assert kiwoom_min_interval(is_paper=True) == KIWOOM_INTERVAL_PAPER
    assert kiwoom_min_interval(is_paper=False) == KIWOOM_INTERVAL_REAL


def test_real_server_is_faster_than_paper():
    assert kiwoom_min_interval(False) < kiwoom_min_interval(True)


@requires_requests
def test_provider_picks_interval_from_mode(tmp_path):
    from autotrader.data import KiwoomProvider

    paper = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y", is_paper=True),
                           cache_dir=str(tmp_path))
    real = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y", is_paper=False),
                          cache_dir=str(tmp_path))
    assert paper.min_interval == KIWOOM_INTERVAL_PAPER
    assert real.min_interval == KIWOOM_INTERVAL_REAL
