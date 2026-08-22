from autotrader.data import SyntheticProvider
from autotrader.config import Universe
from autotrader.screener import Screener


def test_top_ranker_sorted_desc():
    p = SyntheticProvider()
    u = Universe(symbols=p.universe(), min_price=0, min_avg_dollar_vol=0, lookback_days=500)
    ranked = [r for r in Screener(p, u).rank() if r.passed]
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_low_price_filter_rejects():
    p = SyntheticProvider()
    u = Universe(symbols=p.universe(), min_price=1e12, min_avg_dollar_vol=0, lookback_days=500)
    ranked = Screener(p, u).rank()
    # 모든 종목이 min_price 를 못 넘겨서 통과된 게 하나도 없어야 한다.
    assert not [r for r in ranked if r.passed]
