"""60거래일 모의투자 잡 세트.

이 세트가 지켜야 하는 것은 "돌아간다" 가 아니라 **조용히 성공하지 않는다** 다.
크론은 종료코드만 본다. 설정이 빠졌는데 0 으로 끝나면 60일 뒤에 빈 계좌를 보고
"전략이 나빴다" 고 오진하게 된다 — 실제로는 시작조차 안 한 것이다.
"""
import json
import os
import subprocess
import sys

import pytest

from autotrader.cli import SCHEDULE_PROFILES, build_schedule
from autotrader.jobs import (JOBS, TARGET_SESSIONS, JobContext, JobFailed,
                             _count_sessions, run)


# ------------------------------------------------------------ 실패 경로
def test_paper_session_without_registry_fails_loudly(tmp_path):
    ctx = JobContext(cache_dir=str(tmp_path), registry_path=None,
                     use_kiwoom=False, runs_dir=str(tmp_path / "runs"))
    with pytest.raises(JobFailed, match="registry"):
        run("paper-session", ctx)


def test_paper_session_with_no_approved_strategy_fails_loudly(tmp_path):
    """폐기한 5종이 기본으로 도는 것을 막는 안전장치."""
    reg = tmp_path / "registry.json"
    reg.write_text("[]", encoding="utf-8")   # 레지스트리 파일은 레코드의 평범한 JSON 리스트
    ctx = JobContext(cache_dir=str(tmp_path), registry_path=str(reg),
                     use_kiwoom=False, runs_dir=str(tmp_path / "runs"))
    with pytest.raises(JobFailed, match="승인된 전략이 없습니다"):
        run("paper-session", ctx)


def test_validate_data_fails_when_no_csv(tmp_path):
    ctx = JobContext(cache_dir=str(tmp_path / "empty"), use_kiwoom=False,
                     runs_dir=str(tmp_path / "runs"))
    with pytest.raises(JobFailed, match="검사할 CSV 가 없습니다"):
        run("validate-data", ctx)


def test_job_failure_becomes_nonzero_exit_code(tmp_path):
    """크론이 실제로 보는 것은 이것뿐이다."""
    proc = subprocess.run(
        [sys.executable, "-m", "autotrader", "run-job", "validate-data",
         "--cache", str(tmp_path / "empty"), "--runs", str(tmp_path / "runs")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert proc.returncode == 1, f"실패가 0 으로 끝났다:\n{proc.stdout}{proc.stderr}"
    assert "[FAIL]" in proc.stdout


def test_unknown_job_exits_two():
    proc = subprocess.run([sys.executable, "-m", "autotrader", "run-job", "nope"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert proc.returncode == 2


# ------------------------------------------------------------ 세션 일지
def test_session_report_without_journal_warns_not_crashes(tmp_path):
    ctx = JobContext(use_kiwoom=False, runs_dir=str(tmp_path / "runs"))
    assert "세션 기록이 없습니다" in run("session-report", ctx)


def test_holidays_do_not_count_toward_sixty_sessions(tmp_path):
    """휴장일에 크론이 돌아도 진척이 되면 안 된다. 그렇지 않으면 60일을
    채웠다고 보고하는데 실제 거래일은 40일인 상태가 된다."""
    path = tmp_path / "sessions.jsonl"
    rows = [
        {"date": "2026-08-24", "market_open": True},
        {"date": "2026-08-25", "market_open": False},   # 휴장
        {"date": "2026-08-26", "market_open": True},
        {"date": "2026-08-26", "market_open": True},    # 같은 날 두 번 (재실행)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert _count_sessions(str(path)) == 2


def test_broken_journal_line_does_not_lose_the_rest(tmp_path):
    path = tmp_path / "sessions.jsonl"
    path.write_text(
        json.dumps({"date": "2026-08-24", "market_open": True}) + "\n"
        + "{반쪽만 쓰이다 정전\n"
        + json.dumps({"date": "2026-08-25", "market_open": True}) + "\n",
        encoding="utf-8")
    assert _count_sessions(str(path)) == 2


# ------------------------------------------------------------ 스케줄 프로파일
def test_default_profile_is_the_paper_session_set():
    names = [n for n, _, _ in SCHEDULE_PROFILES["paper"]]
    assert "paper-session" in names and "validate-data" in names
    # 폐기한 전략군을 전제하는 잡이 기본에 있으면 안 된다
    assert "eod-flat" not in names and "morning-entry" not in names


def test_every_scheduled_job_actually_exists():
    """크론에는 있는데 CLI 가 거부하는 상태를 막는다. 이게 어긋나면 매일
    'unknown job' 으로 조용히 실패한다."""
    for profile, entries in SCHEDULE_PROFILES.items():
        for name, _, _ in entries:
            assert name in JOBS, f"{profile} 프로파일의 {name!r} 이 JOBS 에 없다"


def test_paper_session_runs_during_market_hours_not_after_close():
    """LiveTrader.cycle() 은 휴장 중이면 즉시 반환한다. 세션을 장 마감 뒤로
    옮기면 60일 내내 아무 일도 일어나지 않는다."""
    expr = dict((n, e) for n, e, _ in SCHEDULE_PROFILES["paper"])["paper-session"]
    minute, hour = expr.split()[0], expr.split()[1]
    assert 9 <= int(hour) < 15, f"장중이 아니다: {expr}"
    assert int(minute) >= 0


def test_profiles_export_with_cron_weekday_convention():
    for profile in SCHEDULE_PROFILES:
        for line in build_schedule(profile).crontab_lines(prefix_command="x "):
            assert line.split()[4] != "0-4", f"내부 규약이 새어 나갔다: {line}"


def test_target_sessions_matches_promotion_path():
    """CLAUDE.md 의 '최소 60거래일 모의투자'."""
    assert TARGET_SESSIONS == 60
