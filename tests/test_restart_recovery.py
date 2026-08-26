"""재시작 복구 — 브로커가 최종 진실의 기준이다.

프로세스가 죽었다 살아나면 우리는 아무것도 모른다. 그 상태로 사이클을 돌리면
이미 들고 있는 종목에 또 들어가고, 일일 진입 상한이 0 부터 다시 세어지고,
손절선을 몰라 스탑이 영원히 안 걸린다.
"""
from datetime import date, datetime, timedelta

import pytest

from autotrader.config import Config, Costs
from autotrader.models import Position
from autotrader.recovery import (SessionState, reconcile_positions,
                                 snapshot_positions)


def _pos(symbol="005930", qty=10, avg=100.0, **kw):
    base = dict(symbol=symbol, qty=qty, avg_price=avg,
                opened_at=datetime(2024, 1, 5, 9, 30))
    base.update(kw)
    return Position(**base)


# ---- 무엇이 이기는가 --------------------------------------------------

def test_broker_wins_on_quantity_and_average_price():
    """우리가 죽어 있는 동안 부분체결됐거나 손으로 팔았을 수 있다."""
    state = SessionState(position_meta=snapshot_positions(
        {"005930": _pos(qty=100, avg=70_000.0, stop_price=66_000.0)}))
    res = reconcile_positions({"005930": _pos(qty=30, avg=71_000.0)}, state)
    pos = res.positions["005930"]
    assert pos.qty == 30
    assert pos.avg_price == 71_000.0


def test_our_record_supplies_what_the_broker_cannot_know():
    """브로커 잔고에는 손절가·목표가·트레일 폭·진입 시각이 없다.

    복구하지 않으면 재시작 이후 그 포지션에는 스탑이 영원히 걸리지 않는다.
    """
    ours = _pos(stop_price=66_000.0, take_price=80_000.0, trail_pct=0.106,
                highest_close=75_000.0, bars_held=7, stop_from_trail=True,
                entry_score=0.88, entry_votes=3,
                entry_factors={"swing_trend.roc_120": 0.52})
    state = SessionState(position_meta=snapshot_positions({"005930": ours}))
    # 브로커가 주는 것에는 이 값들이 전부 비어 있다
    bare = _pos(opened_at=datetime(2026, 8, 24, 15, 0))
    res = reconcile_positions({"005930": bare}, state)
    pos = res.positions["005930"]
    assert pos.stop_price == 66_000.0
    assert pos.take_price == 80_000.0
    assert pos.trail_pct == pytest.approx(0.106)
    assert pos.highest_close == 75_000.0
    assert pos.bars_held == 7
    assert pos.stop_from_trail is True
    assert pos.entry_score == pytest.approx(0.88)
    assert pos.entry_votes == 3
    assert pos.entry_factors == {"swing_trend.roc_120": 0.52}
    assert pos.opened_at == datetime(2024, 1, 5, 9, 30), \
        "진입 시각이 재시작 시각으로 덮여 보유기간 청산이 리셋됐다"


def test_quantity_change_is_detected_and_reported():
    """부분체결이나 외부 매매로 수량이 바뀐 것을 알아채야 한다.

    qty 를 스냅샷에 남기지 않던 판에서는 이 감지가 절대 발동하지 않았다 —
    코드는 있는데 데이터가 없어서 조용히 죽어 있었다.
    """
    state = SessionState(position_meta=snapshot_positions({"005930": _pos(qty=100)}))
    res = reconcile_positions({"005930": _pos(qty=30)}, state)
    assert res.requantified == [("005930", 100, 30)]
    assert any("수량 100 → 30" in n for n in res.notes)


def test_unchanged_quantity_is_not_reported():
    state = SessionState(position_meta=snapshot_positions({"005930": _pos(qty=100)}))
    res = reconcile_positions({"005930": _pos(qty=100)}, state)
    assert res.requantified == []


def test_position_missing_from_the_broker_is_dropped():
    """우리가 모르는 사이 정리된 포지션. 계속 들고 있다고 믿으면 안 된다."""
    state = SessionState(position_meta=snapshot_positions({"005930": _pos()}))
    res = reconcile_positions({}, state)
    assert res.positions == {}
    assert res.vanished == ["005930"]


def test_unknown_broker_position_is_kept_and_flagged():
    """버리면 그 포지션은 영영 청산되지 않는다. 대신 경고로 드러낸다."""
    res = reconcile_positions({"000660": _pos("000660")}, SessionState())
    assert "000660" in res.positions
    assert res.unknown == ["000660"]
    assert any("손절선을 모른다" in n for n in res.notes)


# ---- 일일 카운터 ------------------------------------------------------

def _trader(tmp_path, broker=None, provider=None):
    from autotrader.broker.paper import PaperBroker
    from autotrader.data.synthetic import SyntheticProvider
    from autotrader.live import LiveTrader

    return LiveTrader(provider or SyntheticProvider(),
                      broker or PaperBroker(10_000_000.0, Costs()),
                      Config(), dry_run=True,
                      state_path=str(tmp_path / "state.json"),
                      order_log=str(tmp_path / "orders.jsonl"))


def test_daily_entry_count_survives_a_restart(tmp_path):
    """0 부터 다시 세면 일일 상한이 하루에 두 번 열린다."""
    a = _trader(tmp_path)
    now = datetime(2024, 3, 4, 10, 0)
    a.risk.new_day(now.date(), 10_000_000.0)
    a.risk.register_entry()
    a.risk.register_entry()
    a.save_state(now)

    b = _trader(tmp_path)
    b.recover(now)
    assert b.risk.state.day_new_entries == 2


def test_yesterdays_counters_are_not_carried_into_today(tmp_path):
    a = _trader(tmp_path)
    a.risk.new_day(date(2024, 3, 4), 10_000_000.0)
    a.risk.register_entry()
    a.save_state(datetime(2024, 3, 4, 15, 30))

    b = _trader(tmp_path)
    notes = b.recover(datetime(2024, 3, 5, 9, 5))
    assert b.risk.state.day_new_entries == 0
    assert any("새로 시작" in n for n in notes)


def test_eod_flat_is_not_repeated_after_a_restart(tmp_path):
    """재시작 후 또 일괄청산하면 이미 없는 포지션을 다시 판다."""
    now = datetime(2024, 3, 4, 15, 25)
    a = _trader(tmp_path)
    a.risk.new_day(now.date(), 10_000_000.0)
    a._flat_done_for = "2024-03-04"
    a.save_state(now)

    b = _trader(tmp_path)
    b.recover(now)
    assert b._flat_done_for == "2024-03-04"


def test_cooldowns_survive_a_restart(tmp_path):
    now = datetime(2024, 3, 4, 10, 0)
    a = _trader(tmp_path)
    a.risk.new_day(now.date(), 10_000_000.0)
    a.cooldown.register_exit("005930", "stop", now.date(), pnl=-1000.0)
    a.save_state(now)

    b = _trader(tmp_path)
    b.recover(now)
    assert b.cooldown.is_blocked("005930", now.date()), \
        "재시작으로 쿨다운이 풀려 방금 나를 밀어낸 종목에 다시 들어간다"


def test_stop_price_survives_a_restart_through_the_trader(tmp_path):
    from autotrader.broker.paper import PaperBroker

    now = datetime(2024, 3, 4, 10, 0)
    br = PaperBroker(10_000_000.0, Costs())
    br.portfolio.positions["005930"] = _pos(stop_price=66_000.0,
                                            take_price=80_000.0)
    a = _trader(tmp_path, broker=br)
    a.risk.new_day(now.date(), 10_000_000.0)
    a.save_state(now)

    br2 = PaperBroker(10_000_000.0, Costs())
    br2.portfolio.positions["005930"] = _pos()      # 손절선 없이 되살아남
    b = _trader(tmp_path, broker=br2)
    b.recover(now)
    assert br2.portfolio.positions["005930"].stop_price == 66_000.0


def test_open_orders_are_recovered_and_flagged(tmp_path):
    from autotrader.orders import BrokerOrder, OrderStatus
    from autotrader.models import Side

    now = datetime(2024, 3, 4, 10, 0)
    a = _trader(tmp_path)
    a.book.add(BrokerOrder(client_order_id="c1", symbol="005930",
                           side=Side.BUY, qty=10,
                           status=OrderStatus.ACCEPTED, broker_order_id="B1"))
    a.save_state(now)

    b = _trader(tmp_path)
    notes = b.recover(now)
    assert b.book.get("c1") is not None, "미결 주문이 재시작으로 사라졌다"
    assert any("미결 주문" in n for n in notes)


def test_recovery_refuses_to_proceed_when_the_broker_is_unreachable(tmp_path):
    """빈 상태로 매매를 시작하면 이미 들고 있는 종목에 또 들어간다."""
    from autotrader.broker.base import BrokerError

    class Dead:
        def positions(self):
            raise RuntimeError("timeout")

        def cash(self):
            return 0.0

        def submit(self, order, price_hint):
            raise AssertionError

    with pytest.raises(BrokerError):
        _trader(tmp_path, broker=Dead()).recover(datetime(2024, 3, 4, 10, 0))


def test_a_corrupt_state_file_does_not_stop_trading(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
    st = SessionState.load(str(p))
    assert st.day is None and st.position_meta == {}


def test_saving_is_atomic_enough_to_survive_a_crash(tmp_path, monkeypatch):
    """쓰다가 죽으면 예전 상태가 남는 편이 반쯤 쓰인 상태보다 낫다.

    같은 파일에 곧바로 쓰면, 전원이 나갔을 때 반쯤 쓰인 JSON 이 남는다.
    그러면 다음 기동에서 상태를 통째로 잃는다 — 손절선까지 함께.
    """
    import json as _json

    p = str(tmp_path / "state.json")
    SessionState(day=date(2024, 3, 4), day_new_entries=1).save(p)

    def boom(*a, **k):
        raise OSError("전원이 나갔다")

    monkeypatch.setattr(_json, "dump", boom)
    with pytest.raises(OSError):
        SessionState(day=date(2024, 3, 5), day_new_entries=9).save(p)

    monkeypatch.undo()
    assert SessionState.load(p).day_new_entries == 1, \
        "쓰다 만 저장이 예전 상태를 파괴했다"


def test_no_temp_file_is_left_behind(tmp_path):
    p = str(tmp_path / "state.json")
    SessionState(day=date(2024, 3, 5), day_new_entries=9).save(p)
    assert SessionState.load(p).day_new_entries == 9
    assert not (tmp_path / "state.json.tmp").exists()
