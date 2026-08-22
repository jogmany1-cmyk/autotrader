"""LocalStream · KiwoomConditionStream · LiveTrader 스트림 통합 테스트."""
import time
from datetime import datetime

import pytest

from autotrader.streaming import (KiwoomConditionStream, LocalStream,
                                  StreamEvent, StreamError)
from autotrader.streaming.base import StreamClient


def test_local_stream_delivers_prewired_events():
    events = [
        StreamEvent(datetime.utcnow(), "signal", "AAA"),
        StreamEvent(datetime.utcnow(), "signal", "BBB"),
        StreamEvent(datetime.utcnow(), "heartbeat"),
    ]
    s = LocalStream(events, gap_seconds=0.001)
    s.start()
    time.sleep(0.05)
    drained = s.drain()
    s.stop()
    kinds = [ev.kind for ev in drained]
    assert kinds.count("signal") == 2
    assert kinds.count("heartbeat") == 1


def test_local_stream_accepts_push_after_start():
    s = LocalStream([], gap_seconds=0.005)
    s.start()
    s.push(StreamEvent(datetime.utcnow(), "signal", "PUSHED"))
    time.sleep(0.05)
    drained = s.drain()
    s.stop()
    assert any(ev.symbol == "PUSHED" for ev in drained)


def test_kiwoom_stream_requires_credentials():
    with pytest.raises(StreamError):
        KiwoomConditionStream(access_token="", condition_seq="0")
    with pytest.raises(StreamError):
        KiwoomConditionStream(access_token="x", condition_seq="")


def test_kiwoom_stream_selects_correct_url_by_mode():
    real = KiwoomConditionStream(access_token="x", condition_seq="0", is_paper=False)
    paper = KiwoomConditionStream(access_token="x", condition_seq="0", is_paper=True)
    assert "mockapi" in paper.url
    assert real.url.startswith("wss://") and "mockapi" not in real.url


def test_live_trader_promotes_stream_signals_to_orders():
    from autotrader.broker import PaperBroker
    from autotrader.config import Config, Costs
    from autotrader.data import SyntheticProvider
    from autotrader.live import LiveTrader

    p = SyntheticProvider()
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    trader = LiveTrader(p, PaperBroker(1e7, Costs()), cfg,
                        ensemble_threshold=0.1, ensemble_min_votes=1,
                        dry_run=True)
    trader.stream = LocalStream([
        StreamEvent(datetime.utcnow(), "signal", "FFF"),
    ], gap_seconds=0.001)
    trader.stream.start()
    time.sleep(0.03)
    rep = trader.cycle(now=datetime(2026, 8, 24, 10, 0))
    trader.stream.stop()
    assert rep.stream_events == 1
    # FFF 는 합성 데이터에서 강한 모멘텀을 갖도록 설계됨 → 앙상블 BUY 가 확실히 뜬다
    assert any("[stream] FFF" in line for line in rep.details)


def test_stream_signal_blocked_by_cooldown():
    from datetime import date
    from autotrader.broker import PaperBroker
    from autotrader.config import Config, Costs
    from autotrader.data import SyntheticProvider
    from autotrader.live import LiveTrader

    p = SyntheticProvider()
    cfg = Config.default()
    cfg.universe.symbols = p.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    trader = LiveTrader(p, PaperBroker(1e7, Costs()), cfg,
                        ensemble_threshold=0.1, ensemble_min_votes=1, dry_run=True)
    trader.cooldown.register_exit("AAA", "stop", date(2026, 8, 24))
    trader.stream = LocalStream([
        StreamEvent(datetime.utcnow(), "signal", "AAA"),
    ], gap_seconds=0.001)
    trader.stream.start()
    time.sleep(0.03)
    rep = trader.cycle(now=datetime(2026, 8, 25, 10, 0))
    trader.stream.stop()
    # 쿨다운 상태이므로 스트림 이벤트가 들어와도 주문/디테일에 [stream] BUY 는 없어야 함
    assert not any("[stream] AAA: BUY" in line for line in rep.details)
