from datetime import date
from autotrader.risk import RiskEngine
from autotrader.config import RiskLimits


def test_position_sizing_respects_1r():
    r = RiskEngine(RiskLimits(per_trade_risk_pct=0.01, max_position_pct=1.0, min_cash_pct=0.0))
    r.new_day(date(2024, 1, 2), 10_000_000)
    d = r.evaluate_entry(symbol="A", price=1_000, stop_price=900,
                         equity=10_000_000, cash=10_000_000, positions={}, score=1.0)
    # 1R = 100원, 예산 = 1_000_000 * 1.2, qty ≈ 12_000. score 부스팅 상한이 1.5.
    assert d.allowed and d.qty > 0
    # 위험이 1R * qty ≈ risk budget 이하여야 함.
    assert 100 * d.qty <= 10_000_000 * 0.015 + 1


def test_max_positions_gate():
    r = RiskEngine(RiskLimits(max_positions=2))
    r.new_day(date(2024, 1, 2), 10_000_000)
    from autotrader.models import Position
    from datetime import datetime
    positions = {
        "X": Position("X", 10, 100, datetime(2024, 1, 2)),
        "Y": Position("Y", 10, 100, datetime(2024, 1, 2)),
    }
    d = r.evaluate_entry(symbol="Z", price=100, stop_price=90,
                         equity=10_000_000, cash=10_000_000, positions=positions)
    assert not d.allowed and d.reason == "max-positions"


def test_daily_loss_stop():
    r = RiskEngine(RiskLimits(daily_loss_stop_pct=0.03))
    r.new_day(date(2024, 1, 2), 10_000_000)
    r.register_exit(-500_000, date(2024, 1, 2))  # -5%
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=950,
                         equity=9_500_000, cash=9_500_000, positions={})
    assert not d.allowed and d.reason == "daily-loss-stop"


def test_stop_above_price_uses_fallback():
    """stop 이 price 위인 이상한 입력은 fallback 3% stop 으로 대체된다."""
    r = RiskEngine(RiskLimits())
    r.new_day(date(2024, 1, 2), 10_000_000)
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=1100,
                         equity=10_000_000, cash=10_000_000, positions={})
    assert d.allowed and d.qty > 0 and 20 <= d.risk_per_share <= 40

def test_cooldown_after_consecutive_losses():
    r = RiskEngine(RiskLimits(max_consecutive_losses=3))
    r.new_day(date(2024, 1, 2), 10_000_000)
    for _ in range(3):
        r.register_exit(-1000, date(2024, 1, 2))
    d = r.evaluate_entry(symbol="X", price=1000, stop_price=950,
                         equity=10_000_000, cash=10_000_000, positions={})
    assert not d.allowed and d.reason in {"consec-losses", "cooldown"}
