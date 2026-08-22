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



def test_nxt_session_of_returns_correct_label():
    from autotrader.market import session_of
    assert session_of(datetime(2026, 8, 24, 8, 30)) == "pre"
    assert session_of(datetime(2026, 8, 24, 10, 0)) == "regular"
    assert session_of(datetime(2026, 8, 24, 18, 0)) == "after"
    assert session_of(datetime(2026, 8, 24, 21, 0)) == "closed"
    assert session_of(datetime(2026, 8, 23, 10, 0)) == "closed"   # 일요일


def test_extended_market_open_respects_flags():
    from autotrader.market import is_extended_market_open
    ts_pre = datetime(2026, 8, 24, 8, 30)
    ts_after = datetime(2026, 8, 24, 18, 0)
    ts_reg = datetime(2026, 8, 24, 10, 0)
    assert is_extended_market_open(ts_reg)  # 정규장은 플래그와 무관하게 열림
    assert not is_extended_market_open(ts_pre, include_pre=False)
    assert is_extended_market_open(ts_pre, include_pre=True)
    assert not is_extended_market_open(ts_after, include_after=False)
    assert is_extended_market_open(ts_after, include_after=True)


def test_live_trader_filters_to_validated_strategies():
    import datetime as dt
    from autotrader.broker import PaperBroker
    from autotrader.config import Config, Costs
    from autotrader.data import SyntheticProvider
    from autotrader.live import LiveTrader
    from autotrader.registry import StrategyRegistry, StrategyRecord

    reg = StrategyRegistry()
    reg.upsert(StrategyRecord("day_breakout", dt.datetime.utcnow(), 1.5, 40, -0.10))
    reg.upsert(StrategyRecord("swing_trend", dt.datetime.utcnow(), 1.5, 40, -0.10))
    p = SyntheticProvider()
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    trader = LiveTrader(p, PaperBroker(1e7, Costs()), cfg,
                        registry=reg, validated_only=True, dry_run=True)
    names = {s.name for s in trader.strategies}
    assert names == {"day_breakout", "swing_trend"}
