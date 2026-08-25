from datetime import datetime, timedelta
import pytest
from autotrader.data.synthetic import generate_bars
from autotrader.strategy import (DayBreakout, DayPullback, DayMomentum,
                                 SwingTrend, MeanReversion, Ensemble)
from autotrader.strategy.base import StrategyContext
from autotrader.strategy.base import Strategy, StrategyResult
from autotrader.config import StrategyWeights
from autotrader.models import Side, Signal


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
    for cls in (DayBreakout, DayPullback, DayMomentum, SwingTrend, MeanReversion):
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
