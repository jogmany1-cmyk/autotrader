"""edge 도 지표 캐시를 써야 한다 — Backtester 만 쓰고 있었다.

`StrategyContext.cache` 는 같은 종목의 모든 봉이 지표 시리즈 하나를 공유하게
해서 계산량을 봉 수의 제곱에서 선형으로 낮춘다. `Backtester` 는 처음부터
`indicator_cache` 를 넘기고 있었는데(backtest.py), `EdgeAnalyzer.run` 만
`StrategyContext(symbol=sym, bars=series, at=i)` 로 캐시 없이 만들고 있었다.

증상은 조용했다. 값은 정확히 같고 **느리기만** 하다. 그래서 테스트도 리포트도
아무 불평을 하지 않았고, 실데이터에서 처음 드러났다 — 4,298종목 × 2,500봉
기준 종목당 13.3초, 전체 약 15.8시간. 캐시를 연결하자 같은 조건에서 22배
빨라졌고 출력은 한 글자도 바뀌지 않았다.

여기서 고정하는 것은 **값이 아니라 배선**이다. 값의 동일성은
tests/test_indicator_cache.py 가 이미 인과성으로 보장한다. 이 파일은 edge 가
그 장치에 실제로 연결돼 있는지만 본다 — 끊어져도 결과가 멀쩡해 보이는 종류의
회귀라서, 사람이 알아채기를 기대할 수 없다.
"""
import random
from datetime import datetime, timedelta
from typing import Dict, List

from autotrader import indicators as ind
from autotrader.config import Config
from autotrader.data.base import DataProvider
from autotrader.edge import EdgeAnalyzer
from autotrader.models import Bar


def _series(n: int, seed: int) -> List[Bar]:
    random.seed(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        c = price * (1 + random.gauss(0, 0.02))
        h = max(o, c) * (1 + abs(random.gauss(0, 0.01)))
        l = min(o, c) * (1 - abs(random.gauss(0, 0.01)))
        bars.append(Bar(ts=datetime(2020, 1, 1) + timedelta(days=i), open=o,
                        high=h, low=l, close=c, volume=10_000.0))
        price = c
    return bars


class _Provider(DataProvider):
    """종목 2개짜리 최소 공급자. 캐시가 종목 경계에서 갈리는지 보려면 2개가 필요하다."""

    def __init__(self, n: int = 320):
        self._bars = {"AAA": _series(n, seed=1), "BBB": _series(n, seed=2)}

    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        bars = self._bars[symbol]
        return bars[-limit:] if limit else bars

    def universe(self) -> List[str]:
        return list(self._bars)


def _analyzer(provider: _Provider) -> EdgeAnalyzer:
    # warmup 을 낮춰 짧은 시리즈에서도 봉이 실제로 평가되게 한다.
    return EdgeAnalyzer(provider, Config(), threshold=0.45,
                        horizons=(1, 5), warmup=50)


def test_edge_passes_a_shared_cache_per_symbol():
    """종목 안에서는 같은 dict 하나를 공유하고, 종목이 바뀌면 새로 만든다."""
    provider = _Provider()
    an = _analyzer(provider)

    seen: Dict[str, List[int]] = {}
    real = an.ensemble.evaluate

    def _spy(ctx):
        assert ctx.cache is not None, (
            "edge 가 StrategyContext 에 캐시를 넘기지 않는다 — "
            "봉 수의 제곱으로 느려진다")
        seen.setdefault(ctx.symbol, []).append(id(ctx.cache))
        return real(ctx)

    an.ensemble.evaluate = _spy
    an.run(bars=0)

    assert set(seen) == {"AAA", "BBB"}, "두 종목 모두 평가돼야 의미 있는 검사다"
    for sym, ids in seen.items():
        assert len(ids) > 1, f"{sym}: 봉이 하나뿐이면 캐시 공유를 볼 수 없다"
        assert len(set(ids)) == 1, f"{sym}: 봉마다 캐시가 새로 만들어지고 있다"
    assert seen["AAA"][0] != seen["BBB"][0], (
        "종목이 바뀌었는데 캐시를 재사용하면 남의 시세로 계산한다")


def test_edge_computes_each_indicator_series_once_per_symbol():
    """지표 계산 횟수가 봉 수를 따라 늘지 않는다 — 캐시가 실제로 먹히는지."""
    provider = _Provider()
    an = _analyzer(provider)

    calls = {"n": 0}
    real_atr = ind.atr

    def _counting(bars, period):
        calls["n"] += 1
        return real_atr(bars, period)

    ind.atr = _counting
    try:
        rep = an.run(bars=0)
    finally:
        ind.atr = real_atr

    assert rep.n_bars > 100, "평가된 봉이 너무 적어 회귀를 잡지 못한다"
    # 캐시가 걸리면 (종목 × 기간) 수만큼만 계산된다. 끊기면 봉마다 계산되므로
    # n_bars 규모로 폭증한다. 그 사이를 넉넉히 가르는 상한을 둔다.
    assert calls["n"] < rep.n_bars / 10, (
        f"atr 이 {calls['n']}회 계산됐다 (평가 봉 {rep.n_bars}개) — "
        "캐시가 끊겨 매 봉마다 다시 계산하고 있다")
