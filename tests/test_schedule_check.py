"""크론잡이 실제로 등록됐고 실제로 돌고 있는지 확인하는 게이트.

등록했다고 **보고하는 것**과 등록된 것은 다르다. 그리고 등록되어 있어도
자격증명이 없거나 경로가 틀리면 매일 조용히 실패한다 — 크론은 아무 말도
하지 않고, 로그를 봐도 "아무 일도 없었다" 로만 보인다.

60거래일을 쌓는 동안 이 둘을 주기적으로 확인하지 않으면, 60일 뒤에야
"사실 하루도 안 돌았다" 를 알게 된다.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from autotrader.cli import STALE_AFTER_DAYS, build_schedule, evaluate_schedule

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _expected(profile="paper"):
    return {j.name: j.to_crontab_expression() for j in build_schedule(profile).jobs()}


def _status(runs_dir, job, *, code=0, ago_days=0):
    d = os.path.join(runs_dir, "status")
    os.makedirs(d, exist_ok=True)
    finished = (NOW - timedelta(days=ago_days)).isoformat()
    with open(os.path.join(d, f"{job}.json"), "w", encoding="utf-8") as fh:
        json.dump({"job": job, "exit_code": code, "finished_at": finished,
                   "log": f"/logs/{job}.log"}, fh)


def _all_healthy_lines(exp):
    return [f"{expr} /repo/scripts/cron-run.sh {name}" for name, expr in exp.items()]


def test_all_registered_and_running_passes(tmp_path):
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name)
    out, problems, warnings = evaluate_schedule(
        exp, _all_healthy_lines(exp), str(tmp_path), STALE_AFTER_DAYS, now=NOW)
    assert problems == 0 and warnings == 0, "\n".join(out)


def test_missing_job_is_a_problem(tmp_path):
    exp = _expected()
    lines = [ln for ln in _all_healthy_lines(exp) if "paper-session" not in ln]
    out, problems, _ = evaluate_schedule(exp, lines, str(tmp_path),
                                         STALE_AFTER_DAYS, now=NOW)
    assert problems >= 1
    assert any("paper-session" in ln and "crontab 에 없습니다" in ln for ln in out)


def test_wrong_time_is_a_problem(tmp_path):
    """사람이 crontab 을 손으로 고쳐서 시각이 어긋난 경우."""
    exp = _expected()
    lines = _all_healthy_lines(exp)
    lines[0] = "0 3 * * 1,2,3,4,5 /repo/scripts/cron-run.sh paper-session"
    out, problems, _ = evaluate_schedule(exp, lines, str(tmp_path),
                                         STALE_AFTER_DAYS, now=NOW)
    assert problems >= 1
    assert any("시각이 다릅니다" in ln for ln in out)


def test_failed_last_run_is_a_problem(tmp_path):
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name)
    _status(str(tmp_path), "validate-data", code=1)
    out, problems, _ = evaluate_schedule(exp, _all_healthy_lines(exp),
                                         str(tmp_path), STALE_AFTER_DAYS, now=NOW)
    assert problems == 1
    assert any("마지막 실행이 실패" in ln for ln in out)


def test_stale_job_is_a_problem(tmp_path):
    """등록도 됐고 마지막 실행도 성공했는데, 그게 2주 전인 경우.
    크론이 멈춰 있어도 상태 파일은 성공으로 남아 있다."""
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name)
    _status(str(tmp_path), "collect-daily", code=0, ago_days=14)
    out, problems, _ = evaluate_schedule(exp, _all_healthy_lines(exp),
                                         str(tmp_path), STALE_AFTER_DAYS, now=NOW)
    assert problems == 1
    assert any("14일째 안 돌았습니다" in ln for ln in out)


def test_weekend_gap_is_not_stale(tmp_path):
    """금요일에 돌고 월요일에 점검하면 3일 차이다. 이걸 FAIL 로 만들면
    매주 월요일마다 거짓 경보가 울리고, 곧 아무도 안 본다."""
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name, ago_days=3)
    _, problems, _ = evaluate_schedule(exp, _all_healthy_lines(exp),
                                       str(tmp_path), STALE_AFTER_DAYS, now=NOW)
    assert problems == 0


def test_never_run_is_a_warning_not_a_failure(tmp_path):
    """방금 설치한 직후. 여기서 FAIL 을 내면 설치하자마자 빨간불이 뜨고,
    사람은 이 게이트를 무시하는 법부터 배운다."""
    exp = _expected()
    out, problems, warnings = evaluate_schedule(
        exp, _all_healthy_lines(exp), str(tmp_path), STALE_AFTER_DAYS, now=NOW)
    assert problems == 0
    assert warnings == len(exp)
    assert all("아직 한 번도 안 돌았습니다" in ln for ln in out)


def test_leftover_job_from_another_profile_is_flagged(tmp_path):
    """daytrade 로 깔았다가 paper 로 바꿨는데 옛 라인이 남은 경우.
    옛 잡이 계속 돌면서 같은 계좌 파일을 건드린다."""
    exp = _expected("paper")
    lines = _all_healthy_lines(exp)
    lines.append("0 15 * * 1,2,3,4,5 /repo/scripts/cron-run.sh eod-flat")
    out, _, warnings = evaluate_schedule(exp, lines, str(tmp_path),
                                         STALE_AFTER_DAYS, now=NOW)
    assert warnings >= 1
    assert any("프로파일에 없는" in ln and "eod-flat" in ln for ln in out)


def test_unrelated_crontab_lines_are_ignored(tmp_path):
    """사용자의 다른 크론잡까지 참견하면 안 된다."""
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name)
    lines = _all_healthy_lines(exp) + ["0 4 * * * /usr/bin/backup.sh"]
    _, problems, warnings = evaluate_schedule(exp, lines, str(tmp_path),
                                              STALE_AFTER_DAYS, now=NOW)
    assert problems == 0 and warnings == 0


def test_wrapper_prefix_does_not_break_matching(tmp_path):
    """사용자는 래퍼로 감싸 쓴다. 명령 문자열이 기대와 달라도 잡 이름으로
    찾아야 한다 — 안 그러면 정상 설치가 매번 FAIL 로 뜬다."""
    exp = _expected()
    for name in exp:
        _status(str(tmp_path), name)
    lines = [f"{expr} cd /srv/at && AUTOTRADER_RUNS=/srv/at/runs "
             f"./scripts/cron-run.sh {name} >> /var/log/at.log 2>&1"
             for name, expr in exp.items()]
    _, problems, _ = evaluate_schedule(exp, lines, str(tmp_path),
                                       STALE_AFTER_DAYS, now=NOW)
    assert problems == 0
