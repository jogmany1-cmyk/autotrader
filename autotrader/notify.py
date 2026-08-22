"""알림 채널 얇은 추상.

배경: 실전 자동매매는 어딘가로 알림이 나가야 유용하다 (텔레그램·슬랙·이메일·
디스코드 등). 하지만 벤더 종속 코드를 코어에 넣으면 다른 사용자는 못 쓴다.
그래서 인터페이스 하나 + 콘솔 기본 구현체 하나만 남기고, 사용자가 자기 스택에
맞춰 확장하도록 한다.
"""
from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


log = logging.getLogger("autotrader.notify")


@dataclass
class Notification:
    ts: datetime
    level: str                       # "info" | "warn" | "error" | "trade"
    title: str
    body: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_line(self) -> str:
        return f"[{self.ts.isoformat(timespec='seconds')}] {self.level.upper():<5} {self.title}"


class NotificationChannel(ABC):
    """모든 알림 채널이 만족해야 하는 최소 표면."""

    @abstractmethod
    def send(self, notification: Notification) -> None: ...


class ConsoleChannel(NotificationChannel):
    """stderr 로 한 줄 알림 출력. 기본 채널, 항상 안전."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def send(self, notification: Notification) -> None:
        line = notification.as_line()
        if notification.body:
            line += " — " + notification.body
        print(line, file=self.stream)


class NoopChannel(NotificationChannel):
    """알림을 삼킨다. 테스트·조용한 모드에서 사용."""

    def send(self, notification: Notification) -> None:
        return None


class RecordingChannel(NotificationChannel):
    """받은 알림을 리스트로 보관. 테스트 전용."""

    def __init__(self) -> None:
        self.received: List[Notification] = []

    def send(self, notification: Notification) -> None:
        self.received.append(notification)


class Notifier:
    """다중 채널 팬아웃."""

    def __init__(self, channels: Optional[List[NotificationChannel]] = None):
        self.channels: List[NotificationChannel] = list(channels or [])

    def add(self, channel: NotificationChannel) -> None:
        self.channels.append(channel)

    def send(self, notification: Notification) -> None:
        for ch in self.channels:
            try:
                ch.send(notification)
            except Exception as exc:  # pragma: no cover — 알림 실패는 매매에 영향 없어야 함
                log.warning("notification channel failed: %s", exc)

    # 편의 헬퍼 -----------------------------------------------------------
    def info(self, title: str, body: str = "", **meta) -> None:
        self.send(Notification(datetime.utcnow(), "info", title, body, meta))

    def warn(self, title: str, body: str = "", **meta) -> None:
        self.send(Notification(datetime.utcnow(), "warn", title, body, meta))

    def error(self, title: str, body: str = "", **meta) -> None:
        self.send(Notification(datetime.utcnow(), "error", title, body, meta))

    def trade(self, title: str, body: str = "", **meta) -> None:
        self.send(Notification(datetime.utcnow(), "trade", title, body, meta))
