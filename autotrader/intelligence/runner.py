"""환경변수와 JSON 관심목록으로 아침 정보 파이프라인을 조립한다."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ..market import now_kst
from ..notify import ConsoleChannel, Notifier
from .kakao import KakaoMemoChannel
from .pipeline import BriefingReport, Collector, MorningIntelligencePipeline
from .providers import NaverNewsProvider, OpenDartProvider, SecEdgarProvider
from .store import IntelligenceStore


def load_watchlist(path: str | Path) -> Dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError("관심목록 JSON의 최상위 값은 객체여야 합니다")
    return value


def build_collectors(config: Dict, *, now: datetime
                     ) -> tuple[List[tuple[str, Collector]], List[str]]:
    collectors: List[tuple[str, Collector]] = []
    disabled = []

    naver_id = os.getenv("NAVER_CLIENT_ID", "")
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "")
    if naver_id and naver_secret:
        naver = NaverNewsProvider(naver_id, naver_secret)
        for index, item in enumerate(config.get("naver_queries", []), 1):
            query = str(item.get("query") or "").strip()
            if not query:
                continue
            collectors.append((
                f"naver-{index}",
                lambda item=dict(item), query=query: naver.search(
                    query, symbol=str(item.get("symbol") or ""),
                    company=str(item.get("company") or ""),
                    display=int(item.get("display") or 20)),
            ))
    else:
        disabled.append("naver")

    dart_key = os.getenv("OPENDART_API_KEY", "")
    if dart_key:
        dart = OpenDartProvider(dart_key)
        days = max(int(config.get("dart_days", 1)), 1)
        begin = now.date() - timedelta(days=days)
        end = now.date()
        mapping = dict(config.get("dart_corp_to_symbol", {}))
        collectors.append(("opendart", lambda: dart.disclosures(
            begin, end, corp_to_symbol=mapping)))
    else:
        disabled.append("opendart")

    sec_agent = os.getenv("SEC_USER_AGENT", "")
    if sec_agent:
        sec = SecEdgarProvider(sec_agent)
        days = max(int(config.get("sec_days", 2)), 1)
        since = now.date() - timedelta(days=days)
        for item in config.get("us_watchlist", []):
            symbol = str(item.get("symbol") or "").upper()
            cik = str(item.get("cik") or "")
            if not symbol or not cik:
                continue
            collectors.append((
                f"sec-{symbol}",
                lambda cik=cik, symbol=symbol: sec.filings(
                    cik, symbol, since=since),
            ))
    else:
        disabled.append("sec-edgar")
    return collectors, disabled


def run_morning_briefing(config_path: str | Path, *,
                         output_dir: str | Path = "data/intelligence",
                         send_kakao: bool = True,
                         now: Optional[datetime] = None) -> BriefingReport:
    now = now or now_kst()
    config = load_watchlist(config_path)
    collectors, disabled = build_collectors(config, now=now)
    channels = [ConsoleChannel()]
    if send_kakao:
        channels.append(KakaoMemoChannel.from_env())
    notifier = Notifier(channels)
    pipeline = MorningIntelligencePipeline(
        collectors, store=IntelligenceStore(output_dir))
    report = pipeline.run(
        now=now, holdings=list(config.get("holdings", [])),
        baseline_buy_symbols=list(config.get("baseline_buy_symbols", [])),
    )
    if disabled:
        report.body += "\n\n비활성 공급자(환경변수 없음): " + ", ".join(disabled)
    notifier.info("아침 시장 요약", report.body)
    return report
