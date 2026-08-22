import math
from autotrader import indicators as ind
from autotrader.models import Bar
from datetime import datetime


def _b(n, base=100.0, step=1.0):
    return [Bar(datetime(2024, 1, 1), base + i * step, base + i * step + 1,
                base + i * step - 1, base + i * step, 1000) for i in range(n)]


def test_sma_matches_manual():
    values = [1, 2, 3, 4, 5]
    got = ind.sma(values, 3)
    assert got == [None, None, 2.0, 3.0, 4.0]


def test_rsi_bounds_and_uptrend():
    up = list(range(1, 30))
    r = ind.rsi(up, 14)
    assert r[-1] == 100.0
    down = list(range(30, 0, -1))
    r = ind.rsi(down, 14)
    assert r[-1] == 0.0
    mixed = ind.rsi([100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101], 14)
    assert 0 < mixed[-1] < 100


def test_atr_is_positive_and_finite():
    bars = _b(30)
    a = ind.atr(bars, 14)
    assert a[-1] is not None and a[-1] > 0


def test_zscore_zero_mean_unit_std():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    z = ind.zscore(vals)
    mean = sum(z) / len(z)
    assert abs(mean) < 1e-9
    var = sum((x - mean) ** 2 for x in z) / (len(z) - 1)
    assert abs(math.sqrt(var) - 1.0) < 1e-9


def test_max_drawdown_negative_or_zero():
    equity = [100, 110, 90, 95, 120, 100]
    dd = ind.max_drawdown(equity)
    assert dd < 0
    equity_up = [100, 110, 120]
    assert ind.max_drawdown(equity_up) == 0
