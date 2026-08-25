"""edge --strategy — 전략을 하나씩 떼어 재는 격리 측정.

앙상블 전체가 지고 있을 때 "누가 주범인가" 는 합쳐 놓은 숫자로는 답이 안 나온다.
이기는 전략의 우위를 지는 전략이 덮어 쓰기 때문이다. 실제로 실데이터에서 그
상황이 나왔다 — OOS Profit Factor 0.786, 그리고 **신뢰도가 높을수록 승률이
낮아지는** 역전(conf<50 34.8% → conf 60-70 21.4%)까지 관측됐다. 전략별로 떼어
보지 않고는 원인을 특정할 수 없다.

여기서 고정하는 것:
  - 이름으로 전략을 정확히 걸러내는가 (오타는 조용히 넘어가지 않고 실패하는가)
  - 격리해도 나머지 측정 경로가 그대로인가
  - 아무 것도 지정하지 않으면 **기존 동작 그대로**인가 ← 회귀 방지의 핵심
"""
import random
from datetime import datetime, timedelta
from typing import List

import pytest

from autotrader.config import Config
from autotrader.data.base import DataProvider
from autotrader.edge import STRATEGY_NAMES, EdgeAnalyzer, default_ensemble
from autotrader.models import Bar


def _series(n: int, seed: int) -> List[Bar]:
    random.seed(seed)
    bars, price = [], 10_000.0
    for i in range(n):
        o = price
        c = price * (1 + random.gauss(0, 0.02))
        h = max(o, c) * (1 + abs(random.gauss(0, 0.01)))
        l = min(o, c) * (1 - abs(random.gauss(0, 0.01)))
        bars.append(Bar(ts=datetime(2020, 1, 1) + timedelta(days=i), open=o,
                        high=h, low=l, close=c, volume=100_000.0))
        price = c
    return bars


class _Provider(DataProvider):
    def __init__(self, n: int = 400):
        self._bars = {"AAA": _series(n, 1), "BBB": _series(n, 2)}

    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        bars = self._bars[symbol]
        return bars[-limit:] if limit else bars

    def universe(self) -> List[str]:
        return list(self._bars)


# ---- 전략 선택 -------------------------------------------------------------

def test_names_match_weight_fields():
    """Ensemble 은 weights 를 strat.name 으로 찾는다 — 이름이 어긋나면 가중치가
    0 으로 읽혀 그 전략이 조용히 빠진다."""
    w = Config().weights
    for name in STRATEGY_NAMES:
        assert hasattr(w, name), f"StrategyWeights 에 {name} 필드가 없다"
    assert {s.name for s in default_ensemble(Config(), 0.5).strategies} == set(STRATEGY_NAMES)


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_only_keeps_exactly_one(name):
    ens = default_ensemble(Config(), 0.5, only=[name])
    assert [s.name for s in ens.strategies] == [name]


def test_only_keeps_a_subset_in_declared_order():
    ens = default_ensemble(Config(), 0.5, only=["mean_reversion", "day_breakout"])
    # 선택 순서가 아니라 앙상블의 고정 순서를 따른다 — 결과가 인자 순서에 흔들리면
    # 같은 조합을 다르게 적은 두 실행이 다른 숫자를 낸다.
    assert [s.name for s in ens.strategies] == ["day_breakout", "mean_reversion"]


def test_unknown_strategy_fails_loudly():
    """오타를 조용히 무시하면 '전체 앙상블'을 격리 측정으로 착각하게 된다."""
    with pytest.raises(ValueError) as e:
        default_ensemble(Config(), 0.5, only=["swing_trend", "swingtrend"])
    assert "swingtrend" in str(e.value)


def test_empty_only_means_full_ensemble():
    """--strategy 를 안 주면 args.strategy 는 None 이다. 그때는 전체를 켠다."""
    for empty in (None, []):
        ens = default_ensemble(Config(), 0.5, only=empty)
        assert len(ens.strategies) == len(STRATEGY_NAMES)


# ---- 측정 경로 -------------------------------------------------------------

def test_isolated_run_records_which_strategies_ran():
    """리포트에 남지 않으면 나중에 어떤 조건의 숫자인지 알 수 없다."""
    prov = _Provider()
    ens = default_ensemble(Config(), 0.45, only=["swing_trend"])
    rep = EdgeAnalyzer(prov, Config(), ensemble=ens, threshold=0.45,
                       horizons=(1, 5), warmup=50).run(bars=0)
    assert rep.strategies == ["swing_trend"]
    assert rep.as_dict()["strategies"] == ["swing_trend"]
    assert rep.n_bars > 0, "격리해도 기준선 측정은 그대로 돌아야 한다"


def test_isolated_score_equals_that_strategys_strength():
    """하나만 켜면 점수 = strength (weighted/total_w 에서 가중치가 약분된다).

    이 성질이 깨지면 격리 측정의 --threshold 가 무슨 뜻인지 알 수 없어진다.
    """
    from autotrader.models import Side
    from autotrader.strategy.base import StrategyContext

    bars = _series(400, seed=3)
    ens = default_ensemble(Config(), 0.0, only=["swing_trend"])
    strat = ens.strategies[0]
    checked = 0
    for at in range(250, 400):
        ctx = StrategyContext("AAA", bars, at, cache={})
        dec = ens.evaluate(ctx)
        res = strat.evaluate(StrategyContext("AAA", bars, at, cache={}))
        sig = res.signal.clamped()
        if sig.side is Side.BUY and sig.strength > 0:
            assert dec.score == pytest.approx(sig.strength), f"at={at}"
            checked += 1
    assert checked > 0, "매수 신호가 하나도 없어 성질을 확인하지 못했다"


def test_full_ensemble_run_is_unchanged_by_the_new_option():
    """옵션을 안 쓰면 결과가 기존과 같아야 한다 — 진단 도구를 붙이다 측정 자체를
    바꿔 버리면, 이전 결과와 비교할 수 없게 된다."""
    prov = _Provider()
    kw = dict(threshold=0.45, horizons=(1, 5), warmup=50)
    a = EdgeAnalyzer(prov, Config(), **kw).run(bars=0)
    b = EdgeAnalyzer(prov, Config(),
                     ensemble=default_ensemble(Config(), 0.45, 1, only=None),
                     **kw).run(bars=0)
    assert a.n_bars == b.n_bars and a.n_signals == b.n_signals
    assert [h.t_stat for h in a.horizons] == [h.t_stat for h in b.horizons]
    assert set(a.strategies) == set(STRATEGY_NAMES)
