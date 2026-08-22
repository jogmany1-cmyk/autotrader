from datetime import datetime
from autotrader.tracker import PredictionTracker, Prediction


def _entry(sym, price, conf, target, stop, ts=datetime(2026, 8, 1)):
    return Prediction(sym, ts, price, conf, 2, target, stop, "t")


def test_matching_exit_records_outcome_and_target_hit():
    t = PredictionTracker()
    t.record_entry(_entry("A", 10000, 0.72, 11000, 9500))
    out = t.record_exit(symbol="A", exit_ts=datetime(2026, 8, 5),
                        exit_price=11100, exit_reason="target")
    assert out is not None and out.hit_target and out.return_pct > 0


def test_stop_outcome_flagged_correctly():
    t = PredictionTracker()
    t.record_entry(_entry("B", 10000, 0.55, 10500, 9500))
    out = t.record_exit(symbol="B", exit_ts=datetime(2026, 8, 6),
                        exit_price=9400, exit_reason="stop")
    assert out.hit_stop and out.return_pct < 0


def test_report_aggregates_by_confidence_bucket():
    t = PredictionTracker()
    t.record_entry(_entry("A", 100, 0.72, 110, 95))
    t.record_exit(symbol="A", exit_ts=datetime(2026, 8, 5), exit_price=115, exit_reason="target")
    t.record_entry(_entry("B", 100, 0.55, 105, 95))
    t.record_exit(symbol="B", exit_ts=datetime(2026, 8, 6), exit_price=95, exit_reason="stop")
    r = t.report()
    assert r.n == 2 and 0 < r.win_rate <= 1.0
    assert "70-80" in r.by_confidence_bucket


def test_unmatched_exit_returns_none():
    assert PredictionTracker().record_exit(
        symbol="NOPE", exit_ts=datetime(2026, 8, 6),
        exit_price=1.0, exit_reason="stop"
    ) is None
