from datetime import date, datetime
from autotrader.market import is_trading_day, is_market_open, reason_closed


def test_weekend_is_not_trading():
    assert not is_trading_day(date(2026, 8, 22))  # Saturday
    assert not is_trading_day(date(2026, 8, 23))  # Sunday
    assert is_trading_day(date(2026, 8, 24))       # Monday


def test_new_year_is_holiday():
    assert not is_trading_day(date(2026, 1, 1))
    assert reason_closed(datetime(2026, 1, 1, 10)) == "holiday"


def test_off_hours_is_closed_even_on_trading_day():
    assert not is_market_open(datetime(2026, 8, 24, 7, 0))
    assert is_market_open(datetime(2026, 8, 24, 10, 0))
    assert not is_market_open(datetime(2026, 8, 24, 16, 0))
