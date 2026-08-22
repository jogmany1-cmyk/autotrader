"""LocalStream — 테스트·데모용 로컬 이벤트 스트림.

외부 벤더 접속 없이 스트림 계층 전체를 검증한다. 테스트 코드는 미리 만들어둔
이벤트 리스트를 넘겨 시나리오를 재현할 수 있고, 라이브 데모는 `push()` 로
사용자가 임의 시점에 이벤트를 던져 넣을 수 있다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Iterable, List, Optional

from .base import StreamClient, StreamEvent


class LocalStream(StreamClient):
    def __init__(self, prewired: Optional[Iterable[StreamEvent]] = None,
                 gap_seconds: float = 0.01):
        super().__init__()
        self._prewired: List[StreamEvent] = list(prewired or [])
        self._external: List[StreamEvent] = []
        self._external_lock = threading.Lock()
        self.gap_seconds = gap_seconds

    def push(self, event: StreamEvent) -> None:
        """실행 중인 스트림에 이벤트 하나 추가."""
        with self._external_lock:
            self._external.append(event)

    def _run_forever(self, stop_event: threading.Event,
                     emit: Callable[[StreamEvent], None]) -> None:
        # 사전 등록된 이벤트를 순서대로 흘려보낸다.
        for ev in self._prewired:
            if stop_event.is_set():
                return
            emit(ev)
            if self.gap_seconds > 0:
                stop_event.wait(self.gap_seconds)

        # 그 뒤에는 외부 push 대기.
        while not stop_event.is_set():
            with self._external_lock:
                pending, self._external = self._external, []
            for ev in pending:
                emit(ev)
            stop_event.wait(self.gap_seconds or 0.05)
