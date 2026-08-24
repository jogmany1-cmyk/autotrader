"""미결 주문 장부 — 재시작 복구와 통보 라우팅."""
from datetime import date, datetime

import pytest

from autotrader.models import Side
from autotrader.orderbook import OrderBook
from autotrader.orders import BrokerOrder, ExecutionReport, OrderStatus


def _bo(cid, symbol="005930", qty=100, side=Side.BUY, broker_id=None):
    return BrokerOrder(client_order_id=cid, symbol=symbol, side=side, qty=qty,
                       status=OrderStatus.ACCEPTED, broker_order_id=broker_id)


def test_same_client_order_id_is_not_added_twice():
    """장부는 중복 주문의 마지막 방어선이다."""
    book = OrderBook()
    a = book.add(_bo("c1"))
    b = book.add(_bo("c1"))
    assert a is b
    assert len(book.orders) == 1


def test_report_for_an_unknown_order_is_reported_not_swallowed():
    """다른 단말이나 손으로 낸 주문의 통보가 같은 계좌로 들어온다.

    우리 주문인 척 반영하면 포지션 기록이 틀어진다.
    """
    book = OrderBook()
    book.add(_bo("c1", broker_id="B1"))
    assert book.apply(ExecutionReport("UNKNOWN", OrderStatus.FILLED,
                                      filled_qty=10, price=100.0)) is None


def test_report_routes_to_the_right_order():
    book = OrderBook()
    book.add(_bo("c1", symbol="AAA", broker_id="B1"))
    book.add(_bo("c2", symbol="BBB", broker_id="B2"))
    bo = book.apply(ExecutionReport("B2", OrderStatus.PARTIALLY_FILLED,
                                    filled_qty=40, price=100.0, exec_id="e1"))
    assert bo is not None and bo.symbol == "BBB"
    assert book.get("c1").filled_qty == 0


def test_open_symbols_counts_unfilled_buy_quantity():
    """미결 매수 수량을 노출에 넣지 않으면 실제 위험보다 작게 본다."""
    book = OrderBook()
    book.add(_bo("c1", symbol="AAA", qty=100, broker_id="B1"))
    book.apply(ExecutionReport("B1", OrderStatus.PARTIALLY_FILLED,
                               filled_qty=30, price=100.0, exec_id="e1"))
    assert book.open_symbols() == {"AAA": 70}


def test_filled_order_leaves_the_open_list():
    book = OrderBook()
    book.add(_bo("c1", qty=10, broker_id="B1"))
    book.apply(ExecutionReport("B1", OrderStatus.FILLED, filled_qty=10,
                               price=100.0, exec_id="e1"))
    assert book.open_orders() == []
    assert book.open_symbols() == {}


def test_rejected_orders_are_not_counted_as_daily_entries():
    book = OrderBook()
    ok = _bo("c1", broker_id="B1")
    bad = _bo("c2", broker_id="B2")
    book.add(ok)
    book.add(bad)
    book.apply(ExecutionReport("B2", OrderStatus.REJECTED, reason="증거금"))
    assert book.entries_on(ok.created_at.date()) == 1


# ---- 영속화 -----------------------------------------------------------

def test_orders_survive_a_restart(tmp_path):
    path = str(tmp_path / "orders.jsonl")
    book = OrderBook(path)
    book.add(_bo("c1", qty=100, broker_id="B1"))
    book.apply(ExecutionReport("B1", OrderStatus.PARTIALLY_FILLED,
                               filled_qty=30, price=70_000.0, exec_id="e1"))

    revived = OrderBook(path)          # 프로세스가 죽었다 살아났다
    bo = revived.get("c1")
    assert bo is not None
    assert bo.status is OrderStatus.PARTIALLY_FILLED
    assert bo.filled_qty == 30
    assert bo.remaining == 70
    assert revived.by_broker_id("B1") is bo, "브로커 주문번호 연결이 끊겼다"


def test_restart_still_blocks_a_duplicate_of_a_persisted_order(tmp_path):
    """재시작으로 중복 방지가 풀리면 같은 신호에 두 번 주문이 나간다."""
    path = str(tmp_path / "orders.jsonl")
    OrderBook(path).add(_bo("sig-abc", qty=100, broker_id="B1"))
    revived = OrderBook(path)
    again = revived.add(_bo("sig-abc", qty=100))
    assert again.broker_order_id == "B1", "재시작 후 같은 주문이 새로 만들어졌다"
    assert len(revived.orders) == 1


def test_a_torn_last_line_does_not_block_recovery(tmp_path):
    """쓰다 만 줄 하나 때문에 복구 전체가 막히면 안 된다."""
    path = str(tmp_path / "orders.jsonl")
    book = OrderBook(path)
    book.add(_bo("c1", broker_id="B1"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"client_order_id": "c2", "symbol": ')   # 전원이 나갔다
    revived = OrderBook(path)
    assert revived.get("c1") is not None
