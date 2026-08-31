"""이 fold 밖의 새 데이터가 얼마나 쌓였는지 세는 잡.

`fetch` 는 과거 이력까지 받아 온다. 그냥 모아 두고 나중에 전 구간으로
백테스트하면 이미 다섯 번 참조한 2016~2026 fold 를 여섯 번째로 보게 된다.
Bailey et al.(AMS 2014): 과최적화된 전략의 표본 외 기대수익은 0 이 아니라
음수다.

그래서 탐색을 종료한 시점을 파일에 못 박고 그보다 뒤의 거래일만 센다.
**이 파일에서 가장 중요한 테스트는 "경계가 움직이지 않는다" 이다** — 나중에
유리한 쪽으로 옮기고 싶어지는 것이 정확히 이 게이트가 막는 것이다.
"""
import json
import os
from datetime import date, datetime, timedelta

import pytest

from autotrader.cli import SCHEDULE_PROFILES, build_schedule
from autotrader.jobs import JOBS, TARGET_SESSIONS, JobContext, JobFailed, run

RUN_AT = datetime(2026, 8, 27, 16, 15)


def _cache(tmp_path, *, first_day: date, n_days: int, symbols=("005930", "000660")):
    """일봉 CSV 캐시를 만든다. 주말은 건너뛴다."""
    d = tmp_path / "cache"
    d.mkdir(exist_ok=True)
    days, cur = [], first_day
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    for sym in symbols:
        lines = ["date,open,high,low,close,volume"]
        for i, day in enumerate(days):
            px = 70_000 + i * 10
            lines.append(f"{day.isoformat()},{px},{px+50},{px-50},{px},1000000")
        (d / f"{sym}.csv").write_text("\n".join(lines), encoding="utf-8")
    return str(d)


def _ctx(tmp_path, cache):
    return JobContext(cache_dir=cache, use_kiwoom=False,
                      runs_dir=str(tmp_path / "runs"))


def _marker(tmp_path):
    return tmp_path / "runs" / "fresh-data-since.json"


# ------------------------------------------------------- 경계 기록
def test_first_run_records_the_boundary(tmp_path):
    cache = _cache(tmp_path, first_day=date(2026, 8, 3), n_days=15)
    run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    row = json.loads(_marker(tmp_path).read_text(encoding="utf-8"))
    assert row["since"] == "2026-08-27"


def test_boundary_never_moves_on_later_runs(tmp_path):
    """이 테스트가 이 파일의 핵심이다.

    경계가 매 실행마다 오늘로 갱신되면 새 데이터는 영원히 0일이고, 반대로
    사람이 손으로 앞당기면 폐기한 fold 가 슬그머니 다시 들어온다.
    """
    cache = _cache(tmp_path, first_day=date(2026, 8, 3), n_days=15)
    ctx = _ctx(tmp_path, cache)
    run("data-progress", ctx, now=RUN_AT)
    first = json.loads(_marker(tmp_path).read_text(encoding="utf-8"))

    # 한 달 뒤에 다시 돌아도 경계는 그대로여야 한다
    run("data-progress", ctx, now=RUN_AT + timedelta(days=30))
    later = json.loads(_marker(tmp_path).read_text(encoding="utf-8"))
    assert later["since"] == first["since"] == "2026-08-27"


# ------------------------------------------------------- 세는 방식
def test_bars_before_the_boundary_are_not_counted(tmp_path):
    """경계 이전 데이터는 이미 다섯 번 본 구간이다. 세면 안 된다."""
    cache = _cache(tmp_path, first_day=date(2026, 6, 1), n_days=60)
    msg = run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    assert f"새 데이터 0/{TARGET_SESSIONS}거래일" in msg, msg


def test_bars_after_the_boundary_are_counted(tmp_path):
    cache = _cache(tmp_path, first_day=date(2026, 8, 28), n_days=5)
    msg = run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    assert f"새 데이터 5/{TARGET_SESSIONS}거래일" in msg, msg


def test_boundary_day_itself_is_not_counted(tmp_path):
    """경계 당일 봉은 우리가 멈추기로 결정하기 전에 형성된 것일 수 있다.
    엄격히 그 뒤만 센다."""
    cache = _cache(tmp_path, first_day=date(2026, 8, 27), n_days=1)
    msg = run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    assert "새 데이터 0/" in msg, msg


def test_same_day_across_symbols_counts_once(tmp_path):
    """거래일을 세는 것이지 봉 개수를 세는 것이 아니다."""
    cache = _cache(tmp_path, first_day=date(2026, 8, 28), n_days=3,
                   symbols=("005930", "000660", "035420"))
    msg = run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    assert "새 데이터 3/" in msg, msg


def test_sixty_days_is_announced(tmp_path):
    cache = _cache(tmp_path, first_day=date(2026, 8, 28), n_days=TARGET_SESSIONS)
    msg = run("data-progress", _ctx(tmp_path, cache), now=RUN_AT)
    assert "60거래일 확보" in msg, msg


def test_empty_cache_fails_loudly(tmp_path):
    empty = tmp_path / "cache"
    empty.mkdir()
    with pytest.raises(JobFailed, match="종목이 없습니다"):
        run("data-progress", _ctx(tmp_path, str(empty)), now=RUN_AT)


# ------------------------------------------------------- collect 프로파일
def test_collect_profile_needs_no_strategy():
    """1단계는 승인된 전략 없이 돌아야 한다. paper-session 이 끼면
    레지스트리가 없어 매일 실패한다."""
    names = [n for n, _, _ in SCHEDULE_PROFILES["collect"]]
    assert "paper-session" not in names
    assert names == ["collect-daily", "validate-data", "data-progress"]


def test_collect_is_the_default_profile():
    """지금 승인된 전략이 하나도 없다. paper 프로파일은 매일 실패로 끝난다.
    인자 없이 부른 `autotrader schedule` 은 실제로 돌릴 수 있는 것을 줘야 한다."""
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, "-m", "autotrader", "schedule"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "profile=collect" in proc.stdout
    assert "paper-session" not in proc.stdout


def test_every_collect_job_exists():
    for name, _, _ in SCHEDULE_PROFILES["collect"]:
        assert name in JOBS


def test_collect_profile_exports_with_cron_weekday():
    for line in build_schedule("collect").crontab_lines(prefix_command="x "):
        assert line.split()[4] == "1,2,3,4,5"
