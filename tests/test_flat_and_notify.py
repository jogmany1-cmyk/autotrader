"""EOD flat + 알림 채널 통합 테스트."""
from datetime import datetime

from autotrader.broker import PaperBroker
from autotrader.config import Config, Costs
from autotrader.data import SyntheticProvider
from autotrader.live import LiveTrader
from autotrader.models import Order, Side
from autotrader.notify import (ConsoleChannel, NoopChannel, Notifier,
                               Notification, RecordingChannel)


def test_flat_all_closes_all_positions():
    b = PaperBroker(1_000_000, Costs())
    b.submit(Order("A", Side.BUY, 10), price_hint=1000,
             ts=datetime(2026, 8, 24, 10), stop=900, target=1200)
    b.submit(Order("B", Side.BUY, 5), price_hint=2000,
             ts=datetime(2026, 8, 24, 11), stop=1900, target=2200)
    closed = b.flat_all(prices={"A": 1050, "B": 2050},
                        ts=datetime(2026, 8, 24, 15))
    assert len(closed) == 2
    assert {c.symbol for c in closed} == {"A", "B"}
    assert all(c.exit_reason == "eod_flat" for c in closed)
    assert b.positions() == {}


def test_live_trader_triggers_eod_flat_at_configured_time():
    p = SyntheticProvider()
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    cfg.execution.flat_at_time = "15:00"

    pb = PaperBroker(1e7, Costs())
    last = p.last_price("AAA")
    pb.submit(Order("AAA", Side.BUY, 10), price_hint=last,
              ts=datetime(2026, 8, 24, 10),
              stop=last * 0.5, target=last * 2)

    lt = LiveTrader(p, pb, cfg, ensemble_threshold=0.99, dry_run=True)
    r1 = lt.cycle(now=datetime(2026, 8, 24, 14, 0))
    assert r1.flat_closed == 0 and "AAA" in pb.positions()

    r2 = lt.cycle(now=datetime(2026, 8, 24, 15, 5))
    assert r2.flat_closed == 1 and "AAA" not in pb.positions()

    # 같은 날 재실행은 no-op
    r3 = lt.cycle(now=datetime(2026, 8, 24, 15, 30))
    assert r3.flat_closed == 0


def test_notifier_fans_out_to_all_channels():
    rec1, rec2 = RecordingChannel(), RecordingChannel()
    n = Notifier([rec1, rec2])
    n.info("hi", "world")
    n.trade("BUY X x10")
    n.warn("cooldown")
    n.error("api down")
    for rec in (rec1, rec2):
        levels = [x.level for x in rec.received]
        assert levels == ["info", "trade", "warn", "error"]


def test_notifier_survives_failing_channel():
    class Bad:
        def send(self, notification): raise RuntimeError("boom")
    rec = RecordingChannel()
    n = Notifier([Bad(), rec])
    n.info("still delivered")  # 예외 삼키고 다음 채널로 전달
    assert len(rec.received) == 1


def test_noop_channel_swallows_notifications():
    ch = NoopChannel()
    ch.send(Notification(datetime.utcnow(), "info", "x"))  # 아무 예외도 안 남
