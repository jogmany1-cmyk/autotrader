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
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import KiwoomConfig, kiwoom_min_interval, kiwoom_token_ttl
from ..market import now_kst
from ..models import Bar
from .base import DataError, DataProvider

KIWOOM_REST_REAL = "https://api.kiwoom.com"
KIWOOM_REST_PAPER = "https://mockapi.kiwoom.com"


@dataclass
class _Token:
    value: str
    expires_at: float


log = logging.getLogger(__name__)

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
        # 직전 refresh_* 호출에서 실패한 (종목, 사유). 개수만으로는 무엇이
        # 잘못됐는지 알 수 없어 수집이 조용히 0건으로 끝나던 문제를 막는다.
        self.last_failures: List[Tuple[str, str]] = []
        # 직전 응답의 진단 정보. 벤더 응답 형태가 문서와 다를 때 "시세 없음" 만
        # 남기고 끝나면 원인을 알 수 없어서, 무엇이 돌아왔는지 함께 보고한다.
        self.last_response_meta: Dict[str, object] = {}
        # True 면 응답 본문 일부를 그대로 출력한다 (CLI --debug).
        # 요청 헤더는 절대 찍지 않는다 — appkey/secret 이 들어 있다.
        self.debug = False
        # 유량 제한은 모드에 따라 다르다 (공식 문서 기준).
        #   모의투자 : TR 1개당 1초 1회   → 1.1초
        #   실서버   : 조회 TR 1초당 5회  → 0.25초
        # 연속조회는 한 종목에 최대 30페이지를 연달아 요청하므로, 대기 없이
        # 몰아치면 첫 페이지부터 429 를 맞는다.
        self.min_interval = kiwoom_min_interval(config.is_paper)
        self._last_request_at = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------- 인증·HTTP
    def _http(self):
        import requests
        return requests

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at - 60 > now:
            return self._token.value
        r = self._post(
            "/oauth2/token",
            {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.app_secret,
            },
            headers={"content-type": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            raise DataError(f"Kiwoom 토큰 발급 실패 {r.status_code}: {r.text[:200]}")
        js = r.json()
        # 키움은 인증에 실패해도 HTTP 200 을 준다. return_code 로만 구분된다.
        # 예전에는 곧바로 js["token"] 을 읽어서 KeyError: 'token' 이 났다 —
        # 사용자는 앱키가 틀렸는지, 서버가 이상한지, 우리 코드가 깨졌는지
        # 알 수 없다. 인증 실패는 인증 실패라고 말해야 한다.
        code = js.get("return_code")
        token = js.get("token") or js.get("access_token")
        if code not in (None, 0) or not token:
            msg = js.get("return_msg") or js.get("msg1") or "응답에 토큰이 없습니다"
            raise DataError(
                f"Kiwoom 인증 실패: {msg} (return_code={code})\n"
                f"  · 앱키/시크릿이 맞는지 확인하세요 "
                f"(환경변수 KIWOOM_APP_KEY / KIWOOM_APP_SECRET)\n"
                f"  · 모의/실전 서버가 키와 맞는지 확인하세요 "
                f"(지금 mode={'paper' if self.config.is_paper else 'real'})")
        # expires_dt 는 "만료일" 이지 "남은 초" 가 아니다 — config.kiwoom_token_ttl 참고.
        self._token = _Token(token, now + kiwoom_token_ttl(js))
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
        r = self._post(
            "/api/dostk/stkinfo",
            {"mrkt_tp": market_code},
            headers=self._headers("ka10099"),
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
            raise DataError(f"{symbol}: 시세 없음 — {self._diag()}")
        return merged[-limit:] if limit else merged

    def last_price(self, symbol: str) -> float:
        bars = self.history(symbol, limit=2)
        return bars[-1].close

    def history_minutes(self, symbol: str, interval: int = 5,
                        limit: int = 500) -> List[Bar]:
        """분봉 수집 (ka10080). interval ∈ {1,3,5,10,15,30,45,60}.
        단타 전략에 필수 — 벤더가 최근 N일치만 제공하는 것이 일반적이라,
        Cron 잡 collect-5m 으로 매 5분마다 이어받아 자체 시계열 DB 를 축적하는
        것을 권장. 캐시 파일명은 `{symbol}_{interval}m.csv`."""
        if interval not in (1, 3, 5, 10, 15, 30, 45, 60):
            raise DataError(f"지원되지 않는 분봉 간격: {interval}")
        cached = self._load_minute_cache(symbol, interval)
        fresh = self._fetch_minutes(symbol, interval)
        merged = _merge_bars(cached, fresh)
        if fresh:
            self._save_minute_cache(symbol, interval, merged)
        if not merged:
            raise DataError(f"{symbol}: {interval}분봉 없음")
        return merged[-limit:] if limit else merged

    def _fetch_minutes(self, symbol: str, interval: int) -> List[Bar]:
        out: List[Bar] = []
        cont_yn, next_key = "N", ""
        for _ in range(10):  # 분봉은 페이지가 많을 수 있어 상한 낮게
            r = self._post(
                "/api/dostk/chart",
                {
                    "stk_cd": symbol,
                    "tic_scope": str(interval),
                    "upd_stkpc_tp": "1",
                },
                headers=self._headers("ka10080", cont_yn=cont_yn, next_key=next_key),
            )
            if r.status_code != 200:
                raise DataError(f"Kiwoom 분봉 실패({symbol}, {interval}m): "
                                f"HTTP {r.status_code} {r.text[:200]}")
            js = self._inspect(r, symbol, "ka10080")
            for row in self._rows(js):
                try:
                    out.append(self._bar_from_row(row))
                except (ValueError, TypeError):
                    continue
            cont_yn = r.headers.get("cont-yn", "N")
            next_key = r.headers.get("next-key", "")
            if cont_yn != "Y" or not next_key:
                break
        out.sort(key=lambda b: b.ts)
        return out

    def _minute_cache_path(self, symbol: str, interval: int) -> str:
        return os.path.join(self.cache_dir, f"{symbol}_{interval}m.csv")

    def _load_minute_cache(self, symbol: str, interval: int) -> List[Bar]:
        path = self._minute_cache_path(symbol, interval)
        if not os.path.exists(path):
            return []
        bars: List[Bar] = []
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    bars.append(Bar(
                        ts=datetime.fromisoformat(row["date"]),
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=float(row.get("volume", 0) or 0),
                    ))
                except (ValueError, KeyError):
                    continue
        bars.sort(key=lambda b: b.ts)
        return bars

    def _save_minute_cache(self, symbol: str, interval: int,
                           bars: Sequence[Bar]) -> None:
        path = self._minute_cache_path(symbol, interval)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for b in bars:
                writer.writerow([b.ts.isoformat(sep=" "),
                                 b.open, b.high, b.low, b.close, b.volume])

    def _fetch_daily(self, symbol: str, since=None) -> List[Bar]:
        out: List[Bar] = []
        cont_yn, next_key = "N", ""
        for _ in range(30):  # 페이지네이션 최대 30회 안전 상한
            r = self._post(
                "/api/dostk/chart",
                {
                    "stk_cd": symbol,
                    # base_dt 는 필수다. 빈 문자열이 "오늘" 로 해석될 거라고
                    # 추측했다가 키움이 이렇게 거절했다:
                    #   return_code=2 입력 값 오류입니다
                    #   [1511:필수 입력 값에 값이 존재하지 않습니다. 파라미터=base_dt]
                    # 조회 기준일은 한국 시간 기준이어야 한다 (UTC 로 넣으면
                    # 한국 시간 09:00 이전에 하루 전 날짜가 들어간다).
                    "base_dt": now_kst().strftime("%Y%m%d"),
                    "upd_stkpc_tp": "1", # 수정주가 사용
                },
                headers=self._headers("ka10081", cont_yn=cont_yn, next_key=next_key),
            )
            if r.status_code != 200:
                raise DataError(
                    f"Kiwoom 일봉 실패({symbol}): HTTP {r.status_code} {r.text[:200]}")
            js = self._inspect(r, symbol, "ka10081")
            for row in self._rows(js):
                try:
                    out.append(self._bar_from_row(row))
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

    _DATE_FORMATS = ("%Y%m%d", "%Y%m%d%H%M%S", "%Y-%m-%d", "%Y%m%d%H%M")

    def _bar_from_row(self, row: Dict) -> Bar:
        """한 행을 Bar 로. 필드 이름·부호·날짜형식 차이를 흡수한다."""
        raw_dt = str(self._pick(row, "date") or "").strip()
        ts = None
        for fmt in self._DATE_FORMATS:
            try:
                ts = datetime.strptime(raw_dt, fmt)
                break
            except ValueError:
                continue
        if ts is None:
            raise ValueError(f"날짜 형식을 알 수 없음: {raw_dt!r}")
        close = self._to_price(self._pick(row, "close"))
        # 시/고/저가 빠진 응답이면 종가로 메운다 (분봉 일부 응답에서 발생).
        def _or_close(field):
            try:
                return self._to_price(self._pick(row, field))
            except (ValueError, TypeError):
                return close
        volume_raw = self._pick(row, "volume")
        return Bar(
            ts=ts,
            open=_or_close("open"),
            high=_or_close("high"),
            low=_or_close("low"),
            close=close,
            volume=abs(float(str(volume_raw).replace(",", "").lstrip("+")))
                   if volume_raw not in (None, "") else 0.0,
        )

    # 봉 배열 키 후보. 벤더 문서 기준이지만 여기에만 의존하지 않는다.
    _ROW_KEYS = ("stk_dt_pole_chart_qry", "stk_min_pole_chart_qry",
                 "stk_stk_pole_chart_qry", "list", "output", "output1", "output2")

    # 한 행 안의 필드 별칭. 벤더가 이름을 바꿔도 한 곳만 고치면 되도록 모아둔다.
    _FIELD_ALIASES = {
        "date": ("dt", "stck_bsop_date", "base_dt", "cntr_tm", "trde_dt"),
        "open": ("open_pric", "opn_prc", "stck_oprc", "open"),
        "high": ("high_pric", "hgh_prc", "stck_hgpr", "high"),
        "low": ("low_pric", "low_prc", "stck_lwpr", "low"),
        "close": ("cur_prc", "clos_pric", "stck_clpr", "close", "prpr"),
        "volume": ("trde_qty", "acml_vol", "cntg_vol", "volume"),
    }

    def _rows(self, js) -> List[Dict]:
        """응답에서 봉 배열을 찾아낸다.

        이전 구현은 배열 키 이름을 코드에 못박아 두고 `js.get(그 이름, [])` 로
        읽었다. 실제 응답의 키가 다르면 조용히 0건이 되어 "시세 없음" 으로
        끝났다 — 사용자의 첫 실전 호출이 정확히 그렇게 실패했다.

        이름을 맞히려 하지 말고 찾는다: 알려진 후보를 먼저 보고, 없으면
        사전(dict)을 담은 첫 번째 리스트를 봉 배열로 간주한다.
        """
        if not isinstance(js, dict):
            return []
        for key in self._ROW_KEYS:
            v = js.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in js.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return []

    @classmethod
    def _pick(cls, row: Dict, field: str):
        for name in cls._FIELD_ALIASES[field]:
            if name in row and row[name] not in (None, ""):
                return row[name]
        return None

    @classmethod
    def _to_price(cls, raw) -> float:
        """키움은 부호가 붙은 문자열('+73400', '-1200')로 가격을 주기도 한다."""
        if raw is None:
            raise ValueError("빈 값")
        return abs(float(str(raw).replace(",", "").strip().lstrip("+")))

    # ------------------------------------------------------- 요청 통로
    # 모든 키움 호출은 _post 를 통과한다. 유량 제한 준수와 429 재시도를 한 곳에
    # 두기 위해서다. 이전에는 네 군데가 각자 requests.post 를 불러서, 유량을
    # 지키는 곳도 429 를 처리하는 곳도 없었다. 사용자의 실제 수집이 이렇게 죽었다:
    #   HTTP 429 허용된 API 요청 개수를 초과하였습니다 [유량=1, API ID=ka10081]

    _RETRIES = 3

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        gap = time.monotonic() - self._last_request_at
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_request_at = time.monotonic()

    def _retry_wait(self, response, attempt: int) -> float:
        """서버가 Retry-After 를 주면 따르고, 없으면 지수 백오프."""
        raw = (response.headers or {}).get("Retry-After")
        try:
            if raw is not None:
                return max(float(raw), self.min_interval)
        except (TypeError, ValueError):
            pass
        return max(self.min_interval, 1.0) * (2 ** attempt)

    def _post(self, path: str, payload: Dict, headers: Dict,
              timeout: int = 15):
        """키움 POST 한 번. 유량을 지키고 429 는 물러섰다가 다시 시도한다."""
        url = f"{self.base}{path}"
        body = json.dumps(payload)
        for attempt in range(self._RETRIES + 1):
            self._throttle()
            r = self._http().post(url, headers=headers, data=body, timeout=timeout)
            if r.status_code != 429:
                return r
            if attempt == self._RETRIES:
                raise DataError(
                    f"키움 요청 한도 초과 — {self._RETRIES + 1}회 시도했지만 계속 "
                    f"429 입니다. --min-interval 을 올려 보세요 "
                    f"(현재 {self.min_interval}초). {r.text[:200]}")
            wait = self._retry_wait(r, attempt)
            log.warning("429 유량 초과 — %.1f초 후 재시도 (%d/%d)",
                        wait, attempt + 1, self._RETRIES)
            time.sleep(wait)

    # ---------------------------------------------------- 응답 진단
    # 키움은 HTTP 200 으로 응답하면서 본문에 오류 코드를 담는다. 이것을 보지
    # 않으면 "권한 없음" 같은 업무 오류가 그냥 빈 데이터로 보여 "시세 없음"
    # 으로 둔갑한다. 실제로 그 일이 일어나 원인 파악에 시간을 썼다.
    _RC_KEYS = ("return_code", "rt_cd", "returnCode")
    _MSG_KEYS = ("return_msg", "msg1", "returnMsg", "msg")

    def _inspect(self, response, symbol: str, api_id: str) -> Dict:
        """응답 JSON 을 돌려주되, 벤더 오류 코드를 먼저 걸러낸다."""
        js = response.json()
        meta = {
            "status": response.status_code,
            "api_id": api_id,
            "symbol": symbol,
            "keys": sorted(js.keys())[:12] if isinstance(js, dict) else type(js).__name__,
        }
        if isinstance(js, dict):
            for k in self._RC_KEYS:
                if k in js:
                    meta["return_code"] = js[k]
                    break
            for k in self._MSG_KEYS:
                if k in js:
                    meta["return_msg"] = js[k]
                    break
        self.last_response_meta = meta
        if self.debug:
            body = response.text
            print(f"  [DEBUG] {api_id} {symbol} status={response.status_code} "
                  f"keys={meta['keys']}")
            print(f"  [DEBUG] body[:800]={body[:800]}")
        rc = meta.get("return_code")
        if rc is not None and str(rc) not in ("0", "None"):
            raise DataError(
                f"{symbol}: 키움이 오류를 반환했습니다 "
                f"({api_id} return_code={rc}: {meta.get('return_msg', '메시지 없음')})")
        return js

    def _diag(self) -> str:
        """마지막 응답이 어떻게 생겼는지 한 줄 요약 — 오류 메시지에 붙인다."""
        m = self.last_response_meta
        if not m:
            return "응답 정보 없음"
        return (f"HTTP {m.get('status')} · 응답 최상위 키={m.get('keys')}"
                + (f" · return_msg={m.get('return_msg')}" if m.get("return_msg") else ""))

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
    def refresh_minutes(self, symbols: Optional[Sequence[str]] = None,
                        interval: int = 5, limit: int = 500) -> Tuple[int, int]:
        """분봉 최신화. Cron 잡 collect-5m 에서 매 5분마다 호출."""
        symbols = list(symbols) if symbols else self.universe()
        return self._refresh(
            symbols,
            lambda sym: self.history_minutes(sym, interval=interval, limit=limit))

    def refresh_all(self, symbols: Optional[Sequence[str]] = None,
                    limit: int = 500) -> Tuple[int, int]:
        """유니버스(또는 지정 심볼)의 시세를 최신화. (성공, 실패) 개수 리턴.
        Cron 잡 collect-daily 에서 호출하도록 설계.
        """
        symbols = list(symbols) if symbols else self.universe()
        return self._refresh(symbols, lambda sym: self.history(sym, limit=limit))

    def _refresh(self, symbols: Sequence[str], fetch_one) -> Tuple[int, int]:
        """종목별 수집 루프. 실패는 세는 것으로 끝내지 않고 사유를 남긴다.

        이전 구현은 `except DataError: fail += 1` 로 예외를 통째로 버려서,
        수집이 전부 실패해도 화면에는 `ok=0 fail=1` 만 찍혔다. 무엇이
        잘못됐는지 알 수 없으면 고칠 수도 없다. 사유를 `last_failures` 에
        모으고 경고 로그로도 남긴다.

        DataError 뿐 아니라 모든 예외를 잡는다. 한 종목의 네트워크 오류가
        유니버스 전체 수집을 중단시키면 안 되기 때문이다. 사유를 기록하므로
        삼키는 것이 아니라 미루는 것이다.
        """
        self.last_failures = []
        ok = 0
        total = len(symbols)
        started = time.time()
        for i, sym in enumerate(symbols, 1):
            try:
                fetch_one(sym)
                ok += 1
            except Exception as exc:                    # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                self.last_failures.append((sym, reason))
                log.warning("수집 실패 %s — %s", sym, reason)
            # 진행률. 이게 없으면 4천 종목 1~2시간 동안 실패 몇 줄 말고는
            # 아무것도 안 찍혀서, 사용자는 멈춘 것으로 오인하고 창을 닫는다 —
            # 실제로 그런 일이 있었다. 조용한 성공의 사용자 인터페이스판이다.
            if i % 50 == 0 or i == total:
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0.0
                eta_min = (total - i) / rate / 60 if rate > 0 else 0.0
                print(f"  [{i}/{total}] ok={ok} fail={len(self.last_failures)} "
                      f"경과 {elapsed/60:.0f}분 · 남은 예상 {eta_min:.0f}분",
                      flush=True)
        return ok, len(self.last_failures)


def _merge_bars(a: Sequence[Bar], b: Sequence[Bar]) -> List[Bar]:
    """캐시 봉과 새 봉을 합치되 날짜 기준 중복 제거 (새 것이 우선)."""
    by_date: Dict[str, Bar] = {}
    for bar in a:
        by_date[bar.ts.strftime("%Y-%m-%d")] = bar
    for bar in b:
        by_date[bar.ts.strftime("%Y-%m-%d")] = bar
    return sorted(by_date.values(), key=lambda x: x.ts)
