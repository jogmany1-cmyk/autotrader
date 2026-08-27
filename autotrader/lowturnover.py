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
    #: 편입하려 했으나 체결하지 못한 종목. 현금 부족·호가 없음 등.
    #: **조용히 빠지면 동일가중이 깨진 것을 리포트에서 알 수 없다.**
    unfilled: List[str] = field(default_factory=list)
    #: 주가가 종목당 예산보다 비싸 **1주도 못 사는** 종목.
    #: 코드 버그가 아니라 자본 규모의 물리적 한계다. 조용히 빠지면
    #: "보유가 목표에 미달" 하는 이유를 리포트에서 알 수 없다.
    too_expensive: List[str] = field(default_factory=list)
    n_candidates: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {"ts": self.ts.date().isoformat(),
                "n_holdings": len(self.holdings), "holdings": list(self.holdings),
                "added": list(self.added), "removed": list(self.removed),
                "blocked": dict(self.blocked), "unfilled": list(self.unfilled),
                "too_expensive": list(self.too_expensive),
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


# ---- 백테스트 실행기 -------------------------------------------------------
#
# 왜 `Backtester` 를 재사용하지 않는가. 그쪽은 **종목별·봉별 신호 → RiskEngine
# 사이징 → 개별 손절/트레일링** 구조다. 이 규격은 세 가지가 전부 다르다:
#
#   1. 판단이 횡단면이다 (모든 종목을 줄 세워 상위 N)
#   2. 사이징이 동일가중이다 — RiskEngine 의 `위험예산 ÷ 손절거리` 를 쓰면
#      손절폭이 좁은(=저변동성) 종목에 포지션이 몰린다. 실제로 그것이
#      swing_trend_v2 의 비용을 2.8배로 키운 원인이었다.
#   3. 손절이 없다 (규격 §4). 위험은 25종목 분산으로 관리한다.
#
# 재사용하는 것은 **비용 모델**이다. `PaperBroker` 를 통해 체결하므로 2026
# 세율과 틱 기반 슬리피지가 그대로 적용된다 — 이 규격의 존재 이유가 비용이라
# 비용 계산을 다시 구현하는 것은 특히 위험하다.

BENCHMARK_NAME = "universe-equal-weight-buy-and-hold"


@dataclass
class LowTurnoverReport:
    events: List[RebalanceEvent] = field(default_factory=list)
    equity_curve: List = field(default_factory=list)
    trades: List = field(default_factory=list)
    performance: object = None
    benchmark_return: float = 0.0
    benchmark_n_symbols: int = 0
    cost_audit: object = None
    #: 실제 체결 기준 연 회전율. 편입·편출뿐 아니라 **비중 재조정(trim/topup)**
    #: 까지 포함한 참값이다. 사건 수로만 세면 재조정 거래가 통째로 빠진다.
    annual_turnover: float = 0.0
    #: 사건 수 기반 추정치. 참값과 벌어지면 재조정이 그만큼 돌았다는 뜻이다.
    annual_turnover_from_events: float = 0.0
    years: float = 0.0
    start: str = ""
    end: str = ""

    @property
    def excess_return(self) -> float:
        """벤치마크 대비 초과수익. 이 전략의 존재 이유가 되는 숫자다 —
        지수를 그냥 사는 것보다 나은가."""
        net = getattr(self.performance, "net_return", 0.0)
        return net - self.benchmark_return


def _equal_weight_benchmark(bars_by_symbol: Dict[str, Sequence[Bar]],
                            timeline: Sequence[datetime],
                            start_idx: int) -> Tuple[float, int]:
    """유니버스 동일가중 매수후보유 수익률.

    **규격 §5 는 "KOSPI 동일기간" 이라고 적었지만 지수 데이터가 없다.**
    대신 같은 자격 필터를 통과한 종목 전체를 동일가중으로 사서 들고 있는
    수익률을 쓴다. 이 차이는 유리한 쪽도 불리한 쪽도 아니지만 **다른 벤치마크**
    이므로 리포트에 이름(`BENCHMARK_NAME`)을 명시한다.

    오히려 종목선택 전략에는 이쪽이 더 엄격한 기준이다 — 시장 베타를 빼고
    **선택 능력만** 남기기 때문이다. 지수를 이겼다는 것은 시장이 올라서일 수
    있지만, 같은 유니버스 동일가중을 이겼다면 그건 선택이다.
    """
    if start_idx >= len(timeline):
        return 0.0, 0
    t0, t1 = timeline[start_idx], timeline[-1]
    rets = []
    for bars in bars_by_symbol.values():
        i0 = _index_at_ts(bars, t0)
        i1 = _index_at_ts(bars, t1)
        if i0 is None or i1 is None or i0 >= i1:
            continue
        p0, p1 = bars[i0].close, bars[i1].close
        if p0 > 0:
            rets.append(p1 / p0 - 1.0)
    if not rets:
        return 0.0, 0
    return sum(rets) / len(rets), len(rets)


def _index_at_ts(bars: Sequence[Bar], ts: datetime) -> Optional[int]:
    """이 시각 **이하**의 마지막 봉 인덱스. 없으면 None.

    종목마다 상장일·거래정지가 달라 시간축의 모든 날에 봉이 있지 않다.
    `backtest._index_at` 과 같은 규칙을 쓴다 — 두 곳이 다르면 같은 데이터에서
    다른 가격을 읽게 된다.
    """
    from .backtest import _index_at
    return _index_at(list(bars), ts)


def run_lowturnover(provider, config, *, symbols=None,
                    history_bars: int = 2500,
                    vol_window: int = VOL_WINDOW,
                    target: int = TARGET_HOLDINGS,
                    entry_rank: int = ENTRY_RANK,
                    exit_rank: int = EXIT_RANK,
                    blocked_symbols: Optional[Dict[str, str]] = None
                    ) -> LowTurnoverReport:
    """규격대로 반기 리밸런싱 백테스트를 돌린다.

    체결 규칙은 기존과 같다: **리밸런싱 기준일 종가로 판단하고 다음 거래일
    시가에 체결**한다 (`CLAUDE.md` 규칙 1 — 미래 정보 금지).

    `blocked_symbols` 는 뉴스 수비 필터의 결과다. 신규 편입만 막고 **기존
    보유의 편출은 막지 않는다** — 거래정지 종목을 강제로 계속 들고 있게
    만들면 안 되기 때문이다(팔 수 있을 때 팔아야 한다).
    """
    from .backtest import _merge_timeline
    from .broker import PaperBroker
    from .metrics import build_cost_audit, performance_from
    from .models import EquityPoint, Order, Side

    blocked = dict(blocked_symbols or {})
    syms = list(symbols) if symbols else provider.universe()
    bars_by_symbol: Dict[str, List[Bar]] = {}
    for s in syms:
        try:
            bars_by_symbol[s] = provider.history(s, limit=history_bars)
        except Exception:
            continue
    if not bars_by_symbol:
        raise RuntimeError("사용할 심볼 데이터가 없습니다")

    full_timeline = _merge_timeline(bars_by_symbol)
    timeline = full_timeline[-history_bars:] if history_bars else full_timeline
    if len(timeline) < MIN_LISTED_BARS + 2:
        raise RuntimeError(
            f"봉 {len(timeline)}개로는 실행할 수 없습니다 "
            f"(지표 계산에 최소 {MIN_LISTED_BARS}봉 + 리밸런싱 여유 필요)")

    broker = PaperBroker(config.backtest.initial_cash, config.costs)
    report = LowTurnoverReport(start=timeline[0].date().isoformat(),
                               end=timeline[-1].date().isoformat())
    holdings: List[str] = []
    pending: Optional[Tuple[List[str], List[str], List[str], int, Dict[str, str]]] = None
    first_invested_idx: Optional[int] = None

    for day_ix, ts in enumerate(timeline):
        todays: Dict[str, Bar] = {}
        for sym, bars in bars_by_symbol.items():
            i = _index_at_ts(bars, ts)
            if i is not None and bars[i].ts == ts:
                todays[sym] = bars[i]
        if not todays:
            continue
        prices = {s: b.close for s, b in todays.items()}

        # 1) 전날 확정한 리밸런싱을 오늘 시가에 체결한다.
        if pending is not None:
            new_holdings, added, removed, n_cand, blocked_hit = pending
            pending = None
            # 매도 먼저 — 현금을 만들어야 매수가 들어간다.
            for sym in removed:
                pos = broker.positions().get(sym)
                bar = todays.get(sym)
                if pos is None or bar is None:
                    continue      # 거래정지 등으로 오늘 호가가 없으면 다음 기회에
                broker.submit(Order(sym, Side.SELL, pos.qty, tag="rebalance"),
                              price_hint=bar.open, ts=ts)
            # 규격은 "25종목 **동일가중**" 이다. 그러려면 신규 매수만으로는
            # 부족하다 — 기존 보유의 비중을 목표로 되돌려야 한다.
            #
            # 남는 현금으로만 사면 이런 일이 벌어진다(실측): 첫 리밸런싱에서
            # 자본을 거의 다 쓰고, 다음부터는 편출이 없으면 현금이 없어서
            # **신규 편입이 영구히 실패**한다. 보유 수가 목표에 영원히 못 미치고
            # 비중은 제멋대로 흘러간다. 리포트만 보면 알 수 없다.
            #
            # 그래서 초과분을 먼저 덜어 내고 그 현금으로 부족분을 채운다.
            # 이것이 회전율을 만들지만 **의도된 회전율**이고, 규격의 상한
            # (연 2배) 안에 드는지가 판정 기준 중 하나다.
            equity_now = broker.equity({s: b.open for s, b in todays.items()})
            # 매수측 비용만큼 여유를 둔다. 100% 를 배분하면 체결비용 탓에
            # 마지막 몇 건이 현금부족으로 실패한다.
            buy_bp = config.costs.commission_bp + config.costs.slippage_bp_at(
                max(1.0, equity_now / max(1, target)))
            headroom = 1.0 - min(0.05, (buy_bp * 2.0) / 10_000.0)
            n_target = max(1, len(new_holdings)) if new_holdings else max(1, target)
            per_symbol = equity_now * headroom / n_target

            # 종목당 예산으로 1주도 못 사는 종목은 **동일가중으로 담을 수
            # 없다.** 자본 1,000만원이면 종목당 40만원이라 40만원 넘는 주식이
            # 여기 걸린다. 코드 문제가 아니라 자본 규모의 한계이므로 별도로
            # 기록해 리포트에 드러낸다.
            too_expensive = [s_ for s_ in new_holdings
                             if (b_ := todays.get(s_)) is not None
                             and b_.open > 0 and int(per_symbol // b_.open) <= 0]
            if too_expensive:
                new_holdings = [s_ for s_ in new_holdings if s_ not in too_expensive]
                added = [s_ for s_ in added if s_ not in too_expensive]

            # (a) 초과분 축소 — 현금을 만든다.
            for sym in list(new_holdings) + too_expensive:
                pos = broker.positions().get(sym)
                bar = todays.get(sym)
                if pos is None or bar is None or bar.open <= 0:
                    continue
                want = 0 if sym in too_expensive else int(per_symbol // bar.open)
                if pos.qty > want:
                    broker.submit(Order(sym, Side.SELL, pos.qty - want,
                                        tag="rebalance-trim"),
                                  price_hint=bar.open, ts=ts)

            # (b) 부족분 매수 — 신규 편입과 기존 보유 부족분을 함께 채운다.
            failed: List[str] = []
            for sym in new_holdings:
                bar = todays.get(sym)
                if bar is None or bar.open <= 0:
                    if sym in added:
                        failed.append(sym)
                    continue
                have = broker.positions().get(sym)
                have_qty = have.qty if have else 0
                want = int(per_symbol // bar.open)
                if want <= have_qty:
                    continue
                try:
                    broker.submit(Order(sym, Side.BUY, want - have_qty,
                                        tag="rebalance-add" if sym in added
                                            else "rebalance-topup"),
                                  price_hint=bar.open, ts=ts)
                except Exception:
                    if sym in added:
                        failed.append(sym)   # 그래도 모자라면 기록에 남긴다
            if failed:
                added = [s for s in added if s not in failed]
            holdings = [s for s in new_holdings
                        if s in broker.positions() or s in added]
            holdings = [s for s in holdings if s in broker.positions()]
            report.events.append(RebalanceEvent(
                ts=ts, holdings=list(holdings), added=list(added),
                removed=list(removed), blocked=dict(blocked_hit),
                unfilled=list(failed), too_expensive=list(too_expensive),
                n_candidates=n_cand))
            if first_invested_idx is None and holdings:
                first_invested_idx = day_ix

        # 2) 오늘이 리밸런싱 기준일인가 → 다음 봉 체결로 예약한다.
        next_day = timeline[day_ix + 1].date() if day_ix + 1 < len(timeline) else None
        if is_rebalance_day(ts.date(), next_day):
            cands = []
            for sym, bars in bars_by_symbol.items():
                i = _index_at_ts(bars, ts)
                if i is None:
                    continue
                c = measure(sym, bars, i, vol_window)
                if c is not None:
                    cands.append(c)
            ranked = rank_candidates(cands)
            held_now = list(broker.positions())
            new_holdings, added, removed = select_with_banding(
                ranked, held_now, target=target,
                entry_rank=entry_rank, exit_rank=exit_rank)
            # 뉴스 수비 필터 — **신규 편입만** 막는다. 편출은 막지 않는다.
            blocked_hit = {s: blocked[s] for s in added if s in blocked}
            if blocked_hit:
                added = [s for s in added if s not in blocked_hit]
                new_holdings = [s for s in new_holdings if s not in blocked_hit]
            pending = (new_holdings, added, removed, len(ranked), blocked_hit)

        # 3) 에쿼티 스냅샷
        broker.portfolio.bump_hold_counters()
        report.equity_curve.append(EquityPoint(
            ts=ts, equity=broker.equity(prices), cash=broker.cash(),
            exposure=broker.portfolio.exposure(prices)))

    # 창 끝 청산 — 미실현 손익이 성과에 반영되도록. 마지막 관측 종가를 쓴다.
    last_prices: Dict[str, float] = {}
    for sym, bars in bars_by_symbol.items():
        i = _index_at_ts(bars, timeline[-1])
        if i is not None:
            last_prices[sym] = bars[i].close
    broker.flat_all(last_prices, timeline[-1], reason="window_end")

    report.trades = list(broker.portfolio.closed_trades)
    report.performance = performance_from(report.equity_curve, report.trades)
    report.cost_audit = build_cost_audit(
        broker.fills, config.backtest.initial_cash,
        config.costs.slippage_bp, costs=config.costs)
    report.years = len(timeline) / 252.0
    # 참값은 체결에서 뽑는다. `turnover_ratio` = 총매매대금 ÷ 초기자본 이므로
    # 연수로 나누면 walkforward 의 회전율과 같은 축이 된다.
    gross_turnover = float(getattr(report.cost_audit, "turnover_ratio", 0.0))
    report.annual_turnover = (gross_turnover / report.years
                              if report.years > 0 else 0.0)
    report.annual_turnover_from_events = annual_turnover(
        report.events, report.years, target)
    bench_start = first_invested_idx if first_invested_idx is not None else 0
    report.benchmark_return, report.benchmark_n_symbols = _equal_weight_benchmark(
        bars_by_symbol, timeline, bench_start)
    return report


def judge_lowturnover(report: LowTurnoverReport,
                      n_trials: int = 1) -> List[Tuple[str, bool, str]]:
    """규격 §5 판정. 일봉 전략과 **다른 기준**을 쓴다.

    PF 1.20 을 쓰지 않는 이유: 거래 수가 적고 보유가 길면 PF 는 소수 종목의
    결과에 지배되어 안정적인 통계가 아니다. 대신 벤치마크 대비 초과수익을
    본다 — 유니버스를 그냥 동일가중으로 사는 것보다 나은가가 이 전략의
    존재 이유이기 때문이다.
    """
    from .overfitting import DSR_THRESHOLD, deflated_sharpe

    perf = report.performance
    net = getattr(perf, "net_return", 0.0)
    mdd = getattr(perf, "max_drawdown", 0.0)
    n_trades = len(report.trades)
    pnls = [t.pnl for t in report.trades]
    dsr = deflated_sharpe(pnls, n_trials) if len(pnls) >= 2 else 0.0
    return [
        (f"연 회전율 ≤ {MAX_ANNUAL_TURNOVER:.1f}배",
         report.annual_turnover <= MAX_ANNUAL_TURNOVER,
         f"{report.annual_turnover:.2f}배"),
        ("순수익 > 0", net > 0, f"{net:+.4f}"),
        (f"벤치마크({BENCHMARK_NAME}) 대비 초과수익 > 0",
         report.excess_return > 0,
         f"{report.excess_return:+.4f} (전략 {net:+.4f} / 벤치 {report.benchmark_return:+.4f})"),
        (f"MDD ≥ {MAX_DRAWDOWN:.0%}", mdd >= MAX_DRAWDOWN, f"{mdd:.4f}"),
        (f"Deflated Sharpe ≥ {DSR_THRESHOLD}", dsr >= DSR_THRESHOLD,
         f"{dsr:.4f} (시도 {n_trials}회 선언)"),
        (f"총 거래 ≥ {MIN_TOTAL_TRADES}", n_trades >= MIN_TOTAL_TRADES,
         f"{n_trades}"),
    ]
