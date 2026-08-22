"""표준 5-필드 cron 표현식 파서 + 다중 잡 등록기.

배경: 실전 자동매매에는 서로 다른 주기의 잡이 여러 개 공존한다 — 5분마다
분봉 수집, 장 마감 후 일봉 수집, 09:30 진입 사이클, 15:00 일괄 청산,
장 종료 후 사후 분석 등. 이걸 외부 cron 파일에만 두면 코드와 스케줄이
따로 놀아 배포가 깨진다.

이 모듈은:
- 표현식을 파싱해 `next_after(ts)` 로 다음 실행 시각을 계산 (표준 라이브러리만)
- `JobRegistry` 로 잡을 코드에서 선언 (name·expression·callback)
- 필요 시 백그라운드 러너 `run_forever()` 로 실제로 실행하거나,
  외부 스케줄러(systemd/cron/GH Actions)에는 `crontab_lines()` 로 export

지원 필드: minute · hour · day-of-month · month · day-of-week.
지원 문법: `*`, `N`, `N,M`, `N-M`, `*/K`, `N-M/K`.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------- 파서
_FIELD_BOUNDS = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0=월, 6=일) — 이 라이브러리 규약
)


def _parse_field(expr: str, lo: int, hi: int) -> List[int]:
    out: List[int] = []
    for token in expr.split(","):
        token = token.strip()
        if not token:
            continue
        step = 1
        if "/" in token:
            token, step_s = token.split("/", 1)
            step = int(step_s)
        if token == "*" or token == "":
            start, end = lo, hi
        elif "-" in token:
            a, b = token.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(token)
        for v in range(start, end + 1, step):
            if lo <= v <= hi and v not in out:
                out.append(v)
    return sorted(out)


@dataclass
class CronExpr:
    minutes: List[int]
    hours: List[int]
    days: List[int]
    months: List[int]
    weekdays: List[int]
    source: str

    @classmethod
    def parse(cls, expression: str) -> "CronExpr":
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(f"cron 표현식은 5개 필드여야 합니다: {expression!r}")
        parsed = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS)]
        return cls(*parsed, source=expression)

    def matches(self, ts: datetime) -> bool:
        # weekday: python 은 월요일=0, 이 라이브러리도 동일 (KRX 는 월~금이면 잡힘)
        return (ts.minute in self.minutes and ts.hour in self.hours
                and ts.day in self.days and ts.month in self.months
                and ts.weekday() in self.weekdays)

    def next_after(self, after: datetime) -> datetime:
        """분 단위로 앞으로 최대 1년 안에서 다음 실행 시각을 찾는다."""
        ts = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = ts + timedelta(days=366)
        while ts < limit:
            if self.matches(ts):
                return ts
            ts += timedelta(minutes=1)
        raise RuntimeError(f"{self.source!r}: 1년 안에 실행 시각을 찾지 못함")


# ---------------------------------------------------------------- 잡 등록기
@dataclass
class Job:
    name: str
    expression: str
    callback: Callable[[datetime], None]
    description: str = ""
    _expr: CronExpr = field(init=False)

    def __post_init__(self) -> None:
        self._expr = CronExpr.parse(self.expression)

    def next_after(self, after: datetime) -> datetime:
        return self._expr.next_after(after)


class JobRegistry:
    """잡을 코드로 선언하고 관리."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}

    def register(self, name: str, expression: str,
                 callback: Callable[[datetime], None],
                 description: str = "") -> Job:
        job = Job(name=name, expression=expression, callback=callback,
                  description=description)
        self._jobs[name] = job
        return job

    def unregister(self, name: str) -> None:
        self._jobs.pop(name, None)

    def jobs(self) -> List[Job]:
        return list(self._jobs.values())

    def next_schedule(self, after: datetime,
                      limit: int = 10) -> List[tuple[datetime, Job]]:
        """모든 잡의 다음 실행 시각을 앞에서부터 정렬해 top-N 반환."""
        upcoming = [(job.next_after(after), job) for job in self._jobs.values()]
        upcoming.sort(key=lambda p: p[0])
        return upcoming[:limit]

    def crontab_lines(self, prefix_command: str = "") -> List[str]:
        """외부 cron 데몬에 심을 수 있는 crontab 라인들."""
        lines: List[str] = []
        for job in self._jobs.values():
            cmd = f"{prefix_command}{job.name}".strip()
            desc = f"  # {job.description}" if job.description else ""
            lines.append(f"{job.expression} {cmd}{desc}")
        return lines

    def run_forever(self, tick_seconds: float = 1.0,
                    stop_event: Optional[threading.Event] = None) -> None:
        """블로킹 러너 — 매 초 확인해 예정 시각에 도달한 잡의 callback 호출.

        운영에서는 외부 스케줄러(cron/systemd/GH Actions) 를 쓰는 게 정석이며,
        이 러너는 개발·테스트나 단독 실행 케이스에서 편의로 제공.
        """
        stop_event = stop_event or threading.Event()
        last_fired: Dict[str, datetime] = {}
        while not stop_event.is_set():
            now = datetime.utcnow().replace(second=0, microsecond=0)
            for job in self._jobs.values():
                if last_fired.get(job.name) == now:
                    continue
                if job._expr.matches(now):
                    try:
                        job.callback(now)
                    finally:
                        last_fired[job.name] = now
            stop_event.wait(tick_seconds)
