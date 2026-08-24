"""노출 한도가 실제로 한도 역할을 하는지.

두 가지 모두 "한도가 있는데 지켜지지 않는다" 는 한 가지 실패 양식이다.
백테스트 결과까지 함께 오염되므로, 페이퍼 트레이딩 전에 반드시 막아야 한다.
"""
from datetime import datetime

import pytest

from autotrader.config import RiskLimits
from autotrader.models import Position
from autotrader.risk import RiskEngine


def _pos(symbol, qty, avg_price):
    return Position(symbol=symbol, qty=qty, avg_price=avg_price,
                    opened_at=datetime(2024, 1, 1))


def _engine(**kw):
    cfg = RiskLimits(**kw)
    eng = RiskEngine(cfg)
    eng.new_day(datetime(2024, 1, 2).date(), 10_000_000.0)
    return eng


# ---- gross exposure ---------------------------------------------------

def test_gross_uses_each_positions_own_price_not_the_candidates():
    """후보 가격을 보유 전부에 곱하면 노출이 몇 배씩 틀린다.

    5,000원짜리 10주(=5만원)를 들고 70,000원짜리 후보를 볼 때, 예전 계산은
    보유분을 70만원으로 봤다 — 14배 과대평가다.
    """
    positions = {"CHEAP": _pos("CHEAP", 10, 5_000.0)}
    eng = _engine(max_gross_exposure=0.10)   # 100만원까지 허용
    d = eng.evaluate_entry(
        symbol="EXPENSIVE", price=70_000.0, stop_price=68_000.0,
        equity=10_000_000.0, cash=10_000_000.0, positions=positions,
        position_prices={"CHEAP": 5_000.0},
    )
    assert d.reason != "gross-exposure", (
        "보유 5만원인데 노출 한도 100만원에 걸렸다 — 후보 가격을 보유에 곱하고 있다")
    assert d.allowed


def test_gross_still_blocks_when_actually_over_the_limit():
    """반대 방향도 확인 — 한도를 없앤 게 아니라 바로잡은 것이다."""
    positions = {"BIG": _pos("BIG", 100, 90_000.0)}   # 900만원
    eng = _engine(max_gross_exposure=0.10)            # 100만원까지
    d = eng.evaluate_entry(
        symbol="NEW", price=1_000.0, stop_price=970.0,
        equity=10_000_000.0, cash=1_000_000.0, positions=positions,
        position_prices={"BIG": 90_000.0},
    )
    assert d.reason == "gross-exposure"


def test_gross_falls_back_to_avg_price_when_mark_is_missing():
    """시세를 못 받은 종목은 평균매입가로 — 후보 가격보다 항상 낫다."""
    positions = {"HELD": _pos("HELD", 100, 90_000.0)}
    eng = _engine(max_gross_exposure=0.10)
    d = eng.evaluate_entry(
        symbol="NEW", price=1_000.0, stop_price=970.0,
        equity=10_000_000.0, cash=1_000_000.0, positions=positions,
        position_prices={},          # 시세 없음
    )
    assert d.reason == "gross-exposure"


def test_gross_reflects_price_moves_not_just_cost():
    """평가액이므로, 보유 종목이 오르면 노출도 커져야 한다."""
    positions = {"HELD": _pos("HELD", 100, 50_000.0)}   # 원가 500만원
    eng = _engine(max_gross_exposure=0.60)              # 600만원까지
    at_cost = eng.evaluate_entry(
        symbol="NEW", price=1_000.0, stop_price=970.0,
        equity=10_000_000.0, cash=5_000_000.0, positions=positions,
        position_prices={"HELD": 50_000.0})
    assert at_cost.allowed
    doubled = eng.evaluate_entry(
        symbol="NEW", price=1_000.0, stop_price=970.0,
        equity=10_000_000.0, cash=5_000_000.0, positions=positions,
        position_prices={"HELD": 100_000.0})   # 두 배 오름 → 1,000만원
    assert doubled.reason == "gross-exposure"


# ---- 같은 사이클 안에서 한도가 유지되는가 ----------------------------

def test_live_cycle_counts_orders_placed_in_the_same_cycle():
    """주문을 내고도 positions 를 안 고치면 한 사이클 동안 한도가 무력해진다."""
    import inspect

    from autotrader import live
    src = inspect.getsource(live.LiveTrader.cycle)
    i = src.index("orders_placed += 1")
    tail = src[i:i + 800]
    assert "positions[cand.symbol]" in tail, \
        "주문 후 positions 를 갱신하지 않는다 — 같은 사이클에 한도 초과 가능"
