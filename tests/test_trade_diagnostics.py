from datetime import datetime

import pytest

from autotrader.models import Trade
from autotrader.walkforward import combine_entry_funnels, trade_diagnostics


def _trade(year, pnl, return_pct, reason, bars, score=0.0, votes=0,
           factors=None):
    return Trade(
        symbol="TST", entry_ts=datetime(year, 1, 2),
        exit_ts=datetime(year, 2, 2), qty=1,
        entry_price=100.0, exit_price=100.0 + pnl,
        pnl=pnl, return_pct=return_pct, exit_reason=reason,
        bars_held=bars, entry_score=score, entry_votes=votes,
        entry_factors=dict(factors or {}),
    )


def test_trade_diagnostics_exposes_loss_causes_and_cost_drag():
    trades = [
        _trade(2024, 100.0, 0.10, "target", 5, 0.91, 2),
        _trade(2024, -50.0, -0.05, "stop", 2, 0.86, 1),
        _trade(2025, 50.0, 0.05, "target", 4, 0.93, 2),
        _trade(2025, 0.0, 0.00, "time_exit", 10, 0.86, 1),
    ]
    d = trade_diagnostics(trades, total_cost=30.0)

    assert d["n_trades"] == 4
    assert (d["wins"], d["losses"], d["breakeven"]) == (2, 1, 1)
    assert d["win_rate"] == 0.5
    assert d["net_profit"] == 100.0
    assert d["avg_win"] == 75.0
    assert d["avg_loss"] == -50.0
    assert d["payoff_ratio"] == 1.5
    assert d["avg_bars_held"] == 5.25
    assert d["total_cost"] == 30.0
    assert d["estimated_pre_cost_net"] == 130.0
    assert d["cost_drag_vs_gross_profit"] == 0.2

    assert d["by_exit_reason"]["target"]["n_trades"] == 2
    assert d["by_exit_reason"]["target"]["net_profit"] == 150.0
    assert d["by_exit_reason"]["stop"]["net_profit"] == -50.0
    assert set(d["by_exit_year"]) == {"2024", "2025"}
    assert d["by_exit_year"]["2024"]["net_profit"] == 50.0
    assert d["avg_entry_score"] == pytest.approx(0.89)
    assert d["avg_entry_votes"] == pytest.approx(1.5)
    assert d["by_entry_score_bucket"]["0.8-0.9"]["n_trades"] == 2
    assert d["by_entry_score_bucket"]["0.9-1.0"]["n_trades"] == 2
    assert d["by_entry_votes"]["2"]["net_profit"] == 150.0
    assert d["hard_stop_count"] == 0
    assert d["hard_stop_rate"] == 0.0


def test_empty_diagnostics_are_json_safe_and_zeroed():
    d = trade_diagnostics([], total_cost=0.0)
    assert d["n_trades"] == 0
    assert d["profit_factor"] is None
    assert d["payoff_ratio"] is None
    assert d["cost_drag_vs_gross_profit"] is None
    assert d["by_exit_reason"] == {}
    assert d["by_exit_year"] == {}
    assert d["by_entry_score_bucket"] == {}
    assert d["by_entry_votes"] == {}
    assert d["by_entry_factor"] == {}


def test_entry_factor_buckets_show_where_hard_stops_cluster():
    trades = [
        _trade(2024, -30.0, -0.03, "hard_stop", 2,
               factors={"swing_trend.roc_120": 0.52,
                        "execution.initial_stop_distance_pct": -0.01}),
        _trade(2024, -20.0, -0.02, "hard_stop", 3,
               factors={"swing_trend.roc_120": 0.61,
                        "execution.initial_stop_distance_pct": 0.02}),
        _trade(2024, 80.0, 0.08, "target", 8,
               factors={"swing_trend.roc_120": 0.22,
                        "execution.initial_stop_distance_pct": 0.08}),
    ]
    factors = trade_diagnostics(trades)["by_entry_factor"]

    roc = factors["swing_trend.roc_120"]
    assert roc["label"] == "120봉 수익률"
    assert roc["buckets"]["45%~75%"]["hard_stop_rate"] == 1.0
    assert roc["buckets"]["15%~30%"]["hard_stop_rate"] == 0.0
    stop = factors["execution.initial_stop_distance_pct"]["buckets"]
    assert stop["<0%"]["hard_stop_count"] == 1
    assert stop["6%~10%"]["net_profit"] == 80.0


def test_walkforward_combined_diagnostics_equal_oos_fold_totals():
    # 전 경로 계약은 runner 테스트가 합성 데이터로 검증한다. 여기서는 새 필드가
    # fold 합과 갈라지지 않는다는 단언을 그 테스트가 재사용할 수 있게 남긴다.
    assert trade_diagnostics([
        _trade(2024, 10.0, 0.01, "target", 1),
        _trade(2025, -5.0, -0.005, "stop", 2),
    ])["net_profit"] == pytest.approx(5.0)


def test_entry_funnels_pool_counts_and_rejection_reasons():
    a = {"strategy_evaluations": 100, "buy_signals": 10,
         "pending_attempts": 9, "entries_filled": 3,
         "unprocessed_at_window_end": 1,
         "risk_rejections": {"max-positions": 5},
         "cooldown_blocked_at_fill": 1}
    b = {"strategy_evaluations": 50, "buy_signals": 5,
         "pending_attempts": 5, "entries_filled": 4,
         "risk_rejections": {"max-positions": 1}}
    out = combine_entry_funnels([a, b])

    assert out["strategy_evaluations"] == 150
    assert out["buy_signals"] == 15
    assert out["buy_signal_rate"] == 0.1
    assert out["entries_filled"] == 7
    assert out["fill_rate_from_attempts"] == 0.5
    assert out["risk_rejections"] == {"max-positions": 6}
