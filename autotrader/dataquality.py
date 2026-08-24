"""데이터 무결성 검증 — 실데이터가 백테스트로 흘러 들어가기 전의 관문.

CLAUDE.md 승격 경로 2단계("데이터 무결성")를 코드 게이트로 만든 모듈이다.
지금까지 파이프라인에는 "이 시세가 믿을 만한가" 를 묻는 자리가 아예 없었다.
`CsvProvider` 의 실제 동작을 보면 왜 위험한지 분명하다.

* 숫자 칸이 깨진 행은 **조용히 건너뛴다** (합계행·주석행을 넘기려던 의도).
  원본 500행이 480봉으로 줄어도 백테스트는 아무 불평 없이 돌아간다.
* 날짜 칸이 깨진 행은 반대로 **파일 전체를 실패**시킨다 (`DataError`).
* 읽은 뒤 날짜순으로 **정렬해 버리므로** 원본이 뒤섞여 있어도 흔적이 남지 않는다.

그 침묵이 "합성 데이터에서는 잘 되던 전략이 실전에서 이상하게 동작하는" 사고의
씨앗이다. 이 모듈은 그 침묵을 깨는 것만 한다 — 데이터를 고치지는 않는다.

설계 원칙
---------
* **공급자 무관.** 검사 대상은 `List[Bar]` 다. CSV·키움 캐시·합성 무엇이든
  `DataProvider` 를 통해 들어오면 똑같이 검사된다 (파이프라인의 seam 유지).
* **ERROR 와 WARN 을 구분한다.**
  - `ERROR` = 데이터 자체가 모순이다. 고치거나 다시 받기 전에는 쓰면 안 된다.
    (OHLC 논리 위반, 중복 날짜, 0 이하 가격, 미래 날짜, 역순, 유실된 행)
  - `WARN` = 값 자체는 성립하지만 백테스트를 왜곡할 수 있다.
    (휴장일 대비 결측, 액면분할 의심 점프, 거래량 0, 정지봉, 짧은 이력, 캐시 정체)
* **거짓 경보를 내지 않는다.** `market.KRX_HOLIDAYS` 는 2024~2027 만 담고 있어
  그 밖 구간에서 결측 거래일을 판정하면 공휴일이 전부 결측으로 잡힌다. 그래서
  표가 커버하지 않는 구간은 결측 검사를 **건너뛰고 그 사실을 알린다.**
* **같은 결함을 반복해 외치지 않는다.** 봉마다 걸리는 검사는 종목·코드 단위로
  묶어 건수와 표본 날짜만 보고한다. 깨진 파일 하나가 수천 줄을 쏟아내면 아무도
  리포트를 읽지 않기 때문이다.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .data.base import DataError, DataProvider
from .market import KRX_HOLIDAYS, is_trading_day
from .models import Bar

ERROR = "ERROR"
WARN = "WARN"

# 휴장일 표가 실제로 커버하는 연도. 이 밖에서는 결측 거래일 판정을 하지 않는다.
_CALENDAR_YEARS = {d.year for d in KRX_HOLIDAYS}

# 액면분할·병합에서 흔한 비율. 종가가 이 비율 근처로 튀면 미조정 주가를 의심한다.
_SPLIT_RATIOS: Tuple[float, ...] = (
    1 / 100, 1 / 50, 1 / 20, 1 / 10, 1 / 5, 1 / 4, 1 / 3, 1 / 2,
    2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0,
)

# 리포트 한 줄에 붙이는 표본 날짜 최대 개수.
_MAX_SAMPLES = 3

# 결측 거래일은 날짜 자체가 조치 대상이라 더 넉넉히 나열한다.
_MAX_MISSING_LISTED = 12


@dataclass
class QualityLimits:
    """검사 기준값. 전부 CLI 로 덮어쓸 수 있다."""
    min_bars: int = 200          # SwingTrend 가 200봉 정배열을 보므로 그 아래는 검증 불가
    jump_pct: float = 0.30       # KRX 일일 가격제한폭이 ±30% — 넘으면 분할이나 오류
    split_tolerance: float = 0.03
    max_zero_volume_ratio: float = 0.10
    max_flat_ratio: float = 0.10
    long_gap_days: int = 5       # 이보다 긴 연속 결측은 개별로 지목한다
    stale_days: int = 5          # 마지막 봉이 이보다 오래되면 캐시 정체


@dataclass(frozen=True)
class Issue:
    """결함 하나. 봉마다 걸리는 검사는 종목·코드 단위로 합쳐져 `count>1` 이 된다."""
    symbol: str
    code: str
    severity: str
    detail: str
    when: Optional[date] = None          # 첫 발생일
    count: int = 1
    samples: Tuple[date, ...] = ()       # 앞쪽 표본 날짜

    def as_line(self) -> str:
        stamp = self.when.isoformat() if self.when else "-"
        head = f"[{self.severity}] {self.symbol:<10} {self.code:<22} {stamp}"
        if self.count > 1:
            return f"{head}  {self.count}건 — {self.detail}{_sample_suffix(self.samples, self.count)}"
        return f"{head}  {self.detail}"

    def as_dict(self) -> Dict:
        return {"symbol": self.symbol, "code": self.code, "severity": self.severity,
                "detail": self.detail, "count": self.count,
                "when": self.when.isoformat() if self.when else None,
                "samples": [d.isoformat() for d in self.samples]}


@dataclass
class SymbolReport:
    symbol: str
    n_bars: int = 0
    first_day: Optional[date] = None
    last_day: Optional[date] = None
    issues: List[Issue] = field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == WARN]

    @property
    def ok(self) -> bool:
        return not self.issues

    def has(self, code: str) -> bool:
        return any(i.code == code for i in self.issues)

    def as_dict(self) -> Dict:
        return {"symbol": self.symbol, "n_bars": self.n_bars,
                "first_day": self.first_day.isoformat() if self.first_day else None,
                "last_day": self.last_day.isoformat() if self.last_day else None,
                "issues": [i.as_dict() for i in self.issues]}


@dataclass
class QualityReport:
    as_of: date
    symbols: List[SymbolReport] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)  # (symbol, 사유)

    @property
    def all_issues(self) -> List[Issue]:
        return [i for s in self.symbols for i in s.issues]

    @property
    def n_errors(self) -> int:
        """ERROR 로 분류된 결함 **건수** (묶인 항목은 실제 발생 횟수로 센다)."""
        return sum(i.count for i in self.all_issues if i.severity == ERROR)

    @property
    def n_warnings(self) -> int:
        return sum(i.count for i in self.all_issues if i.severity == WARN)

    @property
    def clean_symbols(self) -> List[str]:
        return [s.symbol for s in self.symbols if s.ok]

    @property
    def error_symbols(self) -> List[str]:
        return [s.symbol for s in self.symbols if s.errors]

    def counts_by_code(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for i in self.all_issues:
            out[i.code] = out.get(i.code, 0) + i.count
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def passed(self, strict: bool = False) -> bool:
        """게이트 판정. 기본은 ERROR 무결, --strict 면 WARN 도 불허."""
        if self.unreadable or self.n_errors:
            return False
        return not (strict and self.n_warnings)

    def summary(self) -> str:
        return (f"symbols={len(self.symbols)}  clean={len(self.clean_symbols)}  "
                f"errors={self.n_errors}({len(self.error_symbols)}종목)  "
                f"warnings={self.n_warnings}  unreadable={len(self.unreadable)}")

    def as_dict(self) -> Dict:
        return {"as_of": self.as_of.isoformat(), "summary": self.summary(),
                "passed": self.passed(), "passed_strict": self.passed(strict=True),
                "counts_by_code": self.counts_by_code(),
                "unreadable": [{"symbol": s, "reason": r} for s, r in self.unreadable],
                "symbols": [s.as_dict() for s in self.symbols]}


class DataQualityChecker:
    """봉 시퀀스를 규칙에 걸어 보는 검사기.

    상태를 갖지 않으므로 같은 인스턴스를 유니버스 전체에 재사용해도 된다.
    """

    def __init__(self, limits: Optional[QualityLimits] = None,
                 as_of: Optional[date] = None):
        self.limits = limits or QualityLimits()
        self.as_of = as_of or date.today()

    # ---- 공개 API ------------------------------------------------------

    def check_bars(self, symbol: str, bars: Sequence[Bar]) -> SymbolReport:
        rep = SymbolReport(symbol=symbol, n_bars=len(bars))
        if not bars:
            rep.issues.append(Issue(symbol, "empty", ERROR, "봉이 하나도 없음"))
            return rep
        rep.first_day = bars[0].day
        rep.last_day = bars[-1].day
        raw: List[Issue] = []
        self._check_each_bar(symbol, raw, bars)
        self._check_sequence(symbol, raw, bars)
        self._check_gaps(symbol, raw, bars)
        self._check_returns(symbol, raw, bars)
        self._check_aggregates(symbol, raw, bars)
        rep.issues = _collapse(raw)
        return rep

    def check_provider(self, provider: DataProvider,
                       symbols: Optional[Sequence[str]] = None,
                       limit: int = 0) -> QualityReport:
        """`limit` 은 종목당 검사할 최근 봉 수. 0 이면 전체 이력.

        백테스트가 최근 N봉만 쓰는데 검사는 40년치 전부를 보면, 쓰지도 않을
        1985년 봉 하나 때문에 FAIL 이 뜬다. 그러면 게이트를 무시하는 습관이
        생긴다 — 게이트가 있으나 마나가 되는 가장 흔한 경로다. 그래서 실제로
        쓸 구간만 검사할 수 있게 한다 (`backtest --bars` 와 같은 값을 준다).
        """
        rep = QualityReport(as_of=self.as_of)
        for sym in (symbols if symbols is not None else provider.universe()):
            try:
                bars = provider.history(sym, limit=limit)
            except DataError as exc:
                rep.unreadable.append((sym, str(exc)))
                continue
            rep.symbols.append(self.check_bars(sym, bars))
        return rep

    def check_csv_dir(self, directory: str,
                      symbols: Optional[Sequence[str]] = None,
                      limit: int = 0) -> QualityReport:
        """CSV 디렉터리 검사 + 원본 행 수 대조.

        `CsvProvider` 가 조용히 버린 행을 드러내려고 원본 행 수를 따로 센다.
        이 대조가 없으면 "파일에는 500행인데 백테스트는 480봉으로 돌았다" 를
        아무도 눈치채지 못한다.
        """
        from .data.csv_provider import CsvProvider

        provider = CsvProvider(directory)
        rep = self.check_provider(provider, symbols, limit=limit)
        if limit:
            # 구간을 잘라 검사할 때는 원본 행 수 대조를 하지 않는다. 파일에는
            # 10,914행이 있는데 2,500봉만 실었으니 "8,414행이 유실됐다" 는
            # 거짓 경보가 난다. 유실 검사는 전체를 볼 때만 의미가 있다.
            return rep
        for sym_rep in rep.symbols:
            path = os.path.join(directory, f"{sym_rep.symbol}.csv")
            raw = _count_raw_rows(path)
            if raw is None or raw <= sym_rep.n_bars:
                continue
            sym_rep.issues.append(Issue(
                sym_rep.symbol, "dropped_rows", ERROR,
                f"원본 {raw}행 중 {raw - sym_rep.n_bars}행이 파싱되지 못하고 버려짐 "
                f"(적재 {sym_rep.n_bars}봉)", count=raw - sym_rep.n_bars))
        return rep

    # ---- 개별 검사 -----------------------------------------------------

    def _check_each_bar(self, sym: str, out: List[Issue], bars: Sequence[Bar]) -> None:
        for b in bars:
            prices = (b.open, b.high, b.low, b.close)
            if any(p <= 0 for p in prices):
                out.append(Issue(sym, "nonpositive_price", ERROR,
                                 f"O/H/L/C={prices}", b.day))
                continue  # 가격이 0 이하면 아래 OHLC 논리 검사는 의미가 없다
            if b.high < b.low:
                out.append(Issue(sym, "ohlc_violation", ERROR,
                                 f"high({b.high:g}) < low({b.low:g})", b.day))
            elif b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
                out.append(Issue(sym, "ohlc_violation", ERROR,
                                 f"고저 범위 밖의 시/종가 O={b.open:g} H={b.high:g} "
                                 f"L={b.low:g} C={b.close:g}", b.day))
            if b.volume < 0:
                out.append(Issue(sym, "negative_volume", ERROR,
                                 f"volume={b.volume:g}", b.day))
            if b.day > self.as_of:
                out.append(Issue(sym, "future_bar", ERROR,
                                 f"기준일({self.as_of}) 이후의 봉", b.day))

    def _check_sequence(self, sym: str, out: List[Issue], bars: Sequence[Bar]) -> None:
        seen: Dict[date, int] = {}
        for idx, b in enumerate(bars):
            seen[b.day] = seen.get(b.day, 0) + 1
            if idx and b.ts < bars[idx - 1].ts:
                out.append(Issue(sym, "unsorted", ERROR,
                                 "앞 봉보다 이른 시각의 봉 (원본이 뒤섞여 있음)", b.day))
        for day, n in sorted(seen.items()):
            if n > 1:
                out.append(Issue(sym, "duplicate_date", ERROR,
                                 f"같은 날짜가 {n}번 등장", day, count=n - 1))
        for b in bars:
            if b.day.year in _CALENDAR_YEARS and not is_trading_day(b.day):
                out.append(Issue(sym, "bar_on_closed_day", WARN,
                                 "휴장일로 등재된 날에 거래가 있음 — "
                                 "market.KRX_HOLIDAYS 가 틀렸을 수 있다", b.day))

    def _check_gaps(self, sym: str, out: List[Issue], bars: Sequence[Bar]) -> None:
        """휴장일 표가 커버하는 구간에서만 결측 거래일을 센다."""
        days = sorted({b.day for b in bars})
        checkable = [d for d in days if d.year in _CALENDAR_YEARS]
        if len(days) != len(checkable):
            out.append(Issue(
                sym, "calendar_uncovered", WARN,
                f"휴장일 표({min(_CALENDAR_YEARS)}~{max(_CALENDAR_YEARS)}) 밖의 "
                f"{len(days) - len(checkable)}봉은 결측 검사 생략"))
        if len(checkable) < 2:
            return
        missing_days: List[date] = []
        long_gaps: List[Tuple[date, int]] = []
        for prev, cur in zip(checkable, checkable[1:]):
            if (cur - prev).days <= 1:
                continue
            missing = [prev + timedelta(days=k) for k in range(1, (cur - prev).days)]
            missing = [d for d in missing if is_trading_day(d)]
            if not missing:
                continue
            missing_days.extend(missing)
            if len(missing) >= self.limits.long_gap_days:
                long_gaps.append((prev, len(missing)))
        if missing_days:
            # 개수만 알려주면 고칠 수가 없다. 실제 날짜를 보고한다 — 이 목록이
            # 곧 "휴장일 표가 틀렸을 수 있는 날" 이라서 바로 대조할 수 있다.
            shown = ", ".join(d.isoformat() for d in missing_days[:_MAX_MISSING_LISTED])
            more = ("" if len(missing_days) <= _MAX_MISSING_LISTED
                    else f" 외 {len(missing_days) - _MAX_MISSING_LISTED}일")
            out.append(Issue(
                sym, "missing_trading_days", WARN,
                f"거래일인데 봉이 없는 날 {len(missing_days)}일: {shown}{more}",
                missing_days[0], count=len(missing_days),
                samples=tuple(missing_days[:_MAX_SAMPLES])))
        for start, n in long_gaps:
            out.append(Issue(sym, "long_gap", WARN,
                             f"{n}거래일 연속 결측 (거래정지 의심)", start))

    def _check_returns(self, sym: str, out: List[Issue], bars: Sequence[Bar]) -> None:
        lim = self.limits
        for prev, cur in zip(bars, bars[1:]):
            if prev.close <= 0 or cur.close <= 0:
                continue
            ratio = cur.close / prev.close
            if abs(ratio - 1.0) < lim.jump_pct:
                continue
            near = _nearest_split_ratio(ratio, lim.split_tolerance)
            if near is not None:
                out.append(Issue(
                    sym, "split_suspect", WARN,
                    f"종가 {prev.close:g} → {cur.close:g} (×{ratio:.4g}, "
                    f"{_ratio_label(near)} 부근) — 액면분할 미조정 의심", cur.day))
            else:
                out.append(Issue(
                    sym, "price_jump", WARN,
                    f"종가 {prev.close:g} → {cur.close:g} "
                    f"({(ratio - 1) * 100:+.1f}%, 가격제한폭 초과)", cur.day))

    def _check_aggregates(self, sym: str, out: List[Issue], bars: Sequence[Bar]) -> None:
        lim = self.limits
        n = len(bars)
        if n < lim.min_bars:
            out.append(Issue(sym, "short_history", WARN,
                             f"{n}봉 — 최소 {lim.min_bars}봉 미만이라 장기 전략 검증 불가"))
        zero_vol = sum(1 for b in bars if b.volume == 0)
        if zero_vol and zero_vol / n > lim.max_zero_volume_ratio:
            out.append(Issue(sym, "zero_volume", WARN,
                             f"거래량 0인 봉 {zero_vol}/{n} ({zero_vol / n:.1%}) "
                             f"— 결손이거나 유동성 없음"))
        flat = sum(1 for b in bars if b.open == b.high == b.low == b.close)
        if flat and flat / n > lim.max_flat_ratio:
            out.append(Issue(sym, "flat_bars", WARN,
                             f"O=H=L=C 인 봉 {flat}/{n} ({flat / n:.1%}) "
                             f"— 거래정지·상한가 잠김·복제된 값 의심"))
        stale = _trading_days_between(bars[-1].day, self.as_of)
        if stale > lim.stale_days:
            out.append(Issue(sym, "stale_data", WARN,
                             f"마지막 봉이 {stale}거래일 전({bars[-1].day}) "
                             f"— 수집이 멈춰 있음", bars[-1].day))


# ---- 보조 함수 ---------------------------------------------------------

def _collapse(issues: Sequence[Issue]) -> List[Issue]:
    """같은 (코드, 심각도) 결함을 하나로 묶는다. 첫 발생 순서는 유지."""
    order: List[Tuple[str, str]] = []
    groups: Dict[Tuple[str, str], List[Issue]] = {}
    for i in issues:
        key = (i.code, i.severity)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    out: List[Issue] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(group[0])
            continue
        first = group[0]
        samples = tuple(i.when for i in group[:_MAX_SAMPLES] if i.when is not None)
        out.append(Issue(first.symbol, first.code, first.severity, first.detail,
                         first.when, count=sum(i.count for i in group), samples=samples))
    return out


def _sample_suffix(samples: Sequence[date], count: int) -> str:
    if not samples:
        return ""
    tail = ", …" if count > len(samples) else ""
    return " (예: " + ", ".join(d.isoformat() for d in samples) + tail + ")"


def _nearest_split_ratio(ratio: float, tolerance: float) -> Optional[float]:
    for cand in _SPLIT_RATIOS:
        if abs(ratio - cand) <= cand * tolerance:
            return cand
    return None


def _ratio_label(cand: float) -> str:
    if cand < 1:
        return f"1/{round(1 / cand)} 분할"
    return f"{round(cand)}:1 병합"


def _trading_days_between(start: date, end: date) -> int:
    """start(제외) ~ end(포함) 사이의 거래일 수. end 가 더 이르면 0."""
    if end <= start:
        return 0
    n = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def _count_raw_rows(path: str) -> Optional[int]:
    """헤더를 뺀 비어 있지 않은 데이터 행 수. 읽을 수 없으면 None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            try:
                next(reader)
            except StopIteration:
                return 0
            return sum(1 for row in reader if any(cell.strip() for cell in row))
    except OSError:
        return None
