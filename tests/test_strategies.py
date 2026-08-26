from datetime import datetime, timedelta
import pytest
from autotrader.data.synthetic import generate_bars
from autotrader.strategy import (DayBreakout, DayPullback, DayMomentum,
                                 SwingTrend, SwingTrendV2Experimental,
                                 MeanReversion, Ensemble)
from autotrader.strategy.base import StrategyContext
from autotrader.strategy.base import Strategy, StrategyResult
from autotrader.config import StrategyWeights
from autotrader.models import Bar, Side, Signal


class _Fixed(Strategy):
    warmup = 0

    def __init__(self, name, side, strength):
        self.name = name
        self.side = side
        self.strength = strength

    def evaluate(self, ctx):
        return StrategyResult(Signal(self.side, self.strength))


def test_all_strategies_return_valid_signal():
    bars = generate_bars("TST", n=500, seed=1)
    for cls in (DayBreakout, DayPullback, DayMomentum, SwingTrend,
               SwingTrendV2Experimental, MeanReversion):
        s = cls()
        result = s.evaluate(StrategyContext("TST", bars, len(bars) - 1))
        assert result.signal.side.value in {"BUY", "HOLD"}
        assert 0.0 <= result.signal.strength <= 1.0


def test_ensemble_never_buys_below_threshold():
    bars = generate_bars("TST", n=500, seed=2)
    ens = Ensemble([DayBreakout(), SwingTrend()], StrategyWeights(),
                   threshold=2.0, min_votes=1)
    for i in range(300, len(bars)):
        d = ens.evaluate(StrategyContext("TST", bars, i))
        assert d.signal.side.value == "HOLD"


def test_ensemble_respects_min_votes():
    bars = generate_bars("TST", n=500, seed=3)
    strict = Ensemble([DayBreakout(), DayPullback(), DayMomentum(),
                       SwingTrend(), MeanReversion()],
                      StrategyWeights(), threshold=0.4, min_votes=5)
    hits = 0
    for i in range(250, len(bars)):
        if strict.evaluate(StrategyContext("TST", bars, i)).signal.side.value == "BUY":
            hits += 1
    # 5개 전략이 동시에 모두 매수일 확률은 사실상 0.
    assert hits == 0


def test_active_voters_changes_only_the_score_denominator():
    bars = generate_bars("TST", n=2, seed=4)
    ctx = StrategyContext("TST", bars, 1)
    strategies = [
        _Fixed("day_breakout", Side.BUY, 0.60),
        _Fixed("day_pullback", Side.HOLD, 0.0),
    ]
    weights = StrategyWeights(day_breakout=1.0, day_pullback=1.0,
                              day_momentum=0.0, swing_trend=0.0,
                              mean_reversion=0.0)

    old = Ensemble(strategies, weights, threshold=0.45,
                   score_mode="all-weights").evaluate(ctx)
    active = Ensemble(strategies, weights, threshold=0.45,
                      score_mode="active-voters").evaluate(ctx)

    assert old.score == 0.30
    assert old.signal.side is Side.HOLD
    assert active.score == 0.60
    assert active.signal.side is Side.BUY
    assert old.votes == active.votes == 1
    assert old.detail == active.detail


def test_active_voters_with_no_votes_has_zero_score():
    bars = generate_bars("TST", n=2, seed=5)
    hold = _Fixed("day_breakout", Side.HOLD, 0.0)
    decision = Ensemble([hold], StrategyWeights(),
                        score_mode="active-voters").evaluate(
                            StrategyContext("TST", bars, 1))
    assert decision.score == 0.0
    assert decision.votes == 0
    assert decision.signal.side is Side.HOLD


def test_unknown_score_mode_fails_loudly():
    with pytest.raises(ValueError, match="score_mode"):
        Ensemble([], StrategyWeights(), score_mode="oops")


def test_swing_preserves_raw_factors_when_public_score_is_clipped():
    bars = []
    for i in range(260):
        close = 100.0 * (1.004 ** i)
        bars.append(Bar(datetime(2024, 1, 1) + timedelta(days=i),
                        close, close * 1.01, close * 0.99, close, 1_000_000))
    ctx = StrategyContext("TST", bars, len(bars) - 1)
    result = SwingTrend().evaluate(ctx)

    assert result.signal.side is Side.BUY
    assert result.signal.strength == pytest.approx(0.95)
    assert result.factors["raw_strength"] > 0.95
    assert result.factors["roc_120"] > 0.45
    assert result.factors["atr_pct"] > 0

    decision = Ensemble([SwingTrend()], StrategyWeights(), threshold=0.45).evaluate(ctx)
    assert decision.factors["swing_trend.raw_strength"] > 0.95
    assert decision.factors["swing_trend.roc_120"] == pytest.approx(
        result.factors["roc_120"])


def test_swing_v2_scores_by_volatility_alone():
    """§8: 점수는 ATR/종가 하나로만 정해진다 — clip 도, 포화도 없다."""
    bars = []
    for i in range(260):
        close = 100.0 * (1.004 ** i)
        bars.append(Bar(datetime(2024, 1, 1) + timedelta(days=i),
                        close, close * 1.05, close * 0.95, close, 1_000_000))
    ctx = StrategyContext("TST", bars, len(bars) - 1)
    result = SwingTrendV2Experimental().evaluate(ctx)

    assert result.signal.side is Side.BUY
    atr_pct = result.factors["atr_pct"]
    assert atr_pct > 0
    assert result.signal.strength == pytest.approx(1.0 / (1.0 + atr_pct))
    # 원본과 달리 clip 전/후 값이 갈리지 않는다 — 애초에 clip 이 없다.
    assert "raw_strength" not in result.factors


def test_swing_v2_prefers_lower_volatility_over_higher_momentum():
    """§8 처방의 핵심: 모멘텀(120봉 수익률)이 더 커도 변동성이 크면 점수가
    낮다 — swing_trend 는 반대로 모멘텀이 클수록 점수를 더 줬다."""
    def make_bars(growth, spread_pct):
        bars = []
        for i in range(260):
            close = 100.0 * (growth ** i)
            bars.append(Bar(datetime(2024, 1, 1) + timedelta(days=i),
                            close, close * (1 + spread_pct),
                            close * (1 - spread_pct), close, 1_000_000))
        return bars

    low_vol_low_momentum = make_bars(1.003, 0.01)
    high_vol_high_momentum = make_bars(1.006, 0.08)

    r_low = SwingTrendV2Experimental().evaluate(
        StrategyContext("TST", low_vol_low_momentum, 259))
    r_high = SwingTrendV2Experimental().evaluate(
        StrategyContext("TST", high_vol_high_momentum, 259))

    assert r_low.signal.side is Side.BUY
    assert r_high.signal.side is Side.BUY
    assert r_high.factors["roc_120"] > r_low.factors["roc_120"]
    assert r_low.signal.strength > r_high.signal.strength
