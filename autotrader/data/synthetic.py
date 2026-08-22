"""결정론적 합성 시세 생성기.

네트워크나 유료 데이터 없이도 전체 파이프라인(스크리닝 → 신호 → 리스크 →
체결 → 성과지표)을 끝까지 돌려보고 테스트할 수 있게 한다. 시드가 같으면
언제나 같은 시세가 나오므로 테스트가 흔들리지 않는다.

주의: 여기서 나온 백테스트 성적은 "코드가 도는지"에 대한 증거일 뿐,
전략이 돈을 번다는 증거가 전혀 아니다.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from ..models import Bar
from .base import DataError, DataProvider


def generate_bars(
    symbol: str,
    n: int = 750,
    start: Optional[datetime] = None,
    seed: int = 0,
    start_price: float = 50_000.0,
    annual_drift: float = 0.08,
    annual_vol: float = 0.30,
    regime_strength: float = 0.6,
) -> List[Bar]:
    """기하 브라운 운동 + 완만한 국면(regime) 변동으로 일봉을 만든다."""
    rng = random.Random(f"{symbol}:{seed}")
    start = start or datetime(2021, 1, 4)
    dt = 1.0 / 252.0
    price = start_price
    bars: List[Bar] = []
    ts = start
    # 국면: 저주파 사인파로 추세 구간과 횡보 구간이 번갈아 나타나게 한다.
    phase = rng.uniform(0, 2 * math.pi)
    period = rng.uniform(90, 220)
    for i in range(n):
        while ts.weekday() >= 5:  # 주말 건너뛰기
            ts += timedelta(days=1)
        regime = math.sin(2 * math.pi * i / period + phase)
        mu = annual_drift + regime_strength * regime * annual_vol
        shock = rng.gauss(0.0, 1.0)
        ret = (mu - 0.5 * annual_vol ** 2) * dt + annual_vol * math.sqrt(dt) * shock
        new_price = max(price * math.exp(ret), 1.0)
        o = price
        c = new_price
        span = abs(c - o) + price * annual_vol * math.sqrt(dt) * abs(rng.gauss(0, 0.7))
        h = max(o, c) + span * rng.uniform(0.0, 0.6)
        l = max(min(o, c) - span * rng.uniform(0.0, 0.6), 0.5)
        vol = max(1000.0, rng.gauss(300_000, 80_000)) * (1 + abs(ret) * 20)
        bars.append(Bar(ts=ts, open=round(o, 2), high=round(h, 2), low=round(l, 2),
                        close=round(c, 2), volume=round(vol)))
        price = new_price
        ts += timedelta(days=1)
    return bars


class SyntheticProvider(DataProvider):
    """데모/테스트용 공급자."""

    def __init__(
        self,
        symbols: Sequence[str] = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"),
        n: int = 750,
        seed: int = 7,
        start: Optional[datetime] = None,
    ):
        self._symbols = list(symbols)
        self._n = n
        self._seed = seed
        self._start = start or datetime(2021, 1, 4)
        self._cache: Dict[str, List[Bar]] = {}

    def universe(self) -> List[str]:
        return list(self._symbols)

    def history(self, symbol: str, limit: int = 500) -> List[Bar]:
        if symbol not in self._symbols:
            raise DataError(f"{symbol}: 합성 유니버스에 없는 종목")
        if symbol not in self._cache:
            idx = self._symbols.index(symbol)
            self._cache[symbol] = generate_bars(
                symbol,
                n=self._n,
                start=self._start,
                seed=self._seed + idx,
                start_price=10_000.0 * (1 + idx),
                annual_drift=0.02 + 0.04 * idx,
                annual_vol=0.20 + 0.04 * (idx % 4),
            )
        bars = self._cache[symbol]
        return bars[-limit:] if limit else bars
