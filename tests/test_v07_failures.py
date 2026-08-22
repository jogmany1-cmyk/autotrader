"""v0.7 — 실패 사례에서 배운 리스크 강화 3종 테스트."""
from datetime import date, datetime

import pytest

from autotrader.config import Config, RiskLimits
from autotrader.metrics import build_cost_audit
from autotrader.models import Fill, Side
from autotrader.risk import RiskEngine


def test_max_trades_per_day_blocks_after_cap():
    r = RiskEngine(RiskLimits(max_trades_per_day=2))
    r.new_day(date(2026, 8, 24), 10_000_000)
    for i in range(2):
        d = r.evaluate_entry(symbol=f"S{i}", price=1000, stop_price=950,
                             equity=10_000_000, cash=10_000_000, positions={})
        assert d.allowed
        r.register_entry()
    d = r.evaluate_entry(symbol="S2", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={})
    assert not d.allowed and d.reason == "max-trades-per-day"


def test_day_counter_resets_next_day():
    r = RiskEngine(RiskLimits(max_trades_per_day=1))
    r.new_day(date(2026, 8, 24), 10_000_000)
    r.register_entry()
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={})
    assert not d.allowed  # 오늘은 상한
    r.new_day(date(2026, 8, 25), 10_000_000)
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={})
    assert d.allowed  # 새 날에는 재개


def test_chase_filter_blocks_hot_symbol():
    r = RiskEngine(RiskLimits(chase_filter_pct=0.05))
    r.new_day(date(2026, 8, 24), 10_000_000)
    d = r.evaluate_entry(symbol="HOT", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={},
                         last_bar_return=0.08)
    assert not d.allowed and d.reason.startswith("chase-filter")


def test_chase_filter_allows_normal_return():
    r = RiskEngine(RiskLimits(chase_filter_pct=0.05))
    r.new_day(date(2026, 8, 24), 10_000_000)
    d = r.evaluate_entry(symbol="OK", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={},
                         last_bar_return=0.02)
    assert d.allowed


def test_chase_filter_disabled_with_zero():
    r = RiskEngine(RiskLimits(chase_filter_pct=0.0))
    r.new_day(date(2026, 8, 24), 10_000_000)
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={},
                         last_bar_return=0.50)
    assert d.allowed  # 0 이면 필터 자체 비활성


def test_cost_audit_empty_fills_returns_zero():
    a = build_cost_audit([], initial_capital=10_000_000)
    assert a.n_fills == 0 and a.turnover_ratio == 0.0
    assert a.cost_to_capital_ratio == 0.0


def test_cost_audit_computes_ratios():
    fills = [
        Fill(datetime(2026, 8, 24), "A", Side.BUY, 10, 1000, fee=100),
        Fill(datetime(2026, 8, 24), "A", Side.SELL, 10, 1050, fee=105, tax=189),
    ]
    a = build_cost_audit(fills, initial_capital=100_000)
    # gross = 10*1000 + 10*1050 = 20500 → turnover = 0.205
    assert abs(a.turnover_ratio - 0.205) < 1e-6
    # fees+tax = 100 + 105 + 189 = 394 → cost/capital = 0.00394
    assert abs(a.cost_to_capital_ratio - 0.00394) < 1e-6
    assert a.n_fills == 2


def test_backtest_report_contains_cost_audit():
    from autotrader.backtest import Backtester
    from autotrader.data import SyntheticProvider
    p = SyntheticProvider(n=500)
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    # chase filter 를 넉넉히 완화해 신호가 살아나게 하고, 일 상한도 여유
    cfg.risk.chase_filter_pct = 0.20
    cfg.risk.max_trades_per_day = 20
    rep = Backtester(p, cfg, ensemble_threshold=0.4, ensemble_min_votes=1).run()
    assert rep.cost_audit is not None
    # 필드 존재만 확인 (트레이드가 0개여도 audit 은 붙는다)
    assert rep.cost_audit.n_fills >= 0
