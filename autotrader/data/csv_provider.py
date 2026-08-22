"""디렉터리에 있는 CSV 파일을 읽는 공급자.

`{dir}/{SYMBOL}.csv` 형식이며 헤더는 date,open,high,low,close,volume 를 기대한다
(대소문자·공백 무시, `timestamp`/`시가` 같은 흔한 별칭도 허용).
증권사에서 내려받은 과거 데이터로 바로 백테스트할 때 쓴다.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Dict, List, Optional

from ..models import Bar
from .base import DataError, DataProvider

_ALIASES = {
    "date": "date", "datetime": "date", "timestamp": "date", "time": "date", "일자": "date", "날짜": "date",
    "open": "open", "시가": "open",
    "high": "high", "고가": "high",
    "low": "low", "저가": "low",
    "close": "close", "adj close": "close", "종가": "close", "현재가": "close",
    "volume": "volume", "거래량": "volume",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def _parse_date(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise DataError(f"날짜 형식을 알 수 없습니다: {raw!r}")


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "").replace("　", "").strip())


class CsvProvider(DataProvider):
    def __init__(self, directory: str):
        self.directory = directory
        self._cache: Dict[str, List[Bar]] = {}

    def universe(self) -> List[str]:
        if not os.path.isdir(self.directory):
            return []
        return sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(self.directory)
            if f.lower().endswith(".csv")
        )

    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        bars = self._cache.get(symbol)
        if bars is None:
            bars = self._load(symbol)
            self._cache[symbol] = bars
        return bars[-limit:] if limit else bars

    def _load(self, symbol: str) -> List[Bar]:
        path = os.path.join(self.directory, f"{symbol}.csv")
        if not os.path.exists(path):
            raise DataError(f"파일이 없습니다: {path}")
        rows: List[Bar] = []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise DataError(f"{path}: 헤더가 없습니다")
            mapping = {}
            for name in reader.fieldnames:
                key = _ALIASES.get(name.strip().lower())
                if key and key not in mapping:
                    mapping[key] = name
            missing = {"date", "open", "high", "low", "close"} - set(mapping)
            if missing:
                raise DataError(f"{path}: 필수 컬럼 누락 {sorted(missing)}")
            vol_col: Optional[str] = mapping.get("volume")
            for row in reader:
                try:
                    rows.append(Bar(
                        ts=_parse_date(row[mapping["date"]]),
                        open=_to_float(row[mapping["open"]]),
                        high=_to_float(row[mapping["high"]]),
                        low=_to_float(row[mapping["low"]]),
                        close=_to_float(row[mapping["close"]]),
                        volume=_to_float(row[vol_col]) if vol_col and row.get(vol_col) else 0.0,
                    ))
                except (ValueError, KeyError):
                    continue  # 합계행·주석행 등은 조용히 건너뛴다
        rows.sort(key=lambda b: b.ts)
        if not rows:
            raise DataError(f"{path}: 읽을 수 있는 데이터가 없습니다")
        return rows
