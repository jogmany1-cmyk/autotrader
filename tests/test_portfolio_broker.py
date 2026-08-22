from datetime import datetime
from autotrader.broker import PaperBroker
from autotrader.config import Costs
from autotrader.models import Bar, Order, Side


def _bar(close, high=None, low=None, o=None):
    return Bar(datetime(2024, 1, 2), o or close, high or close, low or close, close, 1000)


def test_roundtrip_pnl_after_fees_and_tax():
    b = PaperBroker(1_000_000, Costs())
    fill = b.submit(Order("A", Side.BUY, 100), price_hint=1000, ts=datetime(2024, 1, 2),
                    stop=970, target=1030)
    assert fill.side is Side.BUY
    assert b.cash() < 1_000_000
    # 종가가 타깃을 넘어서면 자동 청산
    closed = b.mark({"A": _bar(1050, high=1050, low=1000)}, datetime(2024, 1, 3))
    assert len(closed) == 1
    trade = closed[0]
    assert trade.exit_reason == "target"
    assert trade.pnl > 0
    assert b.cash() > 1_000_000  # 최종 순이익


def test_stop_triggers_when_low_breaches():
    b = PaperBroker(1_000_000, Costs())
    b.submit(Order("A", Side.BUY, 100), price_hint=1000, ts=datetime(2024, 1, 2),
             stop=980, target=1050)
    closed = b.mark({"A": _bar(970, high=990, low=960, o=985)}, datetime(2024, 1, 3))
    assert len(closed) == 1 and closed[0].exit_reason == "stop"


def test_reject_when_cash_insufficient():
    import pytest
    from autotrader.broker.base import BrokerError
    b = PaperBroker(100, Costs())
    with pytest.raises(BrokerError):
        b.submit(Order("A", Side.BUY, 10), price_hint=1000)


def test_hard_stop_triggers_before_strategy_stop():
    from datetime import datetime
    from autotrader.broker import PaperBroker
    from autotrader.config import Costs
    from autotrader.models import Bar, Order, Side

    b = PaperBroker(1_000_000, Costs())
    b.submit(Order("A", Side.BUY, 100), price_hint=1000,
             ts=datetime(2024, 1, 2), stop=850, target=1200)
    # 스탑(850)까지 안 갔지만 하드 스톱(-10% = 900) 아래로 갭다운 → hard_stop 발동
    bar = Bar(datetime(2024, 1, 3), 890, 900, 870, 880, 1000)
    closed = b.mark({"A": bar}, datetime(2024, 1, 3),
                    hard_stop_pct=0.10, trail_pct=0.0)
    assert len(closed) == 1
    assert closed[0].exit_reason == "hard_stop"
