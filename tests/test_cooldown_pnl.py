"""손실로 끝난 청산은 사유와 무관하게 쿨다운을 건다.

`"trail"` 청산 라벨을 만들자 cooldown 에 잠들어 있던 면제 규칙이 깨어났다.
그런데 트레일링 스탑은 이익 실현으로도, 손실 확정으로도 걸린다. 사유만 보고
면제하면 떨어지는 종목에 다음 날 바로 재진입한다 — 정확히 쿨다운이 막으려던
행동이다.
"""
from datetime import date

from autotrader.cooldown import COOLDOWN_EXEMPT_REASONS, CooldownRegistry


def test_losing_exempt_exit_still_gets_a_cooldown():
    reason = next(iter(COOLDOWN_EXEMPT_REASONS))
    cd = CooldownRegistry(default_bars=5)
    cd.register_exit("005930", reason, date(2024, 1, 10), pnl=-1000.0)
    assert cd.is_blocked("005930", date(2024, 1, 11)), \
        "손실로 끝난 청산인데 다음 날 바로 재진입이 열렸다"


def test_winning_exempt_exit_is_still_exempt():
    """면제 규칙 자체를 없앤 게 아니다 — 이익으로 끝났으면 그대로 면제."""
    reason = next(iter(COOLDOWN_EXEMPT_REASONS))
    cd = CooldownRegistry(default_bars=5)
    cd.register_exit("005930", reason, date(2024, 1, 10), pnl=+1000.0)
    assert not cd.is_blocked("005930", date(2024, 1, 11))


def test_unknown_pnl_is_treated_as_exempt_for_backward_compatibility():
    reason = next(iter(COOLDOWN_EXEMPT_REASONS))
    cd = CooldownRegistry(default_bars=5)
    cd.register_exit("005930", reason, date(2024, 1, 10))
    assert not cd.is_blocked("005930", date(2024, 1, 11))


def test_non_exempt_exit_always_gets_a_cooldown():
    cd = CooldownRegistry(default_bars=5)
    cd.register_exit("005930", "stop", date(2024, 1, 10), pnl=+1000.0)
    assert cd.is_blocked("005930", date(2024, 1, 11))


def test_callers_actually_pass_pnl():
    """호출부가 pnl 을 안 넘기면 위 규칙이 백테스트/실매매에 적용되지 않는다."""
    import inspect

    from autotrader import backtest, live
    for mod in (backtest, live):
        src = inspect.getsource(mod)
        assert "cooldown.register_exit(" in src
        i = src.index("cooldown.register_exit(")
        assert "pnl=" in src[i:i + 200], f"{mod.__name__} 가 pnl 을 넘기지 않는다"


def test_eod_flat_also_registers_a_cooldown():
    """장 마감 일괄 청산(`eod_flat`)도 청산이다.

    여기만 쿨다운을 안 걸면, 손실로 강제 정리한 종목에 다음 날 아침 바로
    되들어간다 — 규율로 자른 것을 규율 없이 되사는 셈이다.
    """
    import inspect

    from autotrader import live
    src = inspect.getsource(live.LiveTrader.cycle)
    i = src.index("flat_all(")
    tail = src[i:i + 900]
    assert "cooldown.register_exit(" in tail, "EOD 일괄청산이 쿨다운을 안 건다"
    assert "pnl=" in tail
