"""한국투자증권 Open API 어댑터.

실계좌/모의계좌 REST 를 얇게 감싸는 스텁이다. 자격증명이 비어 있으면
연결하지 않고 예외로 명확하게 알린다. 실제 배포 시 웹소켓 실시간 시세와
주문 통보 스트림을 별도로 붙이면 되며, 이 모듈은 그 붙임점이 되는 최소 표면만
정의한다. (실제 KIS 요구사항에 따라 엔드포인트·헤더·서명을 채워 넣어야 한다.)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ..config import KISConfig
from ..models import Fill, Order, Position, Side
from .base import Broker, BrokerError

REAL_BASE = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"


@dataclass
class _Token:
    value: str
    expires_at: float


class KISBroker(Broker):
    def __init__(self, config: KISConfig):
        if not config.app_key or not config.app_secret or not config.account_number:
            raise BrokerError(
                "KIS 자격증명이 비어 있습니다. 환경변수 KIS_APP_KEY / KIS_APP_SECRET / "
                "KIS_ACCOUNT_NUMBER 를 설정하거나 config.yaml 을 채우세요."
            )
        self.config = config
        self.base = PAPER_BASE if config.is_paper else REAL_BASE
        self._token: Optional[_Token] = None
        # requests 는 옵션 의존성. 없으면 사용 시점에만 실패하도록 한다.
        try:
            import requests  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise BrokerError("requests 패키지가 필요합니다: pip install requests") from exc

    # --- 인증 ---------------------------------------------------------------
    def _http(self):
        import requests
        return requests

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at - 60 > now:
            return self._token.value
        r = self._http().post(
            f"{self.base}/oauth2/tokenP",
            data=json.dumps({
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "appsecret": self.config.app_secret,
            }),
            headers={"content-type": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"KIS 토큰 발급 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        # KIS 정책상 토큰 유효기간은 24시간, 1일 1회 발급 원칙.
        self._token = _Token(js["access_token"], now + int(js.get("expires_in", 60 * 60 * 12)))
        return self._token.value

    def _headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._ensure_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # --- 잔고 / 시세 --------------------------------------------------------
    def cash(self) -> float:
        r = self._http().get(
            f"{self.base}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            headers=self._headers("VTTC8908R" if self.config.is_paper else "TTTC8908R"),
            params={
                "CANO": self.config.account_number,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "PDNO": "005930", "ORD_UNPR": "0",
                "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            }, timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"KIS 잔고 조회 실패: {r.status_code}")
        try:
            return float(r.json()["output"]["ord_psbl_cash"])
        except (KeyError, ValueError, TypeError) as exc:
            raise BrokerError(f"KIS 응답 파싱 실패: {exc}")

    def positions(self) -> Dict[str, Position]:
        r = self._http().get(
            f"{self.base}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._headers("VTTC8434R" if self.config.is_paper else "TTTC8434R"),
            params={
                "CANO": self.config.account_number,
                "ACNT_PRDT_CD": self.config.account_product_code,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            }, timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"KIS 포지션 조회 실패: {r.status_code}")
        out: Dict[str, Position] = {}
        for row in r.json().get("output1", []) or []:
            qty = int(float(row.get("hldg_qty", 0)))
            if qty <= 0:
                continue
            sym = row["pdno"]
            avg = float(row.get("pchs_avg_pric", 0.0))
            out[sym] = Position(sym, qty, avg, datetime.utcnow())
        return out

    # --- 주문 ---------------------------------------------------------------
    def submit(self, order: Order, price_hint: float) -> Fill:
        side_code = "02" if order.side is Side.BUY else "01"  # 02:매수 01:매도 (매수·매도 TR 는 별도)
        payload = {
            "CANO": self.config.account_number,
            "ACNT_PRDT_CD": self.config.account_product_code,
            "PDNO": order.symbol,
            "ORD_DVSN": "00" if order.type.value == "LIMIT" else "01",  # 00:지정가 01:시장가
            "ORD_QTY": str(order.qty),
            "ORD_UNPR": str(int(order.limit_price or 0)),
        }
        if order.side is Side.BUY:
            tr_id = "VTTC0802U" if self.config.is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.config.is_paper else "TTTC0801U"
        r = self._http().post(
            f"{self.base}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            data=json.dumps(payload), timeout=10,
        )
        if r.status_code != 200:
            raise BrokerError(f"KIS 주문 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        if js.get("rt_cd") != "0":
            raise BrokerError(f"KIS 주문 거부: {js.get('msg1')}")
        # 실제 체결가는 체결통보 웹소켓 또는 조회로 확인. 여기서는 접수 응답을
        # 그대로 근사치로 담아 리턴한다.
        return Fill(
            ts=datetime.utcnow(),
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=float(order.limit_price or price_hint),
            fee=0.0, tax=0.0, tag=order.tag or js.get("output", {}).get("ODNO", ""),
        )
