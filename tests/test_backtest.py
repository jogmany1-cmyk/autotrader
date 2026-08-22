from autotrader.backtest import Backtester
from autotrader.config import Config
from autotrader.data import SyntheticProvider


def test_full_pipeline_runs_and_reports():
    p = SyntheticProvider(n=600)
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    cfg.backtest.train_ratio = 0.6
    cfg.backtest.val_ratio = 0.2
    bt = Backtester(p, cfg, ensemble_threshold=0.4, ensemble_min_votes=1, trail_pct=0.05)
    rep = bt.run()
    assert rep.equity_curve, "에쿼티 커브가 비어 있으면 안 된다"
    assert rep.all.days == len(rep.equity_curve)
    assert rep.train.days + rep.val.days + rep.oos.days == rep.all.days
    # 초기 자본이 그대로 유지된 무거래 케이스라도 net_return 은 0 근처.
    assert -0.5 < rep.all.net_return < 0.5


def test_backtest_never_generates_negative_position_qty():
    from autotrader.models import Side
    p = SyntheticProvider(n=500)
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    bt = Backtester(p, cfg, ensemble_threshold=0.4, ensemble_min_votes=1)
    rep = bt.run()
    for tr in rep.trades:
        assert tr.qty > 0
        assert tr.entry_price > 0 and tr.exit_price > 0
