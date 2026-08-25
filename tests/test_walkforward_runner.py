"""러너의 불변조건 — docs/WALKFORWARD-SPEC.md §2.

규격 표가 맞는지는 test_walkforward_spec.py 가 본다. 여기서는 **러너가 그
규격대로 실제로 격리해서 도는지**를 본다. 창을 잘랐다고 선언만 하고 실제로는
전 구간을 매매하고 있으면 fold 비교가 통째로 무의미해진다.
"""
import copy
import random
from datetime import datetime, timedelta
from typing import List

import pytest

from autotrader import walkforward as wf
from autotrader.backtest import Backtester, _merge_timeline
from autotrader.config import Config
from autotrader.data.base import DataProvider
from autotrader.market import is_trading_day
from autotrader.models import Bar, Side, Signal
from autotrader.strategy.base import Strategy, StrategyResult


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


class _AlwaysBuy(Strategy):
    """항상 매수하는 stub. 창 끝에 포지션이 반드시 남게 만든다.

    name 을 swing_trend 로 두어 StrategyWeights 의 기존 필드를 빌린다. 단독으로
    쓰면 total_w 가 그 가중치 하나뿐이라 점수 = strength 가 된다.
    """
    name = "swing_trend"
    warmup = 5

    def evaluate(self, ctx):
        px = ctx.bars[ctx.at].close
        return StrategyResult(Signal(Side.BUY, 0.95, "stub"),
                              stop_hint=px * 0.5, target_hint=px * 5.0)


def test_window_end_actually_closes_open_positions(prov, timeline):
    """창 끝 강제청산이 실제로 일어나는지 — 포지션이 보장되는 stub 으로 본다.

    이전 판은 `assert ... or True` 라 무조건 통과했다. 그런 단언은 검사가
    아니라 검사가 있다는 착시다.
    """
    lo, hi = timeline[299], timeline[499]
    rep = Backtester(prov, _cfg(), strategies=[_AlwaysBuy()],
                     ensemble_threshold=0.45, history_bars=700,
                     trade_window=(lo, hi)).run()
    forced = [t for t in rep.trades if t.exit_reason == "window_end"]
    assert forced, "창 끝에 열린 포지션이 없어 강제청산을 검사할 수 없다"
    assert all(t.exit_ts == hi for t in forced)
    assert rep.equity_curve[-1].exposure == 0.0
    assert rep.equity_curve[-1].equity == pytest.approx(
        rep.equity_curve[-1].cash, rel=1e-9)


def test_window_end_closes_symbols_absent_on_the_final_date():
    """마지막 글로벌 날짜에 봉이 없는 종목도 마지막 관측 종가로 청산된다.

    그날의 prices 만 쓰면 그런 종목은 청산되지 않은 채 남는데, exposure 를
    0 으로 강제하면 리포트에는 청산된 것처럼 보인다 — 손실이 사라진다.
    """
    class _Sparse(DataProvider):
        def __init__(self):
            # SHORT 는 LONG 보다 60봉 일찍 끝난다.
            self.b = {"LONG": _series(400, 11), "SHORT": _series(340, 12)}

        def history(self, symbol, limit=500):
            x = self.b[symbol]
            return x[-limit:] if limit else x

        def universe(self):
            return list(self.b)

    prov = _Sparse()
    tl = _merge_timeline({s: prov.history(s, 400) for s in prov.universe()})
    assert prov.b["SHORT"][-1].ts < tl[-1], "희소 상황이 만들어지지 않았다"

    lo, hi = tl[100], tl[-1]
    rep = Backtester(prov, _cfg(), strategies=[_AlwaysBuy()],
                     ensemble_threshold=0.45, history_bars=400,
                     trade_window=(lo, hi)).run()

    closed_syms = {t.symbol for t in rep.trades}
    assert "SHORT" in closed_syms, "마지막 날 봉이 없는 종목이 청산되지 않았다"
    # 강제청산 뒤에는 노출이 0 이어야 하고, 그 값은 강제된 것이 아니라
    # 브로커에서 다시 읽은 값이어야 한다.
    assert rep.equity_curve[-1].exposure == 0.0
    assert rep.equity_curve[-1].equity == pytest.approx(
        rep.equity_curve[-1].cash, rel=1e-9)
    # 청산에 비용이 붙었는지 (수수료·세금·슬리피지 유지)
    assert rep.cost_audit is not None and rep.cost_audit.total_taxes > 0


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


def test_active_voters_mode_runs_and_is_recorded():
    """실험 모드가 실제 실행 경로에 전달되고 리포트에 남아야 한다."""
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    rep = wf.run_walkforward(_P(n_sym=1, n=need), _cfg(),
                             history_bars=need,
                             score_mode="active-voters")
    assert rep["score_mode"] == "active-voters"


def test_unknown_score_mode_is_refused():
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


# ---- 시간축 고정 ------------------------------------------------------------

class _Staggered(DataProvider):
    """시작일·종료일이 서로 다른 종목들. 합집합이 history_bars 를 넘게 만든다."""

    def __init__(self, n_each: int, offsets):
        self.b = {}
        for i, off in enumerate(offsets):
            bars = _series(n_each, i + 21)
            self.b[f"T{i}"] = [
                Bar(ts=b.ts + timedelta(days=off), open=b.open, high=b.high,
                    low=b.low, close=b.close, volume=b.volume) for b in bars]

    def history(self, symbol, limit=500):
        x = self.b[symbol]
        return x[-limit:] if limit else x

    def universe(self):
        return list(self.b)


def test_union_of_per_symbol_history_can_exceed_the_limit():
    """전제 확인 — 종목별 limit 만큼 가져와도 합집합은 그보다 길어진다.

    이 성질이 없으면 시간축 고정이 왜 필요한지 알 수 없다.
    """
    n = 1600
    prov = _Staggered(n_each=n, offsets=(0, 200, 400))
    union = _merge_timeline({s: prov.history(s, n) for s in prov.universe()})
    assert len(union) > n, "합집합이 limit 를 넘지 않아 이 검사가 무의미하다"


def test_scoring_timeline_is_trimmed_to_the_last_history_bars():
    """합집합이 길어져도 채점 축은 정확히 최근 history_bars 개여야 한다.

    자르지 않으면 fold 가 규격보다 많이 생겨, 사전 등록한 배치가 실행 시점에
    달라진다 — 실행 후 규격을 바꾸는 것과 같다.
    """
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)          # 1540
    prov = _Staggered(n_each=need, offsets=(0, 150, 300))
    union = _merge_timeline({s: prov.history(s, need) for s in prov.universe()})

    rep = wf.run_walkforward(prov, _cfg(), history_bars=need, threshold=0.45)
    s = rep["settings"]
    assert s["n_bars_timeline"] == need
    assert s["merged_union_bars"] == len(union)
    assert s["trimmed_leading_bars"] == len(union) - need > 0
    assert s["timeline_end"] == union[-1].date().isoformat()
    assert s["timeline_start"] == union[-need].date().isoformat()
    # 자르지 않았다면 fold 가 1개보다 많았을 것이다.
    assert len(rep["folds"]) == 1
    assert len(wf.build_folds(len(union))) > 1, "자르기 전에는 fold 가 더 많다"


def test_2500_bar_timeline_always_yields_four_folds():
    """규격의 핵심 약속 — 2,500봉이면 언제나 fold 4개."""
    assert len(wf.build_folds(2500)) == 4
    # 합집합이 얼마나 길든, 잘린 뒤 2,500 이면 4개다.
    for union_len in (2500, 2600, 3000, 4000):
        assert len(wf.build_folds(min(union_len, 2500))) == 4


def test_short_data_is_allowed_below_the_limit_then_length_checked():
    """데이터가 부족하면 2,500 미만도 허용하되 최소 길이 검사에 걸린다."""
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    ok = wf.run_walkforward(_P(n_sym=2, n=need), _cfg(), history_bars=2500,
                            threshold=0.45)
    assert ok["settings"]["n_bars_timeline"] == need < 2500
    assert ok["settings"]["trimmed_leading_bars"] == 0

    with pytest.raises(RuntimeError, match="fold"):
        wf.run_walkforward(_P(n_sym=2, n=need - 1), _cfg(), history_bars=2500)


# ---- 독립 전략 단독 실행 --------------------------------------------------

def test_independent_strategy_spec_matches_the_document():
    """규격 §6 의 표를 그대로 고정한다 — 값이 바뀌면 여기서 실패한다."""
    assert wf.INDEPENDENT_STRATEGIES == {
        "mean_reversion": 5, "swing_trend": 20, "day_momentum": 20}


def test_unknown_strategy_is_refused():
    """앙상블 전용 전략(day_breakout 등)을 단독 실행 대상으로 넣지 않는다."""
    for bad in ("day_breakout", "day_pullback", "oops"):
        with pytest.raises(ValueError, match="독립 실행 대상"):
            wf.run_walkforward(_P(n_sym=2, n=100), _cfg(), strategy=bad)


@pytest.mark.parametrize("name,hold", sorted(wf.INDEPENDENT_STRATEGIES.items()))
def test_solo_run_applies_that_strategys_max_hold(name, hold):
    """최대 보유기간이 실제로 덮어써지고 리포트에 남는지.

    기본 설정(20봉)이 조용히 쓰이면 mean_reversion 이 5봉 전략이 아니게 되는데
    리포트만 보고는 알 수 없다.
    """
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    rep = wf.run_walkforward(_P(n_sym=2, n=need), _cfg(),
                             history_bars=need, strategy=name)
    assert rep["strategy"] == name
    assert rep["max_holding_bars"] == hold
    # 앙상블 실행에는 전략 지정이 없다 — 두 리포트가 섞이지 않게.
    ens = wf.run_walkforward(_P(n_sym=2, n=need), _cfg(), history_bars=need)
    assert ens["strategy"] is None
    assert ens["max_holding_bars"] == _cfg().execution.max_holding_bars


def test_solo_run_does_not_mutate_the_callers_config():
    """설정을 복사해서 바꾼다 — 안 그러면 이어지는 다른 모드 실행이 조용히
    다른 보유기간으로 돈다."""
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    cfg = _cfg()
    before = cfg.execution.max_holding_bars
    wf.run_walkforward(_P(n_sym=2, n=need), cfg, history_bars=need,
                       strategy="mean_reversion")
    assert cfg.execution.max_holding_bars == before


def test_solo_run_keeps_stop_target_and_trailing():
    """최대 보유기간은 상한일 뿐 유일한 청산 규칙이 아니다.

    max_hold 만 남기고 손절·목표가·트레일링을 들어냈다면 청산 사유가
    time_exit(또는 window_end) 한 종류로만 나온다.
    """
    need = (wf.TRAIN_MIN_BARS + 2 * wf.PURGE_BARS
            + wf.VALIDATION_BARS + wf.OOS_BARS)
    prov, cfg = _P(n_sym=4, n=need), _cfg()
    from autotrader.backtest import Backtester, _merge_timeline
    tl = _merge_timeline({s: prov.history(s, need) for s in prov.universe()})
    solo = copy.deepcopy(cfg)
    solo.execution.max_holding_bars = wf.INDEPENDENT_STRATEGIES["swing_trend"]
    rep = Backtester(prov, solo, strategies=[wf._build_strategy("swing_trend")],
                     ensemble_threshold=0.45, history_bars=need,
                     trade_window=(tl[1290], tl[1539])).run()
    if not rep.trades:
        pytest.skip("이 구간에 거래가 없어 청산 사유를 볼 수 없다")
    reasons = {t.exit_reason for t in rep.trades}
    assert reasons - {"window_end"}, "창 끝 강제청산 말고는 청산이 없다"
    # 보유기간 상한이 지켜지는지 — 창 끝 강제청산은 예외.
    for t in rep.trades:
        if t.exit_reason == "window_end":
            continue
        # 거래일만 센다. max_hold 는 mark() 호출 횟수 기준이고, mark 는
        # 휴장일에 불리지 않는다 — 달력일로 세면 주말만큼 과다 집계된다.
        held = sum(1 for d in tl
                   if t.entry_ts < d <= t.exit_ts and is_trading_day(d.date()))
        assert held <= wf.INDEPENDENT_STRATEGIES["swing_trend"] + 1, (
            f"{t.symbol} 를 {held}봉 보유했다 (상한 20)")


def test_solo_uses_the_same_strategy_class_as_the_ensemble():
    """독립 실행이 다른 구현을 쓰면 비교가 무의미해진다."""
    from autotrader.strategy import DayMomentum, MeanReversion, SwingTrend
    assert isinstance(wf._build_strategy("mean_reversion"), MeanReversion)
    assert isinstance(wf._build_strategy("swing_trend"), SwingTrend)
    assert isinstance(wf._build_strategy("day_momentum"), DayMomentum)
