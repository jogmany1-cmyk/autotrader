"""키움증권 Open API (REST) 얇은 어댑터.

v0.5 의 KiwoomConditionStream(WebSocket)과 짝을 이루는 REST 쪽 구현.
자격증명이 비어 있으면 즉시 명확한 예외로 실패한다 (KISBroker 와 동일 패턴).

핵심 원칙 (Chapter 0 튜토리얼과 매칭):
- 실전 / 모의투자 URL 을 코드에서 분리 (환경변수 KIWOOM_MODE 로 스위칭)
- 앱키(AppKey)·시크릿(AppSecret) 은 `config.py` 에서 로드, 코드에는 하드코딩 금지
- OAuth 토큰은 발급 후 24h 캐시. 재발급은 만료 1분 전에만.
- 요청 시 반드시 `Authorization: Bearer <token>` + `appkey` + `appsecret` +
  `api-id` 헤더를 함께 실어 보낸다.

실제 TR ID·엔드포인트 경로·필드명은 벤더 문서(개발자 센터)의 최신값으로 채워야
한다. 여기서는 토큰 발급 · 잔고 조회 · 현금 주문 · 종목마스터 4가지 골격만 구현.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import KiwoomConfig
from ..models import Fill, Order, Position, Side
from .base import Broker, BrokerError

KIWOOM_REST_REAL = "https://api.kiwoom.com"
KIWOOM_REST_PAPER = "https://mockapi.kiwoom.com"


@dataclass
class _Token:
    value: str
    expires_at: float


class KiwoomBroker(Broker):
    def __init__(self, config: KiwoomConfig):
        if not config.app_key or not config.app_secret or not config.account_number:
            raise BrokerError(
                "Kiwoom 자격증명이 비어 있습니다. 환경변수 KIWOOM_APP_KEY / "
                "KIWOOM_APP_SECRET / KIWOOM_ACCOUNT_NUMBER 를 설정하거나 "
                "config.yaml 의 kiwoom 섹션을 채우세요."
            )
        self.config = config
        self.base = KIWOOM_REST_PAPER if config.is_paper else KIWOOM_REST_REAL
        self._token: Optional[_Token] = None
        # requests 는 옵션 의존성 — 사용 시점에만 실패하게 남긴다.
        try:
            import requests  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise BrokerError("requests 패키지가 필요합니다: pip install requests") from exc

    # ------------------------------------------------------------------ 내부
    def _http(self):
        import requests
        return requests

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at - 60 > now:
            return self._token.value
        r = self._http().post(
            f"{self.base}/oauth2/token",
            data=json.dumps({
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.app_secret,
            }),
            headers={"content-type": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"Kiwoom 토큰 발급 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        # Kiwoom 토큰 유효기간은 응답 필드에 따르되, 없으면 12시간 기본.
        ttl = int(js.get("expires_in") or js.get("expires_dt", 43200))
        self._token = _Token(js["token"], now + ttl)
        return self._token.value

    def _headers(self, api_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._ensure_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "api-id": api_id,
        }

    # ---------------------------------------------------------------- 잔고
    def cash(self) -> float:
        r = self._http().post(
            f"{self.base}/api/dostk/acnt",
            headers=self._headers("kt00001"),  # 예수금 조회 (실제 TR ID 확인 필요)
            data=json.dumps({
                "qry_tp": "3",
                "trde_tp": "0",
            }),
            timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"Kiwoom 예수금 조회 실패: {r.status_code}")
        try:
            return float(r.json().get("ord_alow_amt", 0))
        except (ValueError, TypeError) as exc:
            raise BrokerError(f"Kiwoom 응답 파싱 실패: {exc}")

    def positions(self) -> Dict[str, Position]:
        r = self._http().post(
            f"{self.base}/api/dostk/acnt",
            headers=self._headers("kt00018"),  # 계좌평가 잔고 (실제 TR ID 확인 필요)
            data=json.dumps({"qry_tp": "1", "dmst_stex_tp": "KRX"}),
            timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"Kiwoom 잔고 조회 실패: {r.status_code}")
        out: Dict[str, Position] = {}
        for row in r.json().get("acnt_evlt_remn_indv_tot", []) or []:
            qty = int(float(row.get("rmnd_qty", 0)))
            if qty <= 0:
                continue
            sym = row.get("stk_cd", "").strip()
            avg = float(row.get("pur_pric", 0.0))
            out[sym] = Position(sym, qty, avg, datetime.utcnow())
        return out

    # ---------------------------------------------------------------- 주문
    def submit(self, order: Order, price_hint: float) -> Fill:
        # Kiwoom 은 매수/매도가 서로 다른 api-id 를 사용.
        api_id = "kt10000" if order.side is Side.BUY else "kt10001"
        payload = {
            "dmst_stex_tp": "KRX",
            "stk_cd": order.symbol,
            "ord_qty": str(order.qty),
            "ord_uv": str(int(order.limit_price or 0)),
            "trde_tp": "0" if order.type.value == "LIMIT" else "3",  # 3 = 시장가
        }
        r = self._http().post(
            f"{self.base}/api/dostk/ordr",
            headers=self._headers(api_id),
            data=json.dumps(payload),
            timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"Kiwoom 주문 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        if js.get("return_code", 0) != 0:
            raise BrokerError(f"Kiwoom 주문 거부: {js.get('return_msg')}")
        # 접수 응답 — 실제 체결가는 통보 WebSocket 이나 조회로 확인.
        return Fill(
            ts=datetime.utcnow(),
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=float(order.limit_price or price_hint),
            fee=0.0, tax=0.0,
            tag=order.tag or js.get("ord_no", ""),
        )

    # ---------------------------------------------------------- 종목 마스터
    def list_stocks(self, market_code: str = "0") -> List[Dict[str, Any]]:
        """종목 정보 리스트 (Chapter 0 튜토리얼의 예시 기능).

        market_code: "0"=코스피 · "10"=코스닥 (Kiwoom 코드체계).
        """
        r = self._http().post(
            f"{self.base}/api/dostk/stkinfo",
            headers=self._headers("ka10099"),  # 종목정보 리스트 (실제 TR ID 확인 필요)
            data=json.dumps({"mrkt_tp": market_code}),
            timeout=15,
        )
        if r.status_code != 200:
            raise BrokerError(f"Kiwoom 종목목록 조회 실패: {r.status_code}")
        return list(r.json().get("list", []))
