from datetime import datetime, timedelta
from autotrader.data.synthetic import generate_bars
from autotrader.strategy import (DayBreakout, DayPullback, DayMomentum,
                                 SwingTrend, MeanReversion, Ensemble)
from autotrader.strategy.base import StrategyContext
from autotrader.config import StrategyWeights


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
