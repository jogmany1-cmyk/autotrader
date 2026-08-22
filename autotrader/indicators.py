"""순수 파이썬 기술적 지표 모음.

모든 함수는 "가장 오래된 값 → 가장 최신 값" 순서의 리스트를 받고,
입력과 같은 길이의 리스트를 돌려준다. 값을 계산할 수 없는 앞쪽 구간은
None 으로 채운다. 이 규칙 덕분에 인덱스 i 의 지표값은 언제나 i 시점까지의
정보만으로 만들어지며, 백테스트에서 미래 정보가 새어 들어가지 않는다.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

from .models import Bar

Num = Optional[float]


def closes(bars: Sequence[Bar]) -> List[float]:
    return [b.close for b in bars]


def sma(values: Sequence[float], period: int) -> List[Num]:
    if period <= 0:
        raise ValueError("period 는 1 이상이어야 합니다")
    out: List[Num] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else None)
    return out


def ema(values: Sequence[float], period: int) -> List[Num]:
    if period <= 0:
        raise ValueError("period 는 1 이상이어야 합니다")
    k = 2.0 / (period + 1)
    out: List[Num] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> List[Num]:
    """Wilder 방식 RSI."""
    out: List[Num] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        chg = values[i] - values[i - 1]
        gains += max(chg, 0.0)
        losses += max(-chg, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        chg = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(chg, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-chg, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(bars: Sequence[Bar]) -> List[Num]:
    out: List[Num] = [None] * len(bars)
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        out[i] = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> List[Num]:
    """Wilder 방식 ATR. 손절 폭과 포지션 사이징의 기준이 된다."""
    tr = true_range(bars)
    out: List[Num] = [None] * len(bars)
    if len(bars) <= period:
        return out
    window = [t for t in tr[1:period + 1] if t is not None]
    if len(window) < period:
        return out
    prev = sum(window) / period
    out[period] = prev
    for i in range(period + 1, len(bars)):
        t = tr[i] or 0.0
        prev = (prev * (period - 1) + t) / period
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int) -> List[Num]:
    out: List[Num] = [None] * len(values)
    for i in range(period - 1, len(values)):
        win = values[i - period + 1:i + 1]
        mean = sum(win) / period
        var = sum((v - mean) ** 2 for v in win) / (period - 1 if period > 1 else 1)
        out[i] = math.sqrt(var)
    return out


def returns(values: Sequence[float]) -> List[Num]:
    out: List[Num] = [None] * len(values)
    for i in range(1, len(values)):
        prev = values[i - 1]
        out[i] = (values[i] / prev - 1.0) if prev else None
    return out


def roc(values: Sequence[float], period: int) -> List[Num]:
    """period 봉 전 대비 수익률."""
    out: List[Num] = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        out[i] = (values[i] / base - 1.0) if base else None
    return out


def realized_vol(values: Sequence[float], period: int = 20, ann: int = 252) -> List[Num]:
    """연율화 변동성."""
    rets = returns(values)
    out: List[Num] = [None] * len(values)
    for i in range(period, len(values)):
        win = [r for r in rets[i - period + 1:i + 1] if r is not None]
        if len(win) < period:
            continue
        mean = sum(win) / len(win)
        var = sum((r - mean) ** 2 for r in win) / (len(win) - 1)
        out[i] = math.sqrt(var) * math.sqrt(ann)
    return out


def donchian(bars: Sequence[Bar], period: int = 20):
    """(상단, 하단). 당일 봉을 포함하지 않는 직전 period 구간의 고가/저가로,
    돌파 판정에 자기 자신이 섞이는 것을 막는다."""
    up: List[Num] = [None] * len(bars)
    dn: List[Num] = [None] * len(bars)
    for i in range(period, len(bars)):
        win = bars[i - period:i]
        up[i] = max(b.high for b in win)
        dn[i] = min(b.low for b in win)
    return up, dn


def rolling_max(values: Sequence[float], period: int) -> List[Num]:
    out: List[Num] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1:i + 1])
    return out


def drawdown_series(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    peak = float("-inf")
    for v in equity:
        peak = max(peak, v)
        out.append(v / peak - 1.0 if peak > 0 else 0.0)
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    dd = drawdown_series(equity)
    return min(dd) if dd else 0.0


def zscore(values: Sequence[Num]) -> List[Num]:
    """단면(cross-section) 표준화. 팩터 점수 합산 전에 스케일을 맞춘다."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return [0.0 if v is not None else None for v in values]
    mean = sum(clean) / len(clean)
    var = sum((v - mean) ** 2 for v in clean) / (len(clean) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return [0.0 if v is not None else None for v in values]
    return [((v - mean) / sd) if v is not None else None for v in values]


def clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def last_valid(seq: Sequence[Num]) -> Num:
    for v in reversed(seq):
        if v is not None:
            return v
    return None
