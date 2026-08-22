from datetime import datetime, timedelta
from autotrader.metrics import performance_from
from autotrader.models import EquityPoint, Trade


def _curve(values):
    return [EquityPoint(ts=datetime(2024, 1, 1) + timedelta(days=i),
                        equity=v, cash=v, exposure=0.0) for i, v in enumerate(values)]


def test_empty_reports_zero_safely():
    r = performance_from([], [])
    assert r.n_trades == 0 and r.net_return == 0.0 and r.max_drawdown == 0.0


def test_monotone_growth_has_no_drawdown():
    r = performance_from(_curve([100, 105, 110, 120]), [])
    assert r.max_drawdown == 0
    assert r.net_return > 0


def test_win_rate_and_expectancy():
    trades = [
        Trade("A", datetime(2024,1,1), datetime(2024,1,2), 10, 100, 110, 100, 0.10, "target", 1),
        Trade("A", datetime(2024,1,3), datetime(2024,1,4), 10, 110, 105, -50, -0.045, "stop", 1),
        Trade("A", datetime(2024,1,5), datetime(2024,1,6), 10, 105, 115, 100, 0.095, "target", 1),
    ]
    r = performance_from(_curve([1000, 1100, 1050, 1150]), trades)
    assert r.n_trades == 3
    assert abs(r.win_rate - 2/3) < 1e-3
    assert r.profit_factor > 1
