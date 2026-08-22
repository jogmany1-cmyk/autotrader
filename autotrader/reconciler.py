"""SourceReconciler — "두 번째 눈"으로 통합 데이터의 누락을 감지.

배경 (블로그 참고글 §6): 2025년 대체거래소 NXT 출범 이후, 서버 조건검색이
KRX 단독 시세만 보는 반면 HTS 통합 화면은 KRX+NXT 합산(_AL) 시세로 판정한다.
그 결과 프리·애프터 마켓에서 "보이는데 못 잡는" 종목이 생긴다.

우리는 이 문제를 데이터 소스 수준에서 잡는다. 두 개의 DataProvider — 하나는
KRX 단독(A), 하나는 KRX+NXT 통합(B) — 를 받아 같은 조건식을 각각 돌리고,
"B 에서는 잡히지만 A 에서는 안 잡히는" 종목을 통합 세력의 누수(leak)로
리포트한다. 실전에서는 이 리포트가 자체 판정 엔진의 입력이 된다.

이 모듈은 데이터 원천을 결합하거나 판정을 대체하지 않는다. 오직 대조만 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set

from .data.base import DataProvider


@dataclass
class ReconcileReport:
    only_in_primary: List[str] = field(default_factory=list)     # A ∖ B (합산이 놓친 것)
    only_in_secondary: List[str] = field(default_factory=list)   # B ∖ A (통합이 새로 잡은 것 — 누수 후보)
    in_both: List[str] = field(default_factory=list)             # A ∩ B
    primary_missing_data: List[str] = field(default_factory=list)  # A 조회 실패
    secondary_missing_data: List[str] = field(default_factory=list)  # B 조회 실패

    @property
    def leak_count(self) -> int:
        """통합 데이터에서만 잡히는 (=서버 조건에서 새어나간) 종목 수."""
        return len(self.only_in_secondary)

    def summary(self) -> str:
        return (f"both={len(self.in_both)}  "
                f"only_A={len(self.only_in_primary)}  "
                f"only_B={len(self.only_in_secondary)}  "
                f"A_miss={len(self.primary_missing_data)}  "
                f"B_miss={len(self.secondary_missing_data)}")


# 조건식은 "종목의 최근 봉 시퀀스" 를 받고 True/False 를 돌려주는 임의의 함수.
Predicate = Callable[["ProviderView"], bool]


@dataclass
class ProviderView:
    """조건식이 실제 시세 접근을 할 수 있게 감싸는 얇은 뷰."""
    symbol: str
    provider: DataProvider
    lookback: int = 60

    def bars(self):
        return self.provider.history(self.symbol, self.lookback)

    def last_close(self) -> float:
        return self.provider.last_price(self.symbol)


class SourceReconciler:
    """A(주)/B(부) 두 공급자에 같은 조건식을 적용해 결과 집합을 대조."""

    def __init__(self, primary: DataProvider, secondary: DataProvider,
                 lookback: int = 60):
        self.primary = primary
        self.secondary = secondary
        self.lookback = lookback

    def reconcile(self, symbols: Sequence[str], predicate: Predicate) -> ReconcileReport:
        r = ReconcileReport()
        set_a: Set[str] = set()
        set_b: Set[str] = set()
        for sym in symbols:
            hit_a = self._safe_apply(sym, self.primary, predicate)
            hit_b = self._safe_apply(sym, self.secondary, predicate)
            if hit_a is None:
                r.primary_missing_data.append(sym)
            elif hit_a:
                set_a.add(sym)
            if hit_b is None:
                r.secondary_missing_data.append(sym)
            elif hit_b:
                set_b.add(sym)
        r.in_both = sorted(set_a & set_b)
        r.only_in_primary = sorted(set_a - set_b)
        r.only_in_secondary = sorted(set_b - set_a)
        return r

    def _safe_apply(self, sym: str, provider: DataProvider,
                    predicate: Predicate) -> Optional[bool]:
        try:
            return bool(predicate(ProviderView(sym, provider, self.lookback)))
        except Exception:
            return None
