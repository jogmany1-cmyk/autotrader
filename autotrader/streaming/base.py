"""실시간 스트림 인터페이스.

REST 폴링과 WebSocket 스트림은 개념이 완전히 다르다. REST 는 매 주기
"조회 → 응답" 이지만, 스트림은 서버가 연결을 유지하며 이벤트가 발생할 때마다
푸시한다 (조건검색·체결통보 등). 이 모듈은 그런 스트림 계층의 얇은 표준을
제공한다 — 벤더(키움/KIS/그 밖)마다 서로 다른 WebSocket 프로토콜을 감싼다.

의도적으로 asyncio 를 직접 강제하지 않는다. 구현체가 async 로 돌든 스레드로
돌든, 이 인터페이스의 소비자는 큐(queue) 로 이벤트를 받는다.
"""
from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional


class StreamError(RuntimeError):
    pass


@dataclass
class StreamEvent:
    """조건검색·체결·시세 등 실시간 이벤트 하나."""
    ts: datetime
    kind: str                       # "signal" | "fill" | "quote" | "heartbeat" | ...
    symbol: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


class StreamClient(ABC):
    """벤더 무관 실시간 스트림 클라이언트.

    사용 패턴:
        stream = KiwoomConditionStream(...)
        stream.start()
        try:
            for ev in stream.events(timeout=1.0):
                if ev.kind == "signal":
                    ...
        finally:
            stream.stop()
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[StreamEvent] = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_error: Optional[Callable[[Exception], None]] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._runner, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._on_stop()

    def _runner(self) -> None:
        try:
            self._run_forever(self._stop, self._emit)
        except Exception as exc:
            if self._on_error:
                self._on_error(exc)
            self._emit(StreamEvent(datetime.utcnow(), "error", None, {"error": str(exc)}))

    # ----------------------------------------------------------- extension
    @abstractmethod
    def _run_forever(self, stop_event: threading.Event,
                     emit: Callable[[StreamEvent], None]) -> None:
        """구현체는 정지 이벤트가 세팅될 때까지 이벤트를 emit 해야 한다."""

    def _on_stop(self) -> None:
        """훅: 하위 클래스가 리소스 정리에 사용 가능."""

    # ------------------------------------------------------------- events
    def _emit(self, event: StreamEvent) -> None:
        self._queue.put(event)

    def events(self, timeout: Optional[float] = None):
        """무한 제너레이터. stop() 이 호출되면 큐를 비우고 종료."""
        while not self._stop.is_set() or not self._queue.empty():
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue

    def drain(self, max_items: Optional[int] = None):
        """비블로킹으로 현재 쌓인 것만 꺼내 준다 (LiveTrader.cycle 결합용)."""
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
            if max_items and len(out) >= max_items:
                break
        return out
