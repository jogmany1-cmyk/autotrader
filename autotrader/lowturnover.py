"""저회전 횡단면 포트폴리오 — `docs/LOW-TURNOVER-SPEC.md` 구현.

기존 5종 앙상블과 근본적으로 다른 물건이라 `Strategy` 를 상속하지 않는다.
`Strategy.evaluate(ctx)` 는 **한 종목을 한 시점에** 판단하는 인터페이스인데,
이쪽은 **모든 종목을 한 시점에 줄 세워** 상위 N 개를 고르는 횡단면 문제다.
억지로 같은 인터페이스에 넣으면 "종목별 점수"를 낸 뒤 앙상블이 임계값으로
자르는 구조가 되어, 정확히 폐기한 틀로 되돌아간다.

무엇이 달라졌는가 (규격 §1)
---------------------------
회전율이 전부다. 폐기한 전략들의 거래당 순수 우위는 -27.9bp ~ +8.0bp 였고
왕복비용은 33~103bp 였다. 그 격차는 **지표를 바꿔서 메울 수 없다** — 거래
횟수를 줄여 비용을 분산시키는 수밖에 없다.

  연 회전율 2배  → 왕복 1회  → 연 비용 0.4%   (이 규격)
  연 회전율 54배 → 왕복 27회 → 연 비용 10.8%  (폐기한 day_momentum)

Novy-Marx & Velikov (RFS 29(1), 2016): **월 편도 회전율 50% 미만인 아노말리만
비용 차감 후 유의한 순수익**을 낸다. 그리고 그들이 측정한 가장 효과적인 비용
절감 장치가 **밴딩(buy/hold spread)** 이다 — 편입 기준을 편출 기준보다 엄격하게
두어, 순위가 조금 흔들릴 때마다 갈아타지 않게 만든다.

1차 구현 범위 (규격 §8)
-----------------------
가치(PBR) 팩터는 **넣지 않았다**. `DataProvider` 가 OHLCV 만 제공하고 재무
데이터 경로가 아직 없기 때문이다. 없는 데이터를 있는 척하는 대신 저변동성
단독으로 시작하고, 재무 데이터가 확보되면 별도 규격으로 추가한다.

저변동성을 고른 근거는 한국 고유 증거가 가장 강해서다 — KOSDAQ 에서 KOSPI
보다 강하고, 규모 효과의 위장이 아님이 확인됐다 (Sustainability 11(13):3654).
모멘텀은 의도적으로 제외했다: Chui·Titman·Wei (JF 2010) 에서 **한국·중국·일본
모두 유의하지 않았고**, 한국 문헌 다수가 오히려 역투자가 맞다고 보고한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import indicators as ind
from .models import Bar

# ---- 규격 상수 (결과를 보기 전에 고정. docs/LOW-TURNOVER-SPEC.md §3) --------
VOL_WINDOW = 60              # 실현변동성 측정 창(거래일)
TARGET_HOLDINGS = 25         # 동일가중 보유 종목 수
ENTRY_RANK = 25              # 신규 편입 허용 순위 (이내)
EXIT_RANK = 40               # 기존 보유 유지 순위 (밖으로 밀려나야 편출)
MIN_PRICE = 1_000.0          # 호가 상대틱이 지배적인 저가 구간 배제
MIN_DOLLAR_VOL = 5e8         # 60일 일평균 거래대금 하한
MIN_LISTED_BARS = 250        # 지표 계산에 필요한 최소 상장 경과

#: 리밸런싱 월 (해당 월 최종 거래일 기준 → 다음 거래일 시가 체결)
REBALANCE_MONTHS = (6, 12)

#: 판정 기준 (규격 §5). 일봉 전략과 다른 기준을 쓰는 이유는 그 문서에.
MAX_ANNUAL_TURNOVER = 2.0
MIN_TOTAL_TRADES = 40
MAX_DRAWDOWN = -0.35


@dataclass(frozen=True)
class Candidate:
    """한 리밸런싱 시점의 종목 하나."""
    symbol: str
    price: float
    volatility: float          # 연환산 실현변동성
    dollar_volume: float       # 60일 일평균 거래대금
    rank: int = 0              # 1 = 가장 낮은 변동성

    @property
    def score(self) -> float:
        """리포트·디버깅용. 선택은 `rank` 로 한다 — 점수 값 자체는 쓰지 않는다.

        폐기한 전략에서 모든 후보 점수가 0.95 로 포화됐던 일이 있다. 순위로
        고르면 그런 포화가 선택에 영향을 주지 못한다.
        """
        return 1.0 / (1.0 + self.volatility) if self.volatility > 0 else 0.0


def _avg_dollar_volume(bars: Sequence[Bar], at: int, window: int) -> float:
    lo = max(0, at - window + 1)
    seg = bars[lo:at + 1]
    if not seg:
        return 0.0
    return sum(b.close * b.volume for b in seg) / len(seg)


def measure(symbol: str, bars: Sequence[Bar], at: int,
            vol_window: int = VOL_WINDOW) -> Optional[Candidate]:
    """이 시점 이 종목의 특성. 자격 미달이면 None.

    `at` 이 경계다 — `bars[at]` 까지만 본다. 이후 봉을 보면 미래정보 누출이다.
    """
    if at < 0 or at >= len(bars):
        return None
    if at + 1 < MIN_LISTED_BARS:
        return None
    price = bars[at].close
    if price < MIN_PRICE:
        return None
    dv = _avg_dollar_volume(bars, at, vol_window)
    if dv < MIN_DOLLAR_VOL:
        return None
    series = ind.realized_vol(ind.closes(bars[:at + 1]), period=vol_window)
    vol = series[at] if at < len(series) else None
    if vol is None or vol <= 0:
        return None
    return Candidate(symbol=symbol, price=price, volatility=float(vol),
                     dollar_volume=dv)


def rank_candidates(candidates: Sequence[Candidate]) -> List[Candidate]:
    """변동성 오름차순. 동점은 종목코드 오름차순 — 입력 순서가 결과를 바꾸면
    안 된다 (같은 데이터를 순서만 바꿔 넣어도 성과가 달라지는 버그를 실제로
    겪었다)."""
    ordered = sorted(candidates, key=lambda c: (c.volatility, c.symbol))
    return [Candidate(symbol=c.symbol, price=c.price, volatility=c.volatility,
                      dollar_volume=c.dollar_volume, rank=i + 1)
            for i, c in enumerate(ordered)]


def select_with_banding(ranked: Sequence[Candidate],
                        held: Sequence[str],
                        *, target: int = TARGET_HOLDINGS,
                        entry_rank: int = ENTRY_RANK,
                        exit_rank: int = EXIT_RANK) -> Tuple[List[str], List[str], List[str]]:
    """밴딩 적용 종목 선택. `(보유목록, 신규편입, 편출)` 을 돌려준다.

    규칙:
      - 기존 보유는 순위 `exit_rank` 이내면 **유지**한다 (25위 밖이어도).
      - 신규는 순위 `entry_rank` 이내에서만 뽑는다.
      - 목표 종목 수를 채울 때까지 상위부터 채운다.

    이 비대칭이 회전율을 줄이는 장치다. 편입·편출 기준이 같으면 25위와 26위를
    오가는 종목 때문에 매 리밸런싱마다 불필요한 매매가 생긴다.
    """
    by_symbol = {c.symbol: c for c in ranked}
    held_set = set(held)

    keep = [s for s in held if s in by_symbol and by_symbol[s].rank <= exit_rank]
    # 유지분도 순위대로 정렬해 두면 자리가 모자랄 때 잘라내는 기준이 명확해진다.
    keep.sort(key=lambda s: by_symbol[s].rank)
    keep = keep[:target]

    room = target - len(keep)
    additions = [c.symbol for c in ranked
                 if c.rank <= entry_rank and c.symbol not in keep
                 and c.symbol not in held_set][:max(0, room)]

    new_holdings = keep + additions
    removed = [s for s in held if s not in new_holdings]
    return new_holdings, additions, removed


def is_rebalance_day(today: date, next_day: Optional[date],
                     months: Sequence[int] = REBALANCE_MONTHS) -> bool:
    """오늘이 리밸런싱 기준일(해당 월의 최종 거래일)인가.

    `next_day` 는 시간축상 다음 거래일. 그것이 다른 달이면 오늘이 이 달의
    마지막 거래일이다. `None`(시간축 끝)이면 리밸런싱하지 않는다 — 체결할
    다음 봉이 없기 때문이다.
    """
    if today.month not in months or next_day is None:
        return False
    return next_day.month != today.month


@dataclass
class RebalanceEvent:
    """한 번의 리밸런싱 기록. 무엇이 왜 바뀌었는지 남긴다."""
    ts: datetime
    holdings: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    blocked: Dict[str, str] = field(default_factory=dict)
    n_candidates: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {"ts": self.ts.date().isoformat(),
                "n_holdings": len(self.holdings), "holdings": list(self.holdings),
                "added": list(self.added), "removed": list(self.removed),
                "blocked": dict(self.blocked),
                "n_candidates": self.n_candidates}


def annual_turnover(events: Sequence[RebalanceEvent], years: float,
                    target: int = TARGET_HOLDINGS) -> float:
    """연 회전율(양방향, 자본 대비 배수).

    동일가중이므로 한 종목 = 자본의 1/target. 편입 1건 + 편출 1건이 각각
    그만큼의 매매대금을 만든다. `turnover_ratio` 와 같은 축이라
    `MAX_ANNUAL_TURNOVER` 와 직접 비교할 수 있다.
    """
    if years <= 0 or target <= 0:
        return 0.0
    trades = sum(len(e.added) + len(e.removed) for e in events)
    return trades / target / years
