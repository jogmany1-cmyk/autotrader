"""KiwoomProvider — 키움 REST API 를 DataProvider 로 감싸는 어댑터.

배경: 지금까지 우리 백테스트는 CSV 를 사람이 직접 내려받아야 했다. 실전 자동
매매의 데이터 파이프라인은 그러면 안 된다. 매일 아침 스크립트 하나가:
- 종목 목록을 새로 받아 유니버스 갱신
- 각 종목의 최근 봉을 이어받아 CSV 캐시에 append
- 오래된 데이터는 그대로 두고, 앞으로 매일 자동 누적

이 클래스는 그 파이프라인의 데이터 원천이다. 자격증명이 비면 안전하게 실패한다.
CsvProvider 와 같은 폴더 구조에 캐시를 저장하므로, 오프라인 백테스트 때는
CsvProvider 로 스위칭만 하면 그대로 재사용된다.

주의 — 데이터 품질 함정 (붙여넣어 주신 분석 그대로):
① 생존자 편향: 현재 상장 종목만 조회하면 상장폐지된 과거 종목이 빠진다.
   장기 백테스트에는 KRX 과거 종목 유니버스로 보완이 필요.
② 분봉 제공 기간: 벤더 정책상 최근 N일치만 내려올 수 있다. 매일 저장해
   자체 시계열 DB 를 축적하는 것이 정석.
③ 수정주가·액면분할·거래정지·신규상장 이력은 벤더가 이미 반영해 주는지
   실 계정으로 반드시 확인.
④ 연속조회 (cont-yn / next-key) 는 반드시 사용. 안 그러면 최근 N건만 받고 끝남.

이 저장소에서는 실 네트워크 호출을 테스트하지 않는다. TR ID·필드명은 벤더
문서(개발자 센터)의 최신값으로 실 계정에서 검증한 뒤 실전 배포해야 한다.
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import KiwoomConfig
from ..models import Bar
from .base import DataError, DataProvider

KIWOOM_REST_REAL = "https://api.kiwoom.com"
KIWOOM_REST_PAPER = "https://mockapi.kiwoom.com"


@dataclass
class _Token:
    value: str
    expires_at: float


class KiwoomProvider(DataProvider):
    """키움 REST 로 종목·일봉·분봉을 가져오는 DataProvider.

    캐시가 있으면 캐시를 우선 쓰고, 부족한 최근 봉만 API 로 이어받는다. 캐시는
    `cache_dir/{symbol}.csv` 형식으로 CsvProvider 와 완전히 호환된다.
    """

    def __init__(self, config: KiwoomConfig, cache_dir: str,
                 default_market: str = "0"):
        if not config.app_key or not config.app_secret:
            raise DataError(
                "Kiwoom 자격증명이 비어 있습니다. 환경변수 KIWOOM_APP_KEY / "
                "KIWOOM_APP_SECRET 를 설정하거나 config.yaml 을 채우세요."
            )
        try:
            import requests  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise DataError("requests 패키지가 필요합니다: pip install requests") from exc
        self.config = config
        self.base = KIWOOM_REST_PAPER if config.is_paper else KIWOOM_REST_REAL
        self.cache_dir = cache_dir
        self.default_market = default_market
        self._token: Optional[_Token] = None
        self._universe_cache: Optional[List[str]] = None
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------- 인증·HTTP
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
            raise DataError(f"Kiwoom 토큰 발급 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        ttl = int(js.get("expires_in") or js.get("expires_dt", 43200))
        self._token = _Token(js["token"], now + ttl)
        return self._token.value

    def _headers(self, api_id: str, cont_yn: str = "N",
                 next_key: str = "") -> Dict[str, str]:
        return {
            "content-type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._ensure_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "api-id": api_id,
            "cont-yn": cont_yn,      # 연속조회 여부
            "next-key": next_key,    # 연속조회 키
        }

    # ----------------------------------------------------- 종목 마스터
    def universe(self) -> List[str]:
        if self._universe_cache is not None:
            return list(self._universe_cache)
        universe: List[str] = []
        for market in ("0", "10"):  # 0=코스피 10=코스닥
            for row in self._fetch_symbols(market):
                sym = str(row.get("stk_cd") or row.get("code") or "").strip()
                if sym:
                    universe.append(sym)
        self._universe_cache = universe
        return list(universe)

    def _fetch_symbols(self, market_code: str) -> List[Dict[str, Any]]:
        r = self._http().post(
            f"{self.base}/api/dostk/stkinfo",
            headers=self._headers("ka10099"),
            data=json.dumps({"mrkt_tp": market_code}),
            timeout=15,
        )
        if r.status_code != 200:
            raise DataError(f"Kiwoom 종목목록 실패({market_code}): {r.status_code}")
        return list(r.json().get("list", []))

    # ------------------------------------------------------- 시세 일봉
    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        """캐시 우선 + 부족한 부분만 API 로 이어 받아 CSV 로 누적."""
        cached = self._load_cache(symbol)
        if cached and len(cached) >= limit:
            return cached[-limit:]
        # 새 API 호출: 캐시가 있으면 마지막 날짜부터 오늘까지만 요청.
        last_ts = cached[-1].ts.date() if cached else None
        fresh = self._fetch_daily(symbol, since=last_ts)
        merged = _merge_bars(cached, fresh)
        if fresh:
            self._save_cache(symbol, merged)
        if not merged:
            raise DataError(f"{symbol}: 시세 없음")
        return merged[-limit:] if limit else merged

    def last_price(self, symbol: str) -> float:
        bars = self.history(symbol, limit=2)
        return bars[-1].close

    def _fetch_daily(self, symbol: str, since=None) -> List[Bar]:
        out: List[Bar] = []
        cont_yn, next_key = "N", ""
        for _ in range(30):  # 페이지네이션 최대 30회 안전 상한
            r = self._http().post(
                f"{self.base}/api/dostk/chart",
                headers=self._headers("ka10081", cont_yn=cont_yn, next_key=next_key),
                data=json.dumps({
                    "stk_cd": symbol,
                    "base_dt": "",       # 오늘 기준
                    "upd_stkpc_tp": "1", # 수정주가 사용
                }),
                timeout=15,
            )
            if r.status_code != 200:
                raise DataError(f"Kiwoom 일봉 실패({symbol}): {r.status_code}")
            js = r.json()
            for row in js.get("stk_dt_pole_chart_qry", []) or js.get("list", []):
                try:
                    ts = datetime.strptime(str(row.get("dt")), "%Y%m%d")
                    out.append(Bar(
                        ts=ts,
                        open=float(row.get("open_pric", 0)),
                        high=float(row.get("high_pric", 0)),
                        low=float(row.get("low_pric", 0)),
                        close=float(row.get("cur_prc", 0)),
                        volume=float(row.get("trde_qty", 0)),
                    ))
                except (ValueError, TypeError):
                    continue
            # 페이지네이션 (연속조회) — 헤더에 cont-yn=Y 이면 다음 next-key 로 이어 받음.
            cont_yn = r.headers.get("cont-yn", "N")
            next_key = r.headers.get("next-key", "")
            if cont_yn != "Y" or not next_key:
                break
            # 이미 캐시된 구간까지 왔으면 조기 종료 (오래된 데이터는 덮어쓸 필요 없음).
            if since and out and out[-1].ts.date() <= since:
                break
        out.sort(key=lambda b: b.ts)
        return out

    # -------------------------------------------------------- 캐시 IO
    def _cache_path(self, symbol: str) -> str:
        return os.path.join(self.cache_dir, f"{symbol}.csv")

    def _load_cache(self, symbol: str) -> List[Bar]:
        path = self._cache_path(symbol)
        if not os.path.exists(path):
            return []
        bars: List[Bar] = []
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    bars.append(Bar(
                        ts=datetime.fromisoformat(row["date"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0) or 0),
                    ))
                except (ValueError, KeyError):
                    continue
        bars.sort(key=lambda b: b.ts)
        return bars

    def _save_cache(self, symbol: str, bars: Sequence[Bar]) -> None:
        path = self._cache_path(symbol)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for b in bars:
                writer.writerow([b.ts.strftime("%Y-%m-%d"),
                                 b.open, b.high, b.low, b.close, b.volume])

    # ----------------------------------- 데이터 컬렉터 (매일 자동 수집)
    def refresh_all(self, symbols: Optional[Sequence[str]] = None,
                    limit: int = 500) -> Tuple[int, int]:
        """유니버스(또는 지정 심볼)의 시세를 최신화. (성공, 실패) 개수 리턴.
        Cron 잡 collect-daily 에서 호출하도록 설계.
        """
        symbols = list(symbols) if symbols else self.universe()
        ok = fail = 0
        for sym in symbols:
            try:
                self.history(sym, limit=limit)
                ok += 1
            except DataError:
                fail += 1
        return ok, fail


def _merge_bars(a: Sequence[Bar], b: Sequence[Bar]) -> List[Bar]:
    """캐시 봉과 새 봉을 합치되 날짜 기준 중복 제거 (새 것이 우선)."""
    by_date: Dict[str, Bar] = {}
    for bar in a:
        by_date[bar.ts.strftime("%Y-%m-%d")] = bar
    for bar in b:
        by_date[bar.ts.strftime("%Y-%m-%d")] = bar
    return sorted(by_date.values(), key=lambda x: x.ts)
