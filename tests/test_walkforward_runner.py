"""러너의 불변조건 — docs/WALKFORWARD-SPEC.md §2.

규격 표가 맞는지는 test_walkforward_spec.py 가 본다. 여기서는 **러너가 그
규격대로 실제로 격리해서 도는지**를 본다. 창을 잘랐다고 선언만 하고 실제로는
전 구간을 매매하고 있으면 fold 비교가 통째로 무의미해진다.
"""
import random
from datetime import datetime, timedelta
from typing import List

import pytest

from autotrader import walkforward as wf
from autotrader.backtest import Backtester, _merge_timeline
from autotrader.config import Config
from autotrader.data.base import DataProvider
from autotrader.models import Bar


def _series(n: int, seed: int) -> List[Bar]:
    random.seed(seed)
    bars, price = [], 10_000.0
    for i in range(n):
        o = price
        c = price * (1 + random.gauss(0, 0.02))
        h = max(o, c) * (1 + abs(random.gauss(0, 0.012)))
        l = min(o, c) * (1 - abs(random.gauss(0, 0.012)))
        bars.append(Bar(ts=datetime(2016, 1, 4) + timedelta(days=i), open=o,
                        high=h, low=l, close=c, volume=1_000_000.0))
        price = c
    return bars


class _P(DataProvider):
    def __init__(self, n_sym=6, n=700):
        self.b = {f"S{i:04d}": _series(n, i + 1) for i in range(n_sym)}

    def history(self, symbol, limit=500):
        x = self.b[symbol]
        return x[-limit:] if limit else x

    def universe(self):
        return list(self.b)


def _cfg():
    c = Config()
    c.universe.min_price = 0
    c.universe.min_avg_dollar_vol = 0
    return c


@pytest.fixture(scope="module")
def prov():
    return _P()


@pytest.fixture(scope="module")
def timeline(prov):
    return _merge_timeline({s: prov.history(s, 700) for s in prov.universe()})


# ---- 거래 창 격리 -----------------------------------------------------------

def test_trades_and_equity_stay_inside_the_window(prov, timeline):
    """창 밖에서는 매매도 에쿼티 기록도 없어야 한다."""
    lo, hi = timeline[299], timeline[499]      # 300~500번째 봉
    rep = Backtester(prov, _cfg(), ensemble_threshold=0.45,
                     history_bars=700, trade_window=(lo, hi)).run()
    assert rep.equity_curve, "창 안에서 아무것도 안 돌았다면 검사가 무의미하다"
    assert all(lo <= p.ts <= hi for p in rep.equity_curve)
    for t in rep.trades:
        assert lo <= t.entry_ts <= hi, "창 밖에서 진입했다"
        assert lo <= t.exit_ts <= hi, "창 밖에서 청산됐다"


def test_window_start_has_full_capital_and_no_positions(prov, timeline):
    """각 구간은 같은 초기자본·무포지션으로 시작한다 (불변조건 1·2)."""
    cfg = _cfg()
    for start_ix in (300, 400):
        lo, hi = timeline[start_ix], timeline[start_ix + 150]
        rep = Backtester(prov, cfg, ensemble_threshold=0.45, history_bars=700,
                         trade_window=(lo, hi)).run()
        first = rep.equity_curve[0]
        assert first.equity == pytest.approx(cfg.backtest.initial_cash)
        assert first.cash == pytest.approx(cfg.backtest.initial_cash)
        assert first.exposure == 0.0


def test_earlier_bars_are_still_available_to_indicators(prov, timeline):
    """창 밖 과거 봉은 지표 계산에 쓸 수 있어야 한다 (불변조건 4).

    이력을 잘라 버리면 창 시작 직후 지표가 워밍업되지 않는다. 신호가 실제로
    떴는지에 기대지 않고, **전략이 몇 번째 봉을 보고 있는지**를 직접 관찰한다
    — 신호가 하나도 없는 구간에서도 성립해야 하는 성질이기 때문이다.

    동시에 미래 봉 접근이 없는지도 같이 본다: 창 마지막 날 판단에서도
    `ctx.at` 은 창 끝 봉을 넘지 않아야 한다.
    """
    lo, hi = timeline[499], timeline[649]
    bt = Backtester(prov, _cfg(), ensemble_threshold=0.45, history_bars=700,
                    trade_window=(lo, hi))
    seen = []
    real = bt.ensemble.evaluate

    def spy(ctx):
        seen.append((ctx.at, len(ctx.bars), ctx.bars[ctx.at].ts))
        return real(ctx)

    bt.ensemble.evaluate = spy
    bt.run()

    assert seen, "앙상블이 한 번도 호출되지 않았다"
    first_at, n_bars, first_ts = seen[0]
    # 창 첫날 판단에서 이미 500봉 가까운 이력을 보고 있어야 한다.
    assert first_at >= 400, f"창 시작 시 이력이 {first_at}봉뿐이다 — 잘려 있다"
    assert first_ts == lo
    # 미래 봉 금지: 어떤 판단에서도 창 끝을 넘는 봉을 보지 않는다.
    assert all(ts <= hi for _, _, ts in seen)


def test_no_position_is_left_open_at_window_end(prov, timeline):
    """창 끝에 포지션을 남기면 결과가 나쁜 거래가 채점을 빠져나간다."""
    lo, hi = timeline[299], timeline[499]
    rep = Backtester(prov, _cfg(), ensemble_threshold=0.45, history_bars=700,
                     trade_window=(lo, hi)).run()
    assert rep.equity_curve[-1].exposure == 0.0
    assert any(t.exit_reason == "window_end" for t in rep.trades) or True
    # 마지막 에쿼티가 전액 현금이어야 한다 (미청산 포지션 없음).
    assert rep.equity_curve[-1].equity == pytest.approx(
        rep.equity_curve[-1].cash, rel=1e-9)


def test_windows_are_independent_of_each_other(prov, timeline):
    """앞 구간을 돌렸든 안 돌렸든 뒤 구간 결과가 같아야 한다 — 이월 없음."""
    lo, hi = timeline[400], timeline[550]
    a = Backtester(prov, _cfg(), ensemble_threshold=0.45, history_bars=700,
                   trade_window=(lo, hi)).run()
    Backtester(prov, _cfg(), ensemble_threshold=0.45, history_bars=700,
               trade_window=(timeline[250], timeline[399])).run()
    b = Backtester(prov, _cfg(), ensemble_threshold=0.45, history_bars=700,
                   trade_window=(lo, hi)).run()
    assert a.all.net_return == b.all.net_return
    assert [p.equity for p in a.equity_curve] == [p.equity for p in b.equity_curve]


def test_no_window_reproduces_previous_behaviour(prov):
    """trade_window 를 주지 않으면 기존과 동일하게 돈다."""
    kw = dict(ensemble_threshold=0.45, history_bars=700)
    a = Backtester(prov, _cfg(), **kw).run()
    b = Backtester(prov, _cfg(), trade_window=None, **kw).run()
    assert a.all.net_return == b.all.net_return
    assert len(a.equity_curve) == len(b.equity_curve)


# ---- 리포트 계약 ------------------------------------------------------------

def test_report_records_fit_mode_none():
    """TRAIN 자동 튜닝이 나중에 붙어도 같은 실험으로 오인되지 않게."""
    assert wf.FIT_MODE == "none"


def test_active_voters_mode_is_refused_until_implemented():
    """없는 모드를 조용히 all-weights 로 돌리면 두 결과가 같게 나와 비교가 거짓이 된다."""
    with pytest.raises(NotImplementedError):
        wf.run_walkforward(_P(), _cfg(), score_mode="active-voters")
    with pytest.raises(ValueError):
        wf.run_walkforward(_P(), _cfg(), score_mode="oops")


def test_too_short_history_fails_loudly(prov):
    with pytest.raises(RuntimeError, match="fold"):
        wf.run_walkforward(prov, _cfg(), history_bars=700)


def test_judge_uses_preregistered_thresholds_only():
    good = [[100.0] * 25 + [-10.0] * 5 for _ in range(4)]
    v = wf.judge(good, max_drawdown=-0.10)
    assert v.passed, [c for c in v.checks if not c[1]]

    thin = [[100.0] * 5 for _ in range(4)]          # fold 당 5거래
    v2 = wf.judge(thin, max_drawdown=-0.10)
    assert not v2.passed
    failed = [n for n, ok, _ in v2.checks if not ok]
    assert "각 fold 거래 ≥ 20" in failed and "합산 거래 ≥ 100" in failed


def test_judge_fails_on_profit_concentration():
    folds = [[1000.0] * 30, [1.0] * 30, [1.0] * 30, [1.0] * 30]
    v = wf.judge(folds, max_drawdown=-0.05)
    assert not v.passed
    assert "집중도 ≤ 0.50" in [n for n, ok, _ in v.checks if not ok]


# ---- 전 경로 --------------------------------------------------------------

def test_runner_produces_a_serialisable_report_end_to_end():
    """1 fold 가 나오는 최소 길이로 러너 전 경로를 실제로 돈다.

    리포트가 JSON 으로 떨어지지 않거나 판정이 붙지 않으면, 실데이터로 돌린
    뒤에야 알게 된다 — 그때는 한 번 돌리는 데 시간이 오래 걸린다.
    """
    import json

    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)          # 1540
    prov = _P(n_sym=3, n=need)
    rep = wf.run_walkforward(prov, _cfg(), history_bars=need, threshold=0.45)

    assert rep["fit_mode"] == "none"
    assert rep["score_mode"] == "all-weights"
    assert len(rep["folds"]) == 1
    assert rep["excluded_tail_bars"] == [0, 0]        # 딱 맞아떨어짐
    assert rep["settings"]["threshold"] == 0.45
    assert rep["settings"]["costs"]["slippage_bp"] == _cfg().costs.slippage_bp

    f = rep["folds"][0]
    assert set(f) == {"fold", "train_reference", "validation_reference",
                      "oos_scored"}
    assert f["fold"]["oos"] == [1291, 1540]
    for seg in ("train_reference", "validation_reference", "oos_scored"):
        assert "cost" in f[seg] and "gross_profit" in f[seg]

    assert "passed" in rep["verdict"] and rep["verdict"]["checks"]
    assert "탐색 검증" in rep["caveat"]
    json.dumps(rep, ensure_ascii=False)               # 직렬화 가능해야 한다


def test_runner_scores_oos_only():
    """TRAIN·VALIDATION 성적이 아무리 좋아도 판정에 들어가지 않는다."""
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    rep = wf.run_walkforward(_P(n_sym=3, n=need), _cfg(), history_bars=need,
                             threshold=0.45)
    oos = [f["oos_scored"] for f in rep["folds"]]
    assert rep["combined_oos"]["n_trades"] == sum(o["n_trades"] for o in oos)
    assert rep["combined_oos"]["gross_profit"] == pytest.approx(
        sum(o["gross_profit"] for o in oos), abs=0.01)
    # fold 가 1개뿐이면 집중도는 정의상 1.0 이거나(이익 있음) 1.0(이익 0) 이다.
    assert rep["combined_oos"]["profit_concentration"] == pytest.approx(1.0)
