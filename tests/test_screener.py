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



def test_screener_records_stage_counts():
    from autotrader.data import SyntheticProvider
    from autotrader.config import Universe
    from autotrader.screener import Screener
    p = SyntheticProvider()
    u = Universe(symbols=p.universe(), min_price=0, min_avg_dollar_vol=0, lookback_days=500)
    sc = Screener(p, u, top_n=3)
    sc.rank()
    stats = sc.last_stats
    assert stats is not None
    assert stats.input_count == len(p.universe())
    assert stats.tier1_price_pass >= stats.tier3_ranking_pass
    assert stats.tier2_indicator_pass >= stats.tier3_ranking_pass
    assert stats.tier3_ranking_pass <= 3


def test_screener_liquidity_filter_reduces_tier1():
    from autotrader.data import SyntheticProvider
    from autotrader.config import Universe
    from autotrader.screener import Screener
    p = SyntheticProvider()
    u = Universe(symbols=p.universe(), min_price=0, min_avg_dollar_vol=1e18, lookback_days=500)
    sc = Screener(p, u, top_n=3)
    sc.rank()
    assert sc.last_stats.tier1_price_pass == 0
