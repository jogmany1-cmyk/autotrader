"""실계좌에서도 청산이 걸리는가 — v0.9 에서 가장 위험했던 구멍.

청산 규칙이 PaperBroker.mark() 안에만 있었다. 실브로커로 돌리면 손절·트레일링·
시간청산이 **하나도 동작하지 않는다.** 백테스트에서 검증한 것과 전혀 다른 것이
실계좌에서 돈다는 뜻이다.
"""
from datetime import datetime, timedelta

import pytest

from autotrader.config import Config, Costs
from autotrader.exits import evaluate_exit, evaluate_exit_live
from autotrader.models import Bar, Order, Position, Side
from autotrader.orders import BrokerOrder, OrderStatus
from autotrader.portfolio import update_trailing_stop


def _pos(**kw):
    base = dict(symbol="005930", qty=10, avg_price=100.0,
                opened_at=datetime(2024, 1, 1))
    base.update(kw)
    return Position(**base)


def _bar(o, h, l, c):
    return Bar(datetime(2024, 1, 2), o, h, l, c, 1000)


# ---- 판정이 두 경로에서 같은가 ---------------------------------------

def test_hard_stop_wins_over_everything():
    pos = _pos(stop_price=95.0, take_price=110.0, bars_held=99)
    sig = evaluate_exit(pos, _bar(100, 111, 80, 85), max_hold=1,
                        hard_stop_pct=0.10)
    assert sig is not None and sig.reason == "hard_stop"


def test_stop_wins_over_target_in_the_same_bar():
    """한 봉에서 둘 다 닿으면 손실 쪽을 택한다 (보수적 가정)."""
    pos = _pos(stop_price=95.0, take_price=110.0)
    sig = evaluate_exit(pos, _bar(100, 115, 90, 112))
    assert sig is not None and sig.reason == "stop"


def test_trail_and_stop_keep_separate_names():
    plain = evaluate_exit(_pos(stop_price=95.0), _bar(100, 101, 90, 96))
    trailed = evaluate_exit(_pos(stop_price=95.0, stop_from_trail=True),
                            _bar(100, 101, 90, 96))
    assert plain.reason == "stop"
    assert trailed.reason == "trail"


@pytest.mark.parametrize("reason,pos_kw,price", [
    ("hard_stop", dict(stop_price=95.0), 85.0),
    ("stop", dict(stop_price=95.0), 94.0),
    ("trail", dict(stop_price=95.0, stop_from_trail=True), 94.0),
    ("target", dict(take_price=110.0), 111.0),
])
def test_live_path_gives_the_same_reason_as_the_bar_path(reason, pos_kw, price):
    """실시간 판정과 봉 판정의 우선순위가 다르면 페이퍼 검증이 무의미하다."""
    sig = evaluate_exit_live(_pos(**pos_kw), price, hard_stop_pct=0.10)
    assert sig is not None and sig.reason == reason


def test_time_exit_fires_in_the_live_path_too():
    """보유기간 상한도 실계좌에서 걸려야 한다 — 페이퍼에만 있으면 무한 보유."""
    pos = _pos(stop_price=None, take_price=None, bars_held=41)
    sig = evaluate_exit_live(pos, 105.0, max_hold=40)
    assert sig is not None and sig.reason == "time"
    assert evaluate_exit_live(_pos(bars_held=3), 105.0, max_hold=40) is None


def test_no_exit_when_nothing_is_hit():
    assert evaluate_exit_live(_pos(stop_price=95.0, take_price=110.0), 100.0) is None


# ---- 실브로커 디스패치 ------------------------------------------------

class FakeBroker:
    """접수만 하고 체결은 하지 않는 브로커 — 실계좌의 정상 동작."""

    def __init__(self):
        self.sent = []
        self.reject = False

    def submit(self, order, price_hint):
        self.sent.append(order)
        bo = BrokerOrder(client_order_id=order.client_order_id,
                         symbol=order.symbol, side=order.side, qty=order.qty,
                         status=OrderStatus.SUBMITTED, tag=order.tag)
        bo.transition(OrderStatus.REJECTED, reason="증거금 부족") if self.reject \
            else bo.transition(OrderStatus.ACCEPTED)
        return bo

    def cash(self):
        return 10_000_000.0

    def positions(self):
        return {}


class FakeProvider:
    def __init__(self, price=90.0):
        self.price = price

    def last_price(self, symbol):
        return self.price

    def universe(self):
        return []

    def history(self, symbol, limit=500):
        return []


def _trader(broker, provider, dry_run=False):
    from autotrader.live import LiveTrader
    return LiveTrader(provider, broker, Config(), dry_run=dry_run)


def _report():
    from autotrader.live import CycleReport
    return CycleReport(ts=datetime(2024, 1, 2, 10), candidates=0, signals=0,
                       orders_placed=0, orders_rejected=0, closed_trades=0)


def test_real_broker_gets_a_sell_order_when_the_stop_is_hit():
    # 94 는 전략 스탑(95)에는 걸리고 계좌 하드스탑(-10% → 90)에는 안 걸린다.
    br, pr = FakeBroker(), FakeProvider(price=94.0)
    lt = _trader(br, pr)
    positions = {"005930": _pos(stop_price=95.0)}
    n = lt._dispatch_real_exits(positions, datetime(2024, 1, 2, 10), _report())
    assert n == 1, "실계좌에서 스탑이 걸렸는데 청산 주문이 나가지 않았다"
    assert br.sent[0].side is Side.SELL
    assert br.sent[0].tag == "stop"


def test_no_order_when_no_exit_condition():
    br, pr = FakeBroker(), FakeProvider(price=100.0)
    lt = _trader(br, pr)
    n = lt._dispatch_real_exits({"005930": _pos(stop_price=95.0)},
                                datetime(2024, 1, 2, 10), _report())
    assert n == 0 and br.sent == []


def test_dry_run_never_sends_an_exit_order():
    br, pr = FakeBroker(), FakeProvider(price=90.0)
    lt = _trader(br, pr, dry_run=True)
    rep = _report()
    lt._dispatch_real_exits({"005930": _pos(stop_price=95.0)},
                            datetime(2024, 1, 2, 10), rep)
    assert br.sent == []
    assert any("[DRY]" in d for d in rep.details)


def test_rejected_exit_order_is_not_counted_as_closed():
    br, pr = FakeBroker(), FakeProvider(price=90.0)
    br.reject = True
    lt = _trader(br, pr)
    rep = _report()
    n = lt._dispatch_real_exits({"005930": _pos(stop_price=95.0)},
                                datetime(2024, 1, 2, 10), rep)
    assert n == 0, "거부된 청산 주문을 청산 완료로 셌다"
    assert any("거부" in d for d in rep.details)


def test_price_lookup_failure_is_reported_not_swallowed():
    """시세를 못 받으면 스탑이 걸려야 할 포지션이 방치된다 — 반드시 남긴다."""
    class Broken(FakeProvider):
        def last_price(self, symbol):
            raise RuntimeError("timeout")

    br = FakeBroker()
    lt = _trader(br, Broken())
    rep = _report()
    lt._dispatch_real_exits({"005930": _pos(stop_price=95.0)},
                            datetime(2024, 1, 2, 10), rep)
    assert any("시세 조회 실패" in d for d in rep.details)


def test_exit_dispatch_does_not_record_pnl_before_the_fill():
    """접수는 청산이 아니다. 체결도 안 됐는데 손익을 기록하면 유령이 된다."""
    br, pr = FakeBroker(), FakeProvider(price=90.0)
    lt = _trader(br, pr)
    before = lt.risk.state.day_realized_pnl
    lt._dispatch_real_exits({"005930": _pos(stop_price=95.0)},
                            datetime(2024, 1, 2, 10), _report())
    assert lt.risk.state.day_realized_pnl == before


# ---- 트레일링이 실계좌에서도 움직이는가 ------------------------------

def test_trailing_stop_helper_raises_the_stop():
    pos = _pos(stop_price=95.0, highest_close=100.0)
    assert update_trailing_stop(pos, 120.0, 0.05) is True
    assert pos.stop_price == pytest.approx(114.0)
    assert pos.stop_from_trail is True


def test_trailing_stop_never_goes_down():
    pos = _pos(stop_price=114.0, highest_close=120.0, stop_from_trail=True)
    assert update_trailing_stop(pos, 100.0, 0.05) is False
    assert pos.stop_price == pytest.approx(114.0)
    # 최고가 기록 자체도 되돌아가면 안 된다. 여기가 내려가면 다음 상승에서
    # 스탑이 엉뚱하게 낮은 값으로 다시 세워진다.
    assert pos.highest_close == pytest.approx(120.0)


def test_position_specific_width_beats_the_account_default():
    pos = _pos(stop_price=None, highest_close=100.0, trail_pct=0.20)
    update_trailing_stop(pos, 120.0, 0.05)
    assert pos.stop_price == pytest.approx(96.0)   # 120 * 0.80, 계좌 기본값 아님


def test_live_cycle_updates_trailing_for_real_brokers():
    """실계좌에 스탑을 끌어올리는 주체가 없으면 트레일링은 이름만 남는다."""
    import inspect

    from autotrader import live
    src = inspect.getsource(live.LiveTrader.cycle)
    i = src.index("_dispatch_real_exits")
    assert "update_trailing_stop" in src[max(0, i - 600):i], \
        "실계좌 경로에서 트레일링 스탑을 갱신하지 않는다"


# ---- 배선: cycle() 이 실제로 청산 경로를 부르는가 --------------------

def test_cycle_dispatches_exits_for_a_real_broker():
    """`_dispatch_real_exits` 가 있어도 cycle() 이 안 부르면 소용없다.

    이것이 원래 버그의 정확한 모양이다 — 청산 코드는 있는데 실브로커 경로에는
    연결돼 있지 않았다.
    """
    from autotrader.live import LiveTrader

    class Held(FakeBroker):
        def positions(self):
            return {"005930": _pos(stop_price=95.0)}

    class Pr(FakeProvider):
        def universe(self):
            return ["005930"]

        def history(self, symbol, limit=500):
            return [Bar(datetime(2024, 1, 2), 94.0, 94.0, 94.0, 94.0, 1000)]

    br = Held()
    lt = LiveTrader(Pr(price=94.0), br, Config(), dry_run=False)
    lt.cycle(datetime(2024, 1, 2, 10, 0))
    assert br.sent, "실브로커인데 cycle() 에서 청산 주문이 한 건도 나가지 않았다"
    assert br.sent[0].side is Side.SELL
