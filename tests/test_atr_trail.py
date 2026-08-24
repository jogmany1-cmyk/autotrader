"""변동성에 맞춘 트레일링 폭.

고정 %는 종목별 변동성을 무시해서, 변동성이 큰 종목은 정상적인 흔들림에도
잘려나간다. 배수는 관측이 아니라 원리로 정했다 — `edge` 측정상 진입 신호는
10일 지평선에서 유의했고(t=2.81), 변동폭은 대략 기간의 제곱근에 비례하므로
√10 ≈ 3.16 배의 일일 ATR 이 필요하다.

주의: 이 변경은 TRAIN 에서만 성적이 좋아졌고 VAL 에서는 차이가 없었다.
구조(트레일 청산 73→28건, 목표달성 48→61건)는 가설대로 바뀌었지만,
성능 개선이 일반화된다는 증거는 아직 없다.
"""
from datetime import datetime

import pytest

from autotrader.indicators import atr_trail_pct as _atr_trail
from autotrader.broker import PaperBroker
from autotrader.config import Config, Costs, ExecutionCfg
from autotrader.models import Bar, Order, Side


def _bars(n=40, price=100.0, rng=2.0):
    """일일 변동폭이 rng 인 봉 시퀀스."""
    out = []
    for i in range(n):
        out.append(Bar(ts=datetime(2026, 1, 1), open=price, high=price + rng,
                       low=price - rng, close=price, volume=1000.0))
    return out


def test_trail_width_scales_with_volatility():
    """변동성이 두 배면 트레일 폭도 대략 두 배여야 한다."""
    cfg = ExecutionCfg()
    calm = _atr_trail(_bars(rng=1.0), 39, 100.0, cfg)
    wild = _atr_trail(_bars(rng=2.0), 39, 100.0, cfg)
    assert calm is not None and wild is not None
    assert wild == pytest.approx(calm * 2, rel=0.05)


def test_multiplier_is_sqrt_of_the_signal_horizon():
    """배수는 임의값이 아니라 10일 지평선에서 유도했다."""
    assert ExecutionCfg().trail_atr_mult == pytest.approx(10 ** 0.5)


def test_disabled_when_multiplier_is_zero():
    cfg = ExecutionCfg(); cfg.trail_atr_mult = 0.0
    assert _atr_trail(_bars(), 39, 100.0, cfg) is None


def test_returns_none_when_history_is_too_short():
    """워밍업이 안 됐으면 계좌 기본값으로 넘긴다."""
    assert _atr_trail(_bars(n=5), 4, 100.0, ExecutionCfg()) is None


def test_atr_uses_only_bars_up_to_the_decision(monkeypatch):
    """미래 정보 금지 — 판단 봉 이후를 보면 안 된다."""
    seen = {}
    import autotrader.indicators as m

    def spy(window, period):
        seen["n"] = len(window)
        return [2.0]
    monkeypatch.setattr(m, "atr", spy)
    _atr_trail(_bars(n=100), 30, 100.0, ExecutionCfg())
    assert seen["n"] == 31, "bars[:idx+1] 만 넘겨야 한다"


def test_per_position_width_overrides_account_default():
    """포지션별 폭이 있으면 그것을 쓴다."""
    br = PaperBroker(1_000_000.0, Costs())
    br.submit(Order(symbol="AAA", side=Side.BUY, qty=10), price_hint=100.0,
              ts=datetime(2026, 1, 1), stop=50.0, trail=0.20)
    # 120 까지 오르면 20% 트레일 → 스탑 96 (계좌 기본 5% 였다면 114)
    br.mark({"AAA": Bar(ts=datetime(2026, 1, 5), open=100, high=120, low=100,
                        close=120, volume=1.0)}, datetime(2026, 1, 5),
            trail_pct=0.05)
    pos = br.positions()["AAA"]
    assert pos.stop_price == pytest.approx(96.0)


def test_account_default_used_when_position_has_no_width():
    br = PaperBroker(1_000_000.0, Costs())
    br.submit(Order(symbol="AAA", side=Side.BUY, qty=10), price_hint=100.0,
              ts=datetime(2026, 1, 1), stop=50.0)
    br.mark({"AAA": Bar(ts=datetime(2026, 1, 5), open=100, high=120, low=100,
                        close=120, volume=1.0)}, datetime(2026, 1, 5),
            trail_pct=0.05)
    assert br.positions()["AAA"].stop_price == pytest.approx(114.0)
