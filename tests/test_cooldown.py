from datetime import date
from autotrader.cooldown import CooldownRegistry


def test_target_exit_does_not_trigger_cooldown():
    r = CooldownRegistry(default_bars=3)
    r.register_exit("A", "target", date(2026, 8, 20))
    assert not r.is_blocked("A", date(2026, 8, 21))


def test_stop_exit_blocks_for_default_bars():
    r = CooldownRegistry(default_bars=3)
    r.register_exit("A", "stop", date(2026, 8, 20))
    assert r.is_blocked("A", date(2026, 8, 21))
    assert r.is_blocked("A", date(2026, 8, 23))
    assert not r.is_blocked("A", date(2026, 8, 24))


def test_trail_exit_is_considered_profit_taking():
    r = CooldownRegistry(default_bars=3)
    r.register_exit("A", "trail", date(2026, 8, 20))
    assert not r.is_blocked("A", date(2026, 8, 21))


def test_purge_removes_expired_entries():
    r = CooldownRegistry(default_bars=1)
    r.register_exit("A", "stop", date(2026, 8, 20))
    r.purge_expired(date(2026, 8, 25))
    assert "A" not in r.entries
