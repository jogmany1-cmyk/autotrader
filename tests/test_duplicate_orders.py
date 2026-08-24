"""같은 신호로 두 번 주문이 나가면 안 된다 (idempotency).

재시도·재시작·스트림 중복 어디에서 와도 같은 신호는 같은 주문 id 를 낸다.
"""
from datetime import datetime, timedelta

import pytest

from autotrader.models import Side
from autotrader.orders import entry_order_id, exit_order_id


TS = datetime(2024, 3, 4, 9, 31, 0)


def test_same_signal_gives_the_same_id():
    a = entry_order_id("Ens", "005930", TS, Side.BUY)
    b = entry_order_id("Ens", "005930", TS, Side.BUY)
    assert a == b


def test_seconds_within_the_same_minute_do_not_split_the_id():
    """초 단위로 다르게 찍히면 중복 방지가 뚫린다."""
    a = entry_order_id("Ens", "005930", TS, Side.BUY)
    b = entry_order_id("Ens", "005930", TS.replace(second=47), Side.BUY)
    assert a == b


@pytest.mark.parametrize("kw", [
    dict(strategy="Other"),
    dict(symbol="000660"),
    dict(signal_ts=TS + timedelta(minutes=1)),
    dict(side=Side.SELL),
])
def test_different_signals_give_different_ids(kw):
    base = dict(strategy="Ens", symbol="005930", signal_ts=TS, side=Side.BUY)
    assert entry_order_id(**base) != entry_order_id(**{**base, **kw})


def test_exit_ids_separate_two_holdings_of_the_same_symbol():
    """같은 종목을 팔았다 다시 사서 또 팔 때 두 청산이 섞이면 안 된다."""
    first = exit_order_id("005930", "stop", TS, datetime(2024, 1, 5))
    second = exit_order_id("005930", "stop", TS, datetime(2024, 2, 20))
    assert first != second


def test_entry_and_exit_ids_never_collide():
    assert entry_order_id("Ens", "005930", TS, Side.BUY)[0] != \
           exit_order_id("005930", "stop", TS)[0]


# ---- 실제 차단이 되는가 -----------------------------------------------

def test_live_blocks_a_second_order_for_the_same_signal():
    import inspect

    from autotrader import live
    src = inspect.getsource(live.LiveTrader.cycle)
    assert "entry_order_id(" in src, "진입 주문에 결정적 id 를 붙이지 않는다"
    i = src.index("entry_order_id(")
    tail = src[i:i + 400]
    assert "self.book.get(" in tail, "이미 낸 주문인지 장부에서 확인하지 않는다"


def test_rejected_entry_is_not_recorded_as_a_position():
    """거부는 예외가 아니라 상태다 — 여기서 안 걸러내면 유령 포지션이 된다."""
    import inspect

    from autotrader import live
    src = inspect.getsource(live.LiveTrader.cycle)
    i = src.index("self.book.add(bo)")
    j = src.index("OrderStatus.REJECTED", i)
    # 거부 분기가 끝나기 전에 continue 가 있어야 한다. 다음 분기(체결 처리)
    # 시작 전까지를 본다.
    k = src.index("orders_placed += 1", j)
    assert "continue" in src[j:k], "거부된 주문이 보유로 기록된다"
