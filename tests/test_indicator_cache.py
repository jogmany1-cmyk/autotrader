"""지표 캐시 — 값이 바뀌지 않아야 한다.

전략은 매 봉마다 [0..at] 전체에 대해 지표를 다시 계산했다. 봉 수의 제곱으로
느려져(1,000봉 6초 / 5,000봉 159초) 실험 반복의 병목이었다.

최적화의 전제는 **인과성**이다 — 이 지표들은 index i 의 값이 i 이하 데이터만
쓰므로, 전체를 한 번 계산해 [at] 을 읽은 값과 앞부분만 잘라 계산한 마지막 값이
정확히 같다. 그 성질이 깨지면 조용히 미래 정보가 새거나 값이 달라진다.
"""
import random
from datetime import datetime, timedelta

import pytest

from autotrader import indicators as ind
from autotrader.models import Bar
from autotrader.strategy.base import StrategyContext


def _series(n=300, seed=7):
    random.seed(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        c = price * (1 + random.gauss(0, 0.02))
        h = max(o, c) * (1 + abs(random.gauss(0, 0.01)))
        l = min(o, c) * (1 - abs(random.gauss(0, 0.01)))
        bars.append(Bar(ts=datetime(2020, 1, 1) + timedelta(days=i), open=o,
                        high=h, low=l, close=c, volume=1000.0))
        price = c
    return bars


BARS = _series()
VALS = ind.closes(BARS)
IDXS = list(range(60, 300, 11))


# ---- 인과성: 최적화가 성립하는 근거 ---------------------------------------

@pytest.mark.parametrize("name,full,prefix", [
    ("atr", lambda: ind.atr(BARS, 14), lambda i: ind.atr(BARS[:i + 1], 14)[-1]),
    ("rsi", lambda: ind.rsi(VALS, 14), lambda i: ind.rsi(VALS[:i + 1], 14)[-1]),
    ("sma20", lambda: ind.sma(VALS, 20), lambda i: ind.sma(VALS[:i + 1], 20)[-1]),
    ("sma200", lambda: ind.sma(VALS, 200), lambda i: ind.sma(VALS[:i + 1], 200)[-1]),
    ("ema", lambda: ind.ema(VALS, 20), lambda i: ind.ema(VALS[:i + 1], 20)[-1]),
    ("roc", lambda: ind.roc(VALS, 20), lambda i: ind.roc(VALS[:i + 1], 20)[-1]),
])
def test_indicator_is_causal(name, full, prefix):
    """전체 계산 후 [i] == 앞부분만 계산 후 [-1]. 부동소수점 오차도 0이어야 한다."""
    series = full()
    for i in IDXS:
        assert series[i] == prefix(i), f"{name} 가 index {i} 에서 어긋난다"


# ---- 캐시 접근자가 같은 값을 주는가 ---------------------------------------

@pytest.mark.parametrize("at", IDXS)
def test_cached_accessors_match_uncached(at):
    ctx = StrategyContext("AAA", BARS, at, cache={})
    assert ind.atr_at(ctx, 14) == ind.atr(BARS[:at + 1], 14)[-1]
    assert ind.rsi_at(ctx, 14) == ind.rsi(VALS[:at + 1], 14)[-1]
    assert ind.sma_at(ctx, 20) == ind.sma(VALS[:at + 1], 20)[-1]
    assert ind.roc_at(ctx, 20) == ind.roc(VALS[:at + 1], 20)[-1]


def test_cache_absent_still_works():
    """캐시를 안 넘겨도 값은 같다 — 동작은 같고 느릴 뿐."""
    ctx = StrategyContext("AAA", BARS, 100)
    assert ind.atr_at(ctx, 14) == ind.atr(BARS[:101], 14)[-1]


def test_cache_is_actually_reused():
    calls = {"n": 0}
    real = ind.atr

    class _Counting:
        def __call__(self, bars, period):
            calls["n"] += 1
            return real(bars, period)

    cache = {}
    import autotrader.indicators as m
    m.atr = _Counting()
    try:
        for at in range(50, 60):
            ind.atr_at(StrategyContext("AAA", BARS, at, cache=cache), 14)
    finally:
        m.atr = real
    assert calls["n"] == 1, "같은 종목·같은 기간이면 한 번만 계산해야 한다"


def test_cache_rebuilds_when_series_grows():
    """봉이 늘어나면 다시 만든다 — 오래된 시리즈를 인덱싱하면 안 된다."""
    cache = {}
    short = BARS[:150]
    ind.atr_at(StrategyContext("AAA", short, 100, cache=cache), 14)
    v = ind.atr_at(StrategyContext("AAA", BARS, 250, cache=cache), 14)
    assert v == ind.atr(BARS[:251], 14)[-1]


# ---- 미래 정보 금지 --------------------------------------------------------

def test_accessor_value_does_not_depend_on_future_bars():
    """뒤 봉을 바꿔도 [at] 값은 그대로여야 한다."""
    tail_changed = BARS[:200] + [
        Bar(ts=b.ts, open=b.open * 3, high=b.high * 3, low=b.low * 3,
            close=b.close * 3, volume=b.volume) for b in BARS[200:]]
    a = ind.atr_at(StrategyContext("AAA", BARS, 150, cache={}), 14)
    b = ind.atr_at(StrategyContext("AAA", tail_changed, 150, cache={}), 14)
    assert a == b
