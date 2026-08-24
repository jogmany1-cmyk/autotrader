"""진입 신호 우위 측정 회귀 테스트.

이 도구가 답하는 질문은 "백테스트가 나쁠 때 진입이 문제인가 청산이 문제인가"
다. 둘은 정반대의 대응을 요구하므로, 측정이 틀리면 엉뚱한 곳을 고치게 된다.
"""
import math
from datetime import datetime, timedelta

import pytest

from autotrader.config import Config
from autotrader.data.base import DataProvider
from autotrader.edge import EdgeAnalyzer, HorizonEdge
from autotrader.models import Bar, Side, Signal
from autotrader.strategy.ensemble import EnsembleDecision


def _bars(closes, start=datetime(2020, 1, 1)):
    """종가 리스트로 봉 시퀀스를 만든다. 시가=전 종가, 고저는 ±0."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(ts=start + timedelta(days=i), open=prev,
                       high=max(prev, c), low=min(prev, c), close=c,
                       volume=1000.0))
        prev = c
    return out


class _Fixed(DataProvider):
    def __init__(self, series):
        self._s = series

    def universe(self):
        return sorted(self._s)

    def history(self, symbol, limit=500):
        b = self._s[symbol]
        return b[-limit:] if limit else b


class _StubEnsemble:
    """지정한 인덱스에서만 BUY 를 내는 앙상블 대역."""

    def __init__(self, buy_at, score=0.9):
        self.buy_at = set(buy_at)
        self.score = score

    def evaluate(self, ctx):
        side = Side.BUY if ctx.at in self.buy_at else Side.HOLD
        return EnsembleDecision(signal=Signal(side, 1.0, ""),
                                score=self.score if side is Side.BUY else 0.0,
                                votes=1, stop_hint=0.0, target_hint=0.0,
                                detail={})


def _analyzer(series, buy_at, **kw):
    prov = _Fixed(series)
    kw.setdefault("horizons", (1, 2))
    kw.setdefault("warmup", 2)
    kw.setdefault("threshold", 0.5)
    return EdgeAnalyzer(prov, Config.default(),
                        ensemble=_StubEnsemble(buy_at), **kw)


# ---- 계산이 맞는가 -------------------------------------------------------

def test_edge_is_signal_minus_baseline():
    """평평한 시장에서 신호 뒤에만 오르면 우위 = 그 상승분."""
    closes = [100.0] * 12
    closes[6] = 110.0          # index 5 에서 판단 → 6 시가 진입 → 상승
    rep = _analyzer({"A": _bars(closes)}, buy_at=[4]).run()
    e = rep.horizon(1)
    assert e is not None and e.n_signals == 1
    assert e.edge > 0, "신호 뒤 상승이 우위로 잡혀야 한다"


def test_no_edge_when_signal_fires_on_every_bar():
    """모든 봉에서 신호가 뜨면 기준선과 정확히 같아야 한다 — 우위 0.

    시장이 올랐다는 이유만으로 우위가 잡히면 측정이 틀린 것이다.
    """
    closes = [100.0 + i for i in range(40)]     # 꾸준한 상승
    rep = _analyzer({"A": _bars(closes)}, buy_at=range(1000)).run()
    e = rep.horizon(1)
    assert e.n_signals == e.n_baseline
    assert abs(e.edge) < 1e-12, "상승장 자체가 우위로 잡히면 안 된다"


def test_baseline_uses_every_bar_not_just_signals():
    closes = [100.0] * 30
    rep = _analyzer({"A": _bars(closes)}, buy_at=[5]).run()
    e = rep.horizon(1)
    assert e.n_signals == 1
    assert e.n_baseline > 20, "기준선은 신호와 무관하게 전 구간을 봐야 한다"


def test_entry_is_next_bar_open_not_todays_close():
    """미래 정보 금지 — 판단한 봉의 종가로 들어가면 안 된다."""
    closes = [100.0] * 10
    closes[5] = 200.0          # index 4 판단 시점에는 알 수 없는 값
    series = _bars(closes)
    rep = _analyzer({"A": series}, buy_at=[4], horizons=(1,)).run()
    e = rep.horizon(1)
    # 진입가는 series[5].open = 100 (전 종가), 1일 뒤 종가 series[6].close = 100
    assert e.signal_mean == pytest.approx(0.0, abs=1e-9)


# ---- t값 -----------------------------------------------------------------

def test_t_stat_grows_with_sample_size():
    """같은 우위라도 표본이 크면 t 가 커진다 — 작은 표본을 믿지 않기 위해."""
    small = HorizonEdge(5, 4, 100, 0.02, 0.0, 0.5, 0.5, 0.05)
    large = HorizonEdge(5, 400, 100, 0.02, 0.0, 0.5, 0.5, 0.05)
    assert abs(large.t_stat) > abs(small.t_stat)
    assert small.verdict != "유의"
    assert large.verdict == "유의"


def test_zero_variance_does_not_divide_by_zero():
    assert HorizonEdge(5, 10, 10, 0.01, 0.0, 1.0, 0.5, 0.0).t_stat == 0.0


def test_verdict_thresholds():
    # n=500, horizon=5 → 유효 표본 100, 표준오차 0.1/10 = 0.01
    assert HorizonEdge(5, 500, 100, 0.03, 0.0, .5, .5, 0.1).verdict == "유의"
    assert HorizonEdge(5, 500, 100, 0.012, 0.0, .5, .5, 0.1).verdict == "약함"
    assert HorizonEdge(5, 500, 100, 0.001, 0.0, .5, .5, 0.1).verdict == "우연과 구분 불가"


def test_negative_edge_is_called_out_not_praised():
    """신호가 기준선보다 나쁘면 '유의' 가 아니라 '역효과' 여야 한다.

    abs(t) 를 쓰던 판에서는 t=-1.39 가 "약함" 으로 찍혔다 — 우위가 없는 정도가
    아니라 반대로 작동하는데도 긍정으로 읽힌다.
    """
    e = HorizonEdge(5, 500, 100, -0.03, 0.0, .3, .5, 0.1)
    assert e.t_stat < 0
    assert e.verdict == "역효과"


def test_overlapping_windows_shrink_the_effective_sample():
    """h일 수익률을 매 봉마다 재면 이웃 표본이 h-1 일을 공유한다.

    보정이 없으면 10일 지평선 t 가 실제의 약 √10 배로 나온다.
    """
    e = HorizonEdge(10, 250, 100, 0.02, 0.0, .5, .5, 0.1)
    assert e.effective_n == pytest.approx(25.0)
    naive = 0.02 / (0.1 / math.sqrt(250))
    assert abs(e.t_stat) < abs(naive)
    assert abs(e.t_stat) == pytest.approx(abs(naive) / math.sqrt(10), rel=1e-9)


def test_effective_n_never_drops_below_one():
    e = HorizonEdge(20, 3, 100, 0.02, 0.0, .5, .5, 0.1)
    assert e.effective_n == 1.0


def test_gate_uses_one_horizon_not_the_maximum():
    """지평선 4개 중 최대값으로 판정하면 전부 잡음이어도 하나는 커 보인다."""
    from autotrader.edge import EdgeReport
    rep = EdgeReport(n_bars=1000, n_signals=500, threshold=0.5)
    rep.horizons = [
        HorizonEdge(1, 500, 100, 0.03, 0.0, .5, .5, 0.1),    # t 큼
        HorizonEdge(20, 500, 100, 0.001, 0.0, .5, .5, 0.1),  # t 작음
    ]
    assert rep.best_t() > rep.gate_t()          # 기본은 가장 긴 지평선
    assert rep.gate_t(1) > rep.gate_t(20)


# ---- 점수 구간 · 역행 ----------------------------------------------------

def test_score_buckets_are_ordered_by_score():
    closes = [100.0 + i * 0.1 for i in range(60)]
    an = _analyzer({"A": _bars(closes)}, buy_at=range(2, 50))
    rep = an.run(buckets=3)
    assert len(rep.buckets) == 3
    assert rep.buckets[0].lo <= rep.buckets[-1].hi


def test_adverse_ratio_counts_drawdown_after_entry():
    """진입 뒤 저가가 얼마나 밀렸는지 — 손절 폭을 정할 근거."""
    closes = [100.0] * 6 + [80.0] + [100.0] * 6
    rep = _analyzer({"A": _bars(closes)}, buy_at=[4], horizons=(1, 5)).run()
    assert rep.adverse[0.10] == 1.0, "20% 밀렸으므로 -10% 기준에 걸려야 한다"


# ---- 견고성 --------------------------------------------------------------

def test_series_too_short_is_skipped_not_crashed():
    rep = _analyzer({"A": _bars([100.0] * 4)}, buy_at=[0]).run()
    assert rep.n_signals == 0


def test_report_dict_is_json_serialisable():
    import json
    closes = [100.0] * 20
    rep = _analyzer({"A": _bars(closes)}, buy_at=[5]).run()
    blob = json.loads(json.dumps(rep.as_dict(), ensure_ascii=False))
    assert blob["n_signals"] == 1
    assert blob["horizons"][0]["horizon"] == 1
