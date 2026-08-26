"""네이버 뉴스·OpenDART·SEC EDGAR 읽기 전용 공급자."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .http import JsonHttpClient
from .models import MarketEvent


def _plain(value: object) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"<[^>]+>", "", text).strip()


class NaverNewsProvider:
    endpoint = "https://openapi.naver.com/v1/search/news.json"

    def __init__(self, client_id: str, client_secret: str,
                 http: Optional[JsonHttpClient] = None):
        if not client_id or not client_secret:
            raise ValueError("NAVER_CLIENT_ID/SECRET 이 필요합니다")
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or JsonHttpClient()

    def search(self, query: str, *, symbol: str = "", company: str = "",
               display: int = 20) -> List[MarketEvent]:
        data = self.http.request(
            "GET", self.endpoint,
            headers={"X-Naver-Client-Id": self.client_id,
                     "X-Naver-Client-Secret": self.client_secret},
            query={"query": query, "display": min(max(display, 1), 100),
                   "sort": "date"},
        )
        events = []
        for item in data.get("items", []):
            try:
                published = parsedate_to_datetime(item["pubDate"])
            except Exception:
                published = datetime.now(timezone.utc)
            events.append(MarketEvent(
                source="naver-news", region="KR",
                title=_plain(item.get("title")),
                summary=_plain(item.get("description")),
                url=str(item.get("originallink") or item.get("link") or ""),
                published_at=published, symbol=symbol, company=company,
                event_type="news", official=False,
            ))
        return events


class OpenDartProvider:
    endpoint = "https://opendart.fss.or.kr/api/list.json"
    disclosure_url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

    def __init__(self, api_key: str,
                 http: Optional[JsonHttpClient] = None):
        if not api_key:
            raise ValueError("OPENDART_API_KEY 가 필요합니다")
        self.api_key = api_key
        self.http = http or JsonHttpClient()

    def disclosures(self, begin: date, end: date, *,
                    corp_to_symbol: Optional[Mapping[str, str]] = None,
                    page_count: int = 100) -> List[MarketEvent]:
        mapping = dict(corp_to_symbol or {})
        events = []
        page = 1
        while True:
            data = self.http.request(
                "GET", self.endpoint,
                query={"crtfc_key": self.api_key,
                       "bgn_de": begin.strftime("%Y%m%d"),
                       "end_de": end.strftime("%Y%m%d"),
                       "page_no": page,
                       "page_count": min(max(page_count, 1), 100)},
            )
            if data.get("status") == "013":
                return []
            if data.get("status") not in (None, "000"):
                raise RuntimeError(f"OpenDART 요청 실패: status={data.get('status')}")
            for item in data.get("list", []):
                receipt = str(item.get("rcept_no") or "")
                day = datetime.strptime(str(item["rcept_dt"]), "%Y%m%d").replace(
                    tzinfo=timezone.utc)
                corp_code = str(item.get("corp_code") or "")
                symbol = (str(item.get("stock_code") or "").strip()
                          or mapping.get(corp_code, ""))
                events.append(MarketEvent(
                    source="opendart", region="KR",
                    title=_plain(item.get("report_nm")),
                    url=self.disclosure_url.format(receipt),
                    published_at=day, symbol=symbol,
                    company=_plain(item.get("corp_name")),
                    event_type="disclosure", official=True,
                    tags=[str(item.get("corp_cls") or "")],
                ))
            total_pages = int(data.get("total_page") or 1)
            if page >= total_pages:
                break
            page += 1
        return events


class SecEdgarProvider:
    """관심 종목 CIK별 최근 주요 공시를 읽는다."""
    endpoint = "https://data.sec.gov/submissions/CIK{cik}.json"
    filing_url = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
    watched_forms = frozenset(("8-K", "10-Q", "10-K", "6-K", "20-F", "40-F"))

    def __init__(self, user_agent: str,
                 http: Optional[JsonHttpClient] = None):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT 는 연락 가능한 이메일을 포함해야 합니다")
        self.user_agent = user_agent
        self.http = http or JsonHttpClient()

    def filings(self, cik: str, symbol: str, *,
                since: Optional[date] = None,
                forms: Optional[Iterable[str]] = None) -> List[MarketEvent]:
        padded = str(cik).zfill(10)
        data = self.http.request(
            "GET", self.endpoint.format(cik=padded),
            headers={"User-Agent": self.user_agent},
        )
        recent = data.get("filings", {}).get("recent", {})
        wanted = set(forms or self.watched_forms)
        columns: Sequence[Sequence] = (
            recent.get("form", []), recent.get("filingDate", []),
            recent.get("accessionNumber", []), recent.get("primaryDocument", []),
            recent.get("primaryDocDescription", []),
        )
        n = min((len(c) for c in columns), default=0)
        events = []
        for i in range(n):
            form, filing_day, accession, document, description = (
                str(column[i] or "") for column in columns)
            if form not in wanted:
                continue
            day = date.fromisoformat(filing_day)
            if since and day < since:
                continue
            events.append(MarketEvent(
                source="sec-edgar", region="US",
                title=f"{form} {description}".strip(),
                url=self.filing_url.format(
                    cik=int(padded), accession=accession.replace("-", ""),
                    document=document),
                published_at=datetime.combine(day, datetime.min.time(),
                                               tzinfo=timezone.utc),
                symbol=symbol.upper(), company=str(data.get("name") or ""),
                event_type="filing", official=True, tags=[form],
            ))
        return events
