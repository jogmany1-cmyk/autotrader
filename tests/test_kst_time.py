"""market.now_kst 회귀 — 장 시간 판정이 UTC 로 어긋나던 문제."""
from datetime import datetime, timedelta, timezone

from autotrader.market import KST, is_market_open, now_kst


def test_kst_is_utc_plus_nine():
    assert KST.utcoffset(None) == timedelta(hours=9)


def test_now_kst_is_nine_hours_ahead_of_utc():
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now_kst() - utc
    assert timedelta(hours=8, minutes=59) < delta < timedelta(hours=9, minutes=1)


def test_utc_naive_time_would_misjudge_market_hours():
    """왜 now_kst 가 필요한지 고정한다 — UTC 를 넘기면 판정이 뒤집힌다."""
    kst_open = datetime(2026, 8, 24, 9, 30)          # 한국시간 장중
    utc_same_moment = kst_open - timedelta(hours=9)  # 같은 순간의 UTC

    assert is_market_open(kst_open) is True
    assert is_market_open(utc_same_moment) is False  # 장중인데 휴장으로 판정
