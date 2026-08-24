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

from ..config import KiwoomConfig, kiwoom_token_ttl
from ..orders import BrokerOrder, ExecutionReport, OrderStatus
from ..models import Fill, Order, Position, Side
from .base import Broker, BrokerError
from ..market import now_kst

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
        # 키움은 인증에 실패해도 HTTP 200 을 준다. return_code 로만 구분된다.
        # 곧바로 js["token"] 을 읽으면 KeyError: 'token' 이 나고, 사용자는
        # 앱키가 틀렸는지 서버가 이상한지 우리 코드가 깨졌는지 알 수 없다.
        code = js.get("return_code")
        token = js.get("token") or js.get("access_token")
        if code not in (None, 0) or not token:
            msg = js.get("return_msg") or js.get("msg1") or "응답에 토큰이 없습니다"
            raise BrokerError(
                f"Kiwoom 인증 실패: {msg} (return_code={code})\n"
                f"  · 앱키/시크릿이 맞는지 확인하세요 "
                f"(환경변수 KIWOOM_APP_KEY / KIWOOM_APP_SECRET)\n"
                f"  · 모의/실전 서버가 키와 맞는지 확인하세요 "
                f"(지금 mode={'paper' if self.cfg.is_paper else 'real'})")
        # expires_dt 는 "만료일" 이지 "남은 초" 가 아니다 — config.kiwoom_token_ttl 참고.
        self._token = _Token(token, now + kiwoom_token_ttl(js))
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
            # opened_at 은 **모른다**. 브로커 잔고에는 진입 시각이 없다.
            # 여기서 now_kst() 를 넣으면 매 조회마다 "방금 샀다" 가 되어
            # 보유기간 청산이 영원히 안 걸리고, 청산 주문 id 도 매번 바뀐다.
            # 진입 시각은 우리 기록에서 온다 — recovery.reconcile_positions 가
            # 이 값을 덮어쓴다. 여기 값은 기록이 아예 없을 때의 대체값이다.
            out[sym] = Position(sym, qty, avg, now_kst())
        return out

    # ---------------------------------------------------------------- 주문
    def submit(self, order: Order, price_hint: float) -> BrokerOrder:
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
        bo = BrokerOrder(
            client_order_id=order.client_order_id,
            symbol=order.symbol, side=order.side, qty=order.qty,
            status=OrderStatus.SUBMITTED, tag=order.tag,
        )
        if r.status_code != 200:
            bo.transition(OrderStatus.REJECTED,
                          reason=f"HTTP {r.status_code}: {r.text[:200]}")
            return bo
        js = r.json()
        if js.get("return_code", 0) != 0:
            # 거부는 예외가 아니라 상태다. 예외로 던지면 호출부가 "주문이
            # 안 나갔다" 와 "브로커가 거부했다" 를 구분하지 못한다.
            bo.transition(OrderStatus.REJECTED,
                          reason=str(js.get("return_msg") or "거부"))
            return bo
        # 여기까지가 **접수**다. 체결이 아니다.
        #
        # 예전 코드는 이 자리에서 Fill 을 만들어 돌려줬다. 그러면 호가창에
        # 걸려만 있는 주문, 100주 중 30주만 나간 부분체결, 증거금 부족으로
        # 거부된 주문이 전부 "전량 체결" 로 기록된다. 포트폴리오는 있지도 않은
        # 포지션을 들고 있다고 믿고, RiskEngine 은 그 허구 위에서 다음 진입을
        # 판단한다. 실제 체결은 체결통보(WebSocket)나 주문조회로만 들어온다.
        bo.broker_order_id = str(js.get("ord_no") or "") or None
        bo.transition(OrderStatus.ACCEPTED)
        return bo

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
