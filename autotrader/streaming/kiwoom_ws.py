"""KiwoomConditionStream — 키움 조건검색 실시간 WebSocket 스켈레톤.

배경: 조건검색은 REST 폴링으로는 한계가 크다. 서버가 조건에 맞는 종목을
발견할 때마다 즉시 이벤트를 밀어 보내는 WebSocket 스트림을 써야 한다.
프로토콜 요지:
- 소켓 URL 로 connect (모의/실 서버 분리)
- CONNECT 프레임에 access_token 을 실어 2차 로그인
- 서버가 주기적으로 `TRNM=PING` 을 보냄 → 같은 값 그대로 응답 (연결 유지)
- 조건식 목록 조회 요청 → seq 획득
- 조건검색 실시간 등록(REG) → 종목이 잡힐 때마다 `TRNM=REAL` 이벤트 수신
  · 코드 9001 필드에 종목 코드가 들어옴

이 클래스는 자격증명이 없으면 안전하게 실패한다. 실제 접속·프로토콜 완성은
운영 배포 시 벤더 문서(코드 예제 포함)를 따라 채워 넣도록 훅을 남긴다.
`websockets` 패키지는 옵션 의존성이며, 없어도 임포트는 성공한다.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from .base import StreamClient, StreamEvent, StreamError

# 모의 / 실 서버 소켓 URL — 실전 배포 시 벤더 최신 값으로 갱신 필요.
KIWOOM_WS_PAPER = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
KIWOOM_WS_REAL = "wss://api.kiwoom.com:10000/api/dostk/websocket"


class KiwoomConditionStream(StreamClient):
    def __init__(self, access_token: str, condition_seq: str,
                 is_paper: bool = True, on_error: Optional[Callable] = None):
        super().__init__()
        if not access_token:
            raise StreamError("access_token 이 필요합니다")
        if not condition_seq:
            raise StreamError("condition_seq (조건식 일련번호) 가 필요합니다")
        self.access_token = access_token
        self.condition_seq = condition_seq
        self.url = KIWOOM_WS_PAPER if is_paper else KIWOOM_WS_REAL
        self._on_error = on_error
        self._ws = None  # 실 연결 핸들

    def _run_forever(self, stop_event: threading.Event,
                     emit: Callable[[StreamEvent], None]) -> None:
        try:
            import asyncio
            import json
            import websockets  # type: ignore
        except ImportError as exc:
            raise StreamError(
                "websockets 패키지가 필요합니다: pip install websockets"
            ) from exc

        async def _pump() -> None:
            async with websockets.connect(self.url, ping_interval=None) as ws:  # type: ignore
                self._ws = ws
                # 1) 2차 로그인
                await ws.send(json.dumps({
                    "trnm": "LOGIN", "token": self.access_token,
                }))
                # 2) 조건 실시간 등록
                await ws.send(json.dumps({
                    "trnm": "CNSRREQ", "seq": self.condition_seq,
                    "search_type": "1",  # 1 = 실시간 감시 등록
                }))
                # 3) 이벤트 루프
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    trnm = msg.get("trnm")
                    if trnm == "PING":
                        # "안 끊겼어" → "응 안 끊겼어" 로 그대로 반사
                        await ws.send(raw)
                        emit(StreamEvent(datetime.utcnow(), "heartbeat"))
                        continue
                    if trnm == "REAL":
                        # 9001 필드에 종목 코드
                        symbol = msg.get("9001") or msg.get("data", {}).get("9001")
                        emit(StreamEvent(datetime.utcnow(), "signal",
                                         symbol=symbol, payload=msg))
                        continue
                    # 그 외는 진단용
                    emit(StreamEvent(datetime.utcnow(), "meta", payload=msg))

        try:
            import asyncio
            asyncio.run(_pump())
        except Exception as exc:  # pragma: no cover — 실 네트워크는 테스트하지 않음
            emit(StreamEvent(datetime.utcnow(), "error", None, {"error": str(exc)}))
            raise
