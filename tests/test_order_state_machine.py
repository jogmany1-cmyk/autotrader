"""접수와 체결은 같은 것이 아니다 — 상태머신 회귀 테스트.

예전 KiwoomBroker.submit() 은 접수 응답에서 곧바로 Fill 을 만들어 돌려줬다.
그러면 미체결·부분체결·거부가 전부 "전량 체결" 로 기록되고, 포트폴리오가
있지도 않은 포지션을 들고 있다고 믿는다.
"""
from datetime import datetime, timedelta

import pytest

from autotrader.models import Order, Side
from autotrader.orders import (OPEN, TERMINAL, BrokerOrder, ExecutionReport,
                               OrderStateError, OrderStatus, can_transition)


def _order(qty=100, side=Side.BUY, cid="c1"):
    return BrokerOrder(client_order_id=cid, symbol="005930", side=side,
                       qty=qty, status=OrderStatus.SUBMITTED)


def _fill(qty, price, exec_id, oid="B1"):
    return ExecutionReport(broker_order_id=oid, status=OrderStatus.PARTIALLY_FILLED,
                           filled_qty=qty, price=price, exec_id=exec_id)


# ---- 부분체결 ---------------------------------------------------------

def test_partial_fill_does_not_claim_full_position():
    bo = _order(qty=100)
    bo.apply(_fill(30, 70_000.0, "e1"))
    assert bo.status is OrderStatus.PARTIALLY_FILLED
    assert bo.filled_qty == 30
    assert bo.remaining == 70
    assert not bo.is_terminal


def test_fills_accumulate_to_filled():
    bo = _order(qty=100)
    bo.apply(_fill(30, 70_000.0, "e1"))
    bo.apply(_fill(70, 71_000.0, "e2"))
    assert bo.status is OrderStatus.FILLED
    assert bo.filled_qty == 100
    assert bo.remaining == 0
    assert bo.avg_fill_price == pytest.approx((30 * 70_000 + 70 * 71_000) / 100)
    assert len(bo.fills) == 2


# ---- 멱등성: 재연결이 보유수량을 두 배로 만들면 안 된다 --------------

def test_duplicate_execution_is_ignored():
    """재연결 직후 브로커가 놓친 통보를 다시 밀어주는 것은 정상 동작이다."""
    bo = _order(qty=100)
    assert bo.apply(_fill(50, 70_000.0, "e1")) is True
    assert bo.apply(_fill(50, 70_000.0, "e1")) is False, "중복 통보가 반영됐다"
    assert bo.filled_qty == 50


def test_replayed_stream_after_reconnect_does_not_double_the_position():
    bo = _order(qty=100)
    stream = [_fill(40, 70_000.0, "e1"), _fill(60, 70_500.0, "e2")]
    for ev in stream:
        bo.apply(ev)
    for ev in stream:            # 재연결 → 같은 통보가 다시 온다
        bo.apply(ev)
    assert bo.filled_qty == 100, "재연결 재생으로 수량이 늘었다"


def test_overfill_beyond_order_qty_is_refused():
    bo = _order(qty=100)
    bo.apply(_fill(100, 70_000.0, "e1"))
    assert bo.apply(_fill(10, 70_000.0, "e2")) is False
    assert bo.filled_qty == 100


# ---- 거부 · 취소 ------------------------------------------------------

def test_rejected_order_holds_no_position():
    bo = _order()
    bo.apply(ExecutionReport("B1", OrderStatus.REJECTED, reason="증거금 부족"))
    assert bo.status is OrderStatus.REJECTED
    assert bo.filled_qty == 0
    assert bo.reject_reason == "증거금 부족"
    assert bo.is_terminal


def test_cancel_request_is_not_cancellation():
    """취소 '요청' 과 취소 '완료' 는 다르다 — 그 사이에 체결될 수 있다."""
    bo = _order(qty=100)
    bo.transition(OrderStatus.ACCEPTED)
    bo.transition(OrderStatus.CANCEL_REQUESTED)
    assert bo.status is OrderStatus.CANCEL_REQUESTED
    assert bo.is_open, "취소 요청 상태를 종료로 취급하면 그 뒤 체결을 놓친다"
    bo.apply(_fill(40, 70_000.0, "e1"))
    assert bo.filled_qty == 40


# ---- 종료 상태에서 되돌아가지 않는다 ---------------------------------

def test_terminal_status_never_goes_back():
    """낡은 '접수' 통보가 늦게 도착해 체결된 주문을 되살리면 안 된다."""
    bo = _order()
    bo.apply(ExecutionReport("B1", OrderStatus.FILLED, filled_qty=100,
                             price=70_000.0, exec_id="e1"))
    assert bo.status is OrderStatus.FILLED
    assert bo.apply(ExecutionReport("B1", OrderStatus.ACCEPTED)) is False
    assert bo.status is OrderStatus.FILLED


@pytest.mark.parametrize("src", sorted(TERMINAL, key=lambda s: s.value))
def test_no_transition_leaves_a_terminal_state(src):
    for dst in OrderStatus:
        assert not can_transition(src, dst), f"{src.value} → {dst.value} 가 열려 있다"


def test_illegal_transition_raises_rather_than_passing_silently():
    bo = _order()
    bo.apply(ExecutionReport("B1", OrderStatus.REJECTED))
    with pytest.raises(OrderStateError):
        bo.transition(OrderStatus.FILLED)


def test_open_and_terminal_cover_every_status():
    assert OPEN | TERMINAL == set(OrderStatus)
    assert not (OPEN & TERMINAL)


# ---- 브로커 계약 ------------------------------------------------------

def test_paper_broker_returns_a_filled_broker_order():
    from autotrader.broker.paper import PaperBroker
    from autotrader.config import Costs

    br = PaperBroker(10_000_000.0, Costs())
    bo = br.submit(Order("005930", Side.BUY, 10), price_hint=70_000.0,
                   ts=datetime(2024, 1, 2))
    assert isinstance(bo, BrokerOrder)
    assert bo.status is OrderStatus.FILLED
    assert bo.filled_qty == 10


def test_kiwoom_submit_returns_accepted_not_a_fill(monkeypatch):
    """실브로커의 주문 응답은 접수다. 체결로 취급하면 유령 포지션이 생긴다."""
    pytest.importorskip("requests")
    from autotrader.broker.kiwoom import KiwoomBroker
    from autotrader.config import KiwoomConfig

    br = KiwoomBroker.__new__(KiwoomBroker)
    br.cfg = KiwoomConfig(app_key="k", app_secret="s")
    br.base = "https://example.invalid"

    class R:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"return_code": 0, "ord_no": "0001234"}

    class Http:
        @staticmethod
        def post(*a, **k):
            return R()

    monkeypatch.setattr(br, "_http", lambda: Http(), raising=False)
    monkeypatch.setattr(br, "_headers", lambda api_id: {}, raising=False)

    bo = br.submit(Order("005930", Side.BUY, 10), price_hint=70_000.0)
    assert bo.status is OrderStatus.ACCEPTED, "접수를 체결로 보고했다"
    assert bo.filled_qty == 0, "체결통보 없이 체결 수량이 잡혔다"
    assert bo.broker_order_id == "0001234"


def test_kiwoom_rejection_is_a_status_not_an_exception(monkeypatch):
    """거부를 예외로 던지면 '주문이 안 나갔다' 와 구분되지 않는다."""
    pytest.importorskip("requests")
    from autotrader.broker.kiwoom import KiwoomBroker
    from autotrader.config import KiwoomConfig

    br = KiwoomBroker.__new__(KiwoomBroker)
    br.cfg = KiwoomConfig(app_key="k", app_secret="s")
    br.base = "https://example.invalid"

    class R:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"return_code": 3, "return_msg": "증거금 부족"}

    monkeypatch.setattr(br, "_http", lambda: type("H", (), {"post": staticmethod(lambda *a, **k: R())})(), raising=False)
    monkeypatch.setattr(br, "_headers", lambda api_id: {}, raising=False)

    bo = br.submit(Order("005930", Side.BUY, 10), price_hint=70_000.0)
    assert bo.status is OrderStatus.REJECTED
    assert "증거금" in bo.reject_reason
    assert bo.filled_qty == 0
