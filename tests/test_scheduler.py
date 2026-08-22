"""Cron 파서 + JobRegistry 테스트."""
from datetime import datetime, timedelta

import pytest

from autotrader.scheduler import CronExpr, Job, JobRegistry


def test_star_expression_matches_every_minute():
    e = CronExpr.parse("* * * * *")
    assert e.matches(datetime(2026, 8, 24, 10, 0))
    assert e.matches(datetime(2026, 8, 24, 23, 59))


def test_hourly_at_top_of_hour():
    e = CronExpr.parse("0 * * * *")
    n = e.next_after(datetime(2026, 8, 24, 10, 5))
    assert n == datetime(2026, 8, 24, 11, 0)


def test_step_expression_every_5_min():
    e = CronExpr.parse("*/5 * * * *")
    n = e.next_after(datetime(2026, 8, 24, 10, 2))
    assert n == datetime(2026, 8, 24, 10, 5)


def test_range_and_weekday_filter():
    # 평일(월-금) 09~15시 5분 간격 → 토요일 이후에는 다음 월요일 09:00
    e = CronExpr.parse("*/5 9-15 * * 0-4")
    n = e.next_after(datetime(2026, 8, 22, 12, 0))  # Saturday
    assert n.weekday() < 5 and n.hour == 9 and n.minute == 0


def test_specific_time():
    e = CronExpr.parse("30 15 * * 0-4")  # 평일 15:30
    n = e.next_after(datetime(2026, 8, 24, 15, 0))  # Monday 15:00
    assert n == datetime(2026, 8, 24, 15, 30)


def test_invalid_expression_raises():
    with pytest.raises(ValueError):
        CronExpr.parse("only three fields")


def test_registry_orders_upcoming_jobs():
    reg = JobRegistry()
    reg.register("a", "0 10 * * 0-4", lambda t: None)
    reg.register("b", "30 9 * * 0-4", lambda t: None)  # b 가 먼저
    upcoming = reg.next_schedule(datetime(2026, 8, 24, 9, 0), limit=2)
    assert upcoming[0][1].name == "b"
    assert upcoming[1][1].name == "a"


def test_registry_crontab_export_matches_registration():
    reg = JobRegistry()
    reg.register("j1", "0 15 * * 0-4", lambda t: None, description="x")
    lines = reg.crontab_lines(prefix_command="run ")
    assert "0 15 * * 0-4 run j1" in lines[0]
    assert "# x" in lines[0]


def test_registry_unregister_removes_job():
    reg = JobRegistry()
    reg.register("temp", "* * * * *", lambda t: None)
    assert len(reg.jobs()) == 1
    reg.unregister("temp")
    assert reg.jobs() == []
