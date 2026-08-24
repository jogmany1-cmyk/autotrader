"""휴장일 표 회귀 테스트 — 실데이터로 확인된 날짜를 고정한다.

이 표는 손으로 관리하므로 반드시 틀린다. 실제로 삼성전자 40년치 실데이터와
대조했더니 세 건이 어긋나 있었고, 그 틀린 날짜 하나가 실제 거래일을 통째로
건너뛰게 만든다 (`is_trading_day` 가 매매 여부를 결정하므로).

아래 날짜는 KRX 실데이터에 봉이 있었는지/없었는지로 확인한 것이다. 추측이 아니다.
"""
from datetime import date

from autotrader.market import is_trading_day


def test_2026_03_04_is_a_trading_day():
    """공휴일이 아니다. 3/1 삼일절이 일요일이라 3/2 가 대체공휴일이고 3/4 는 평일.
    실데이터에 이 날 봉이 있다."""
    assert is_trading_day(date(2026, 3, 4)) is True


def test_2025_01_27_is_a_holiday():
    """설 연휴 임시공휴일. 표에 1/28~30 만 있고 1/27 이 빠져 있었다.
    실데이터에 이 날 봉이 없다."""
    assert is_trading_day(date(2025, 1, 27)) is False


def test_2026_07_17_is_a_holiday():
    """제헌절 부활 — 2026년부터 공휴일. 실데이터에 이 날 봉이 없다."""
    assert is_trading_day(date(2026, 7, 17)) is False


def test_seollal_2025_full_span_is_closed():
    """1/27 을 추가한 뒤에도 나머지 연휴가 그대로인지 확인."""
    for day in (28, 29, 30):
        assert is_trading_day(date(2025, 1, day)) is False


def test_regular_weekday_is_still_a_trading_day():
    """표를 고치다 평일까지 막아버리지 않았는지 확인."""
    assert is_trading_day(date(2026, 3, 5)) is True
    assert is_trading_day(date(2026, 7, 16)) is True
