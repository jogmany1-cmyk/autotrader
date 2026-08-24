"""청산 사유를 초기 손절과 트레일링으로 구분한다.

배경: `update_trailing` 이 `pos.stop_price` 를 덮어쓰는데 청산 사유는 둘 다
"stop" 으로 기록됐다. 그래서 백테스트에서 "손절 청산 72%, 평균 -1.87%" 를 보고
"ATR 손절이 너무 좁다" 고 오진했다. 실제 측정해 보니 전략의 초기 손절은 진입가
대비 -16.5% 로 충분히 넓었고, 5% 트레일링이 그것을 -5% 로 조이고 있었다.

부수 효과가 하나 더 있다. cooldown.COOLDOWN_EXEMPT_REASONS 는 이미 {"target",
"trail"} 로 트레일링을 면제 대상으로 두고 있었는데, 브로커가 "trail" 을 한 번도
내보내지 않아 모든 트레일링 청산이 쿨다운에 걸리고 있었다.
"""
from datetime import date, datetime

from autotrader.broker import PaperBroker
from autotrader.config import Costs
from autotrader.cooldown import (COOLDOWN_EXEMPT_REASONS, CooldownRegistry)
from autotrader.models import Bar, Order, Side


def _broker(cash=1_000_000.0):
    return PaperBroker(cash, Costs())


def _bar(o, h, l, c):
    return Bar(ts=datetime(2026, 1, 5), open=o, high=h, low=l, close=c,
               volume=1000.0)


def _buy(br, price, stop):
    br.submit(Order(symbol="AAA", side=Side.BUY, qty=10), price_hint=price,
              ts=datetime(2026, 1, 1), stop=stop)


def test_initial_stop_is_labelled_stop():
    """트레일링이 한 번도 개입하지 않았으면 'stop' 이다."""
    br = _broker()
    _buy(br, 100.0, stop=90.0)
    closed = br.mark({"AAA": _bar(95, 95, 89, 89)}, datetime(2026, 1, 5),
                     trail_pct=0.0)
    assert [t.exit_reason for t in closed] == ["stop"]


def test_trailing_stop_is_labelled_trail():
    """트레일링이 스탑을 끌어올린 뒤 걸리면 'trail' 이다."""
    br = _broker()
    _buy(br, 100.0, stop=80.0)          # 초기 손절은 -20% 로 넉넉히
    # 1일차: 120 까지 오르며 트레일링이 스탑을 108 로 끌어올림 (10%)
    br.mark({"AAA": _bar(100, 120, 100, 120)}, datetime(2026, 1, 5),
            trail_pct=0.10)
    # 2일차: 107 까지 밀려 트레일링 스탑에 걸림
    closed = br.mark({"AAA": _bar(118, 118, 107, 107)}, datetime(2026, 1, 6),
                     trail_pct=0.10)
    assert [t.exit_reason for t in closed] == ["trail"]


def test_trail_exit_is_exempt_from_cooldown():
    """트레일링 청산은 쿨다운 면제 — 원래 그렇게 설계돼 있었는데 라벨이 막고 있었다."""
    assert "trail" in COOLDOWN_EXEMPT_REASONS
    reg = CooldownRegistry(default_bars=3)
    reg.register_exit("AAA", "trail", on=date(2026, 1, 5))
    assert not reg.is_blocked("AAA", date(2026, 1, 6))


def test_plain_stop_still_triggers_cooldown():
    """초기 손절은 여전히 쿨다운을 건다 — 기존 동작을 유지한다."""
    reg = CooldownRegistry(default_bars=3)
    reg.register_exit("AAA", "stop", on=date(2026, 1, 5))
    assert reg.is_blocked("AAA", date(2026, 1, 6))


def test_hard_stop_takes_priority_over_trail():
    """계좌 보호선이 먼저다 — 라벨을 나눠도 우선순위는 그대로."""
    br = _broker()
    _buy(br, 100.0, stop=80.0)
    br.mark({"AAA": _bar(100, 110, 100, 110)}, datetime(2026, 1, 5),
            trail_pct=0.05)                      # 트레일 스탑 104.5
    closed = br.mark({"AAA": _bar(100, 100, 85, 85)}, datetime(2026, 1, 6),
                     trail_pct=0.05, hard_stop_pct=0.10)   # 하드스톱 90
    assert [t.exit_reason for t in closed] == ["hard_stop"]


def test_flag_is_not_set_when_trailing_does_not_raise_the_stop():
    """트레일링이 계산한 값이 기존 스탑보다 낮으면 덮어쓰지 않는다."""
    br = _broker()
    _buy(br, 100.0, stop=98.0)          # 초기 손절이 이미 타이트
    br.mark({"AAA": _bar(100, 101, 99, 101)}, datetime(2026, 1, 5),
            trail_pct=0.10)             # 트레일 계산값 90.9 < 98 → 무시
    closed = br.mark({"AAA": _bar(99, 99, 97, 97)}, datetime(2026, 1, 6),
                     trail_pct=0.10)
    assert [t.exit_reason for t in closed] == ["stop"]


# ---- 미래 정보 누수 (CLAUDE.md 불변조건 1) -------------------------------

def test_trailing_stop_does_not_use_todays_close_against_todays_low():
    """오늘 종가로 세운 스탑을 오늘 저가에 적용하면 미래 정보다.

    하루 내내 오르기만 한 봉(시가 100 → 고가 120 → 종가 120)에서, 종가로 계산한
    트레일링 스탑 108 이 같은 봉의 저가 100 에 걸려 손실 청산이 기록됐다.
    종가가 나오기 전에는 그 스탑을 세울 수 없다.
    """
    br = _broker()
    _buy(br, 100.0, stop=80.0)
    rising = _bar(100, 120, 100, 120)          # 저가가 시가와 같다 = 계속 오름
    closed = br.mark({"AAA": rising}, datetime(2026, 1, 5), trail_pct=0.10)
    assert closed == [], "오르기만 한 봉에서 청산이 나오면 안 된다"


def test_new_trailing_stop_takes_effect_from_the_next_bar():
    br = _broker()
    _buy(br, 100.0, stop=80.0)
    br.mark({"AAA": _bar(100, 120, 100, 120)}, datetime(2026, 1, 5),
            trail_pct=0.10)                    # 여기서 스탑 108 로 설정
    closed = br.mark({"AAA": _bar(118, 118, 107, 107)}, datetime(2026, 1, 6),
                     trail_pct=0.10)
    assert [t.exit_reason for t in closed] == ["trail"]
    # 108 에 걸렸으므로 진입가 100 대비 이익이어야 한다 — 손실이면 계산이 틀린 것.
    assert closed[0].return_pct > 0.05
