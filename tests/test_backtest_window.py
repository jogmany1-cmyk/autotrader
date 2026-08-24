"""백테스트 구간을 사람이 정할 수 있는지 고정한다.

배경: 실데이터 40년치(10,914봉)를 수집해 놓고 백테스트는 999봉만 썼다.
`history(s, limit=u.lookback_days * 4)` 로 구간이 코드에 박혀 있었고 바꿀
방법이 없었다. 데이터의 91% 를 버리면 거래 표본이 4건까지 줄어, 어떤 성과
지표도 통계적 의미를 갖지 못한다.
"""
import pytest

from autotrader.backtest import Backtester
from autotrader.config import Config
from autotrader.data import SyntheticProvider


class _Recorder(SyntheticProvider):
    """어떤 limit 으로 호출됐는지 기록하는 공급자.

    주의: history() 는 두 곳에서 불린다. 백테스트가 처음에 종목별로 한 번씩
    (구간 결정), 그 뒤 스크리너가 lookback_days 만큼 또 부른다. 우리가 볼
    것은 앞쪽 — 데이터 로딩 단계의 limit 이다.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.limits = []

    def history(self, symbol, limit=500):
        self.limits.append(limit)
        return super().history(symbol, limit)

    def loading_limits(self):
        """데이터 로딩 단계(종목당 1회)에서 쓰인 limit 들."""
        return set(self.limits[:len(self.universe())])


def _cfg(provider):
    cfg = Config.default()
    cfg.universe.symbols = provider.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    return cfg


def test_default_keeps_previous_behaviour():
    """미지정이면 기존과 같아야 한다 — 조용히 결과가 바뀌면 안 된다."""
    p = _Recorder(symbols=("AAA", "BBB"), n=600)
    cfg = _cfg(p)
    Backtester(p, cfg, ensemble_threshold=0.99).run()
    assert p.loading_limits() == {cfg.universe.lookback_days * 4}


def test_explicit_bars_is_used():
    p = _Recorder(symbols=("AAA", "BBB"), n=600)
    Backtester(p, _cfg(p), ensemble_threshold=0.99, history_bars=300).run()
    assert p.loading_limits() == {300}


def test_zero_means_all_available_bars():
    """0 은 '전부' 다 — CsvProvider·KiwoomProvider 모두 limit=0 을 그렇게 해석한다."""
    p = _Recorder(symbols=("AAA",), n=600)
    Backtester(p, _cfg(p), ensemble_threshold=0.99, history_bars=0).run()
    assert p.loading_limits() == {0}


def test_longer_window_produces_a_longer_equity_curve():
    """구간을 늘리면 실제로 더 긴 기간을 재현해야 한다."""
    p_short = SyntheticProvider(symbols=("AAA", "BBB"), n=1200)
    p_long = SyntheticProvider(symbols=("AAA", "BBB"), n=1200)
    short = Backtester(p_short, _cfg(p_short), ensemble_threshold=0.99,
                       history_bars=300).run()
    long = Backtester(p_long, _cfg(p_long), ensemble_threshold=0.99,
                      history_bars=1200).run()
    assert len(long.equity_curve) > len(short.equity_curve)
