"""crontab 내보내기의 요일 규약 변환.

scheduler.py 내부는 Python 규약(`datetime.weekday()`, 0=월 … 6=일)을 쓴다.
표준 cron 데몬(Vixie/ISC, crontab(5))은 **0=일** 이다. 두 규약이 한 칸씩
어긋나 있어서, 내부 표현식을 그대로 crontab 에 심으면:

    "0 15 * * 0-4"   내부 해석 → 월·화·수·목·금
                     cron 해석 → 일·월·화·수·목

즉 **금요일이 통째로 빠지고 일요일에 헛돈다.** 장이 열리는 날의 20% 를
매주 조용히 놓치는데, 크론은 아무 소리도 내지 않는다. 로그를 봐도
"금요일에 아무 일도 없었다"로만 보여서 원인을 찾기 어렵다.

이 파일은 그 변환을 고정한다.
"""
import subprocess
import sys
from datetime import datetime

from autotrader.scheduler import CronExpr, JobRegistry

WEEKDAY_KO = "월화수목금토일"


def test_internal_convention_is_python_weekday():
    """전제 확인 — 내부 0-4 는 월~금이다. 이게 바뀌면 변환도 바뀌어야 한다."""
    e = CronExpr.parse("0 15 * * 0-4")
    matched = {WEEKDAY_KO[datetime(2026, 8, d, 15, 0).weekday()]
               for d in range(24, 31) if e.matches(datetime(2026, 8, d, 15, 0))}
    assert matched == set("월화수목금")


def test_export_shifts_weekday_field_to_cron_convention():
    """내부 0-4(월~금) → cron 1-5(월~금)."""
    assert CronExpr.parse("0 15 * * 0-4").to_crontab_expression() == "0 15 * * 1,2,3,4,5"


def test_export_maps_sunday_to_zero():
    """내부 6(일) → cron 0(일). 한 칸 밀리는 지점이라 따로 고정한다."""
    assert CronExpr.parse("0 9 * * 6").to_crontab_expression() == "0 9 * * 0"
    assert CronExpr.parse("0 9 * * 5").to_crontab_expression() == "0 9 * * 6"   # 토
    assert CronExpr.parse("0 9 * * 0").to_crontab_expression() == "0 9 * * 1"   # 월


def test_export_keeps_every_day_as_star():
    """매일이면 굳이 0,1,2,... 로 풀어 쓰지 않는다 — 사람이 읽어야 하는 파일이다."""
    assert CronExpr.parse("*/5 9-15 * * *").to_crontab_expression() == "*/5 9-15 * * *"


def test_export_preserves_other_fields_verbatim():
    """분·시·일·월 필드는 손대지 않는다. `*/5` 가 `0,5,10,...` 으로 풀리면
    사람이 crontab 을 읽고 검토할 수 없게 된다."""
    got = CronExpr.parse("*/5 9-15 1,15 */2 0-4").to_crontab_expression()
    assert got.split()[:4] == ["*/5", "9-15", "1,15", "*/2"]


def test_registry_export_uses_cron_convention():
    reg = JobRegistry()
    reg.register("j1", "0 15 * * 0-4", lambda t: None, description="장 마감 후")
    line = reg.crontab_lines(prefix_command="run ")[0]
    assert line.startswith("0 15 * * 1,2,3,4,5 run j1")
    assert "# 장 마감 후" in line
    assert " 0-4 " not in line, "내부 규약이 그대로 새어 나갔다"


def test_shipped_schedule_command_never_emits_python_weekday():
    """`autotrader schedule` 출력 전체에 대한 게이트.

    잡을 새로 추가할 때 표현식을 내부 규약으로 쓰는 것은 옳다. 잘못은
    그것을 변환 없이 내보내는 것이고, 이 테스트가 그 경로를 막는다.
    """
    proc = subprocess.run([sys.executable, "-m", "autotrader", "schedule"],
                          stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for line in proc.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        weekday_field = line.split()[4]
        assert weekday_field != "0-4", (
            f"내부 규약(0-4=월~금)이 그대로 나갔다 — cron 은 일~목으로 읽는다:\n  {line}")
