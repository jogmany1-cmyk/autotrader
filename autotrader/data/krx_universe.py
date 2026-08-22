"""KRX 과거 종목 유니버스 로더 — 생존자 편향 방어.

문제: 키움 REST API 는 "현재 상장된" 종목만 알려준다. 3년 전 상장폐지된
종목까지 포함해서 백테스트하려면 그 시점의 유니버스 스냅샷이 별도로 필요하다.
이걸 없이 하면 "지금 살아 있는 종목만으로 과거 성적을 보는" 생존자 편향에
빠져, 백테스트 수익률이 실제보다 훨씬 좋게 나온다.

해법: 각 시점의 KRX 상장 종목 리스트를 스냅샷 파일(JSONL)로 저장해 둔다.
`pykrx` 가 설치돼 있으면 자동으로 받고, 없으면 수동 스냅샷 파일을 읽는다.
스냅샷 포맷:
    {"date": "2024-01-02", "market": "KOSPI",  "symbols": ["005930", ...]}
    {"date": "2024-01-02", "market": "KOSDAQ", "symbols": ["...", ...]}

pykrx 는 옵션 의존성. 없으면 로더는 여전히 스냅샷 파일만으로 작동한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set


@dataclass
class UniverseSnapshot:
    date: date
    market: str          # "KOSPI" | "KOSDAQ" | "ALL"
    symbols: List[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps({
            "date": self.date.isoformat(),
            "market": self.market,
            "symbols": self.symbols,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "UniverseSnapshot":
        d = json.loads(line)
        return cls(
            date=date.fromisoformat(d["date"]),
            market=d.get("market", "ALL"),
            symbols=list(d.get("symbols", [])),
        )


class KrxUniverse:
    """과거 시점의 KRX 유니버스 스냅샷을 관리하는 저장소."""

    def __init__(self, snapshot_path: str):
        self.snapshot_path = snapshot_path
        self._by_date: Dict[str, Dict[str, List[str]]] = {}
        if os.path.exists(snapshot_path):
            self._load()

    # ------------------------------------------------------------ 저장/로드
    def _load(self) -> None:
        with open(self.snapshot_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                snap = UniverseSnapshot.from_json(line)
                self._by_date.setdefault(snap.date.isoformat(), {})[snap.market] = snap.symbols

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.snapshot_path) or ".", exist_ok=True)
        with open(self.snapshot_path, "w", encoding="utf-8") as fh:
            for date_str in sorted(self._by_date):
                for market, syms in sorted(self._by_date[date_str].items()):
                    fh.write(UniverseSnapshot(
                        date=date.fromisoformat(date_str),
                        market=market, symbols=list(syms),
                    ).as_json())
                    fh.write("\n")

    # ------------------------------------------------------------ 접근 API
    def add(self, snap: UniverseSnapshot) -> None:
        self._by_date.setdefault(snap.date.isoformat(), {})[snap.market] = list(snap.symbols)

    def snapshot_dates(self) -> List[date]:
        return sorted(date.fromisoformat(k) for k in self._by_date)

    def symbols_on(self, on: date, market: str = "ALL") -> List[str]:
        """가장 가까운 이전 스냅샷의 유니버스를 리턴. 없으면 빈 리스트."""
        target = on.isoformat()
        picked_key: Optional[str] = None
        for k in sorted(self._by_date):
            if k <= target:
                picked_key = k
            else:
                break
        if picked_key is None:
            return []
        entry = self._by_date[picked_key]
        if market == "ALL":
            out: Set[str] = set()
            for syms in entry.values():
                out.update(syms)
            return sorted(out)
        return list(entry.get(market, []))

    def union_between(self, start: date, end: date, market: str = "ALL") -> List[str]:
        """[start, end] 사이 어떤 시점에라도 상장돼 있던 종목의 합집합.
        생존자 편향 완화의 핵심."""
        out: Set[str] = set()
        for k in sorted(self._by_date):
            d = date.fromisoformat(k)
            if start <= d <= end:
                entry = self._by_date[k]
                if market == "ALL":
                    for syms in entry.values():
                        out.update(syms)
                else:
                    out.update(entry.get(market, []))
        return sorted(out)

    # -------------------------------------------------- pykrx 자동 수집 (옵션)
    def refresh_from_pykrx(self, dates: Iterable[date]) -> int:
        """pykrx 가 있으면 각 날짜의 KOSPI·KOSDAQ 종목목록을 받아 추가.
        리턴: 추가된 (날짜×시장) 스냅샷 수."""
        try:
            from pykrx import stock  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "pykrx 패키지가 필요합니다: pip install pykrx"
            ) from exc
        added = 0
        for d in dates:
            key = d.strftime("%Y%m%d")
            for market, code in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
                try:
                    syms = stock.get_market_ticker_list(key, market=code)
                except Exception:
                    continue
                self.add(UniverseSnapshot(d, market, list(syms)))
                added += 1
        return added
