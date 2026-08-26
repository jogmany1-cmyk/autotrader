"""저회전 팩터 규격 고정 — docs/LOW-TURNOVER-SPEC.md.

이 규격의 존재 이유는 하나다: 거래당 순수 우위(-27.9bp ~ +8.0bp)가 왕복비용
(33~103bp)보다 작다는 것이 계산으로 확인됐고, 그 격차는 지표를 바꿔서 메울 수
없다. **회전율을 줄이는 것만이 남은 방향**이다. 그래서 여기서 가장 중요하게
고정하는 것은 신호의 품질이 아니라 **회전율 상한과 밴딩의 작동**이다.
"""
from datetime import date, datetime, timedelta

import pytest

from autotrader import lowturnover as lt
from autotrader.models import Bar


# ---- 규격 상수 -------------------------------------------------------------

def test_spec_constants_match_the_document():
    """문서와 코드가 갈라지면 사전등록이 무의미해진다."""
    assert lt.TARGET_HOLDINGS == 25
    assert (lt.ENTRY_RANK, lt.EXIT_RANK) == (25, 40)
    assert lt.REBALANCE_MONTHS == (6, 12)
    assert lt.MAX_ANNUAL_TURNOVER == 2.0
    assert lt.MAX_DRAWDOWN == -0.35        # 손절이 없으므로 기존 -25% 보다 완화


def test_turnover_ceiling_is_far_below_the_novy_marx_velikov_line():
    """NMV 의 '월 편도 50%' = 연 12배. 이 규격은 그보다 훨씬 아래를 목표한다."""
    from autotrader.walkforward import MAX_ANNUAL_TURNOVER as DAILY_LIMIT
    assert lt.MAX_ANNUAL_TURNOVER < DAILY_LIMIT / 5


# ---- 밴딩: 이 규격의 핵심 장치 ----------------------------------------------

def _ranked(n=60):
    """순위 1..n 인 후보들. 변동성은 순위에 비례."""
    return lt.rank_candidates([
        lt.Candidate(symbol=f"S{i:03d}", price=10_000,
                     volatility=0.10 + i * 0.001, dollar_volume=1e9)
        for i in range(n)
    ])


def test_banding_keeps_a_holding_that_drifted_past_the_entry_rank():
    """26위로 밀린 보유 종목을 갈아타지 않는다 — 이것이 회전율 절감의 핵심."""
    ranked = _ranked()
    drifted = ranked[29].symbol          # 30위
    held = [drifted] + [c.symbol for c in ranked[:24]]
    holdings, added, removed = lt.select_with_banding(ranked, held)
    assert drifted in holdings           # 25위 밖이지만 40위 이내라 유지
    assert drifted not in removed


def test_banding_drops_a_holding_past_the_exit_rank():
    ranked = _ranked()
    fallen = ranked[44].symbol           # 45위 — exit_rank 40 밖
    held = [fallen] + [c.symbol for c in ranked[:24]]
    holdings, added, removed = lt.select_with_banding(ranked, held)
    assert fallen not in holdings
    assert fallen in removed


def test_new_entries_only_from_within_entry_rank():
    """편입은 엄격하게. 26위를 새로 사지 않는다."""
    ranked = _ranked()
    holdings, added, removed = lt.select_with_banding(ranked, held=[])
    assert len(added) == lt.TARGET_HOLDINGS
    for symbol in added:
        rank = next(c.rank for c in ranked if c.symbol == symbol)
        assert rank <= lt.ENTRY_RANK


def test_banding_produces_less_turnover_than_naive_reselection():
    """밴딩의 효과를 직접 측정한다 — 이것이 안 되면 규격의 전제가 무너진다."""
    held = [c.symbol for c in _ranked()[:25]]
    banded_trades = naive_trades = 0
    for shift in range(1, 5):
        # 매 기간 순위가 조금씩 흔들리는 상황을 만든다.
        ranked = lt.rank_candidates([
            lt.Candidate(symbol=f"S{i:03d}", price=10_000,
                         volatility=0.10 + ((i + shift * 3) % 60) * 0.001,
                         dollar_volume=1e9)
            for i in range(60)
        ])
        b_hold, b_add, b_rem = lt.select_with_banding(ranked, held)
        banded_trades += len(b_add) + len(b_rem)
        # 밴딩 없음 = 편입·편출 기준을 똑같이 25위로
        n_hold, n_add, n_rem = lt.select_with_banding(
            ranked, held, entry_rank=25, exit_rank=25)
        naive_trades += len(n_add) + len(n_rem)
        held = b_hold
    assert banded_trades < naive_trades


def test_holdings_never_exceed_target():
    ranked = _ranked()
    held = [c.symbol for c in ranked[:40]]      # 이미 초과 보유
    holdings, _, _ = lt.select_with_banding(ranked, held)
    assert len(holdings) <= lt.TARGET_HOLDINGS


def test_delisted_holding_is_dropped_without_error():
    """유니버스에서 사라진 종목(상장폐지 등)은 조용히 편출된다."""
    ranked = _ranked()
    held = ["GONE"] + [c.symbol for c in ranked[:24]]
    holdings, _, removed = lt.select_with_banding(ranked, held)
    assert "GONE" not in holdings and "GONE" in removed


# ---- 신호 측정: 미래정보 차단 ----------------------------------------------

def _bars(n, start_price=10_000, vol=0.01, volume=200_000):
    out, price = [], start_price
    for i in range(n):
        price *= (1.0 + (vol if i % 2 else -vol))
        out.append(Bar(datetime(2020, 1, 1) + timedelta(days=i),
                       price, price * 1.005, price * 0.995, price, volume))
    return out


def test_measure_never_reads_past_the_boundary():
    """`at` 이후 봉을 바꿔도 결과가 같아야 한다 — 미래정보 누출 방지."""
    bars = _bars(400)
    at = 300
    before = lt.measure("A", bars, at)
    tampered = list(bars)
    for i in range(at + 1, len(tampered)):
        b = tampered[i]
        tampered[i] = Bar(b.ts, b.open * 5, b.high * 5, b.low * 5,
                          b.close * 5, b.volume * 5)
    after = lt.measure("A", tampered, at)
    assert before == after


def test_measure_rejects_too_short_history():
    assert lt.measure("A", _bars(100), 99) is None


def test_measure_rejects_penny_stocks():
    """저가주는 호가 상대틱이 지배적이다 — 2,000원에서 1틱이 편도 25bp."""
    assert lt.measure("A", _bars(400, start_price=500), 399) is None


def test_measure_rejects_illiquid():
    assert lt.measure("A", _bars(400, volume=10), 399) is None


def test_lower_volatility_ranks_first():
    calm = lt.measure("CALM", _bars(400, vol=0.003), 399)
    wild = lt.measure("WILD", _bars(400, vol=0.05), 399)
    ranked = lt.rank_candidates([wild, calm])
    assert ranked[0].symbol == "CALM" and ranked[0].rank == 1


def test_ranking_is_invariant_to_input_order():
    """입력 순서가 결과를 바꾸면 안 된다 — 실제로 겪은 버그다."""
    cands = [lt.Candidate(f"S{i}", 10_000, 0.1 + i * 0.01, 1e9) for i in range(10)]
    fwd = [c.symbol for c in lt.rank_candidates(cands)]
    rev = [c.symbol for c in lt.rank_candidates(list(reversed(cands)))]
    assert fwd == rev


def test_ties_break_by_symbol():
    cands = [lt.Candidate("ZZZ", 10_000, 0.2, 1e9),
             lt.Candidate("AAA", 10_000, 0.2, 1e9)]
    assert [c.symbol for c in lt.rank_candidates(cands)] == ["AAA", "ZZZ"]


# ---- 리밸런싱 일정 ---------------------------------------------------------

def test_rebalances_only_on_last_trading_day_of_june_and_december():
    assert lt.is_rebalance_day(date(2026, 6, 30), date(2026, 7, 1))
    assert lt.is_rebalance_day(date(2026, 12, 30), date(2027, 1, 2))
    assert not lt.is_rebalance_day(date(2026, 6, 29), date(2026, 6, 30))
    assert not lt.is_rebalance_day(date(2026, 3, 31), date(2026, 4, 1))


def test_no_rebalance_without_a_next_bar_to_fill_on():
    """체결할 다음 봉이 없으면 리밸런싱하지 않는다."""
    assert not lt.is_rebalance_day(date(2026, 6, 30), None)


# ---- 회전율 계산 -----------------------------------------------------------

def test_annual_turnover_of_a_full_replacement_is_two():
    """25종목 전량 교체 = 편입 25 + 편출 25 = 자본의 200% = 2.0배."""
    ev = lt.RebalanceEvent(ts=datetime(2026, 6, 30),
                           added=[f"A{i}" for i in range(25)],
                           removed=[f"R{i}" for i in range(25)])
    assert lt.annual_turnover([ev], years=1.0) == pytest.approx(2.0)


def test_realistic_semiannual_churn_stays_under_the_ceiling():
    """반기마다 25종목 중 5개만 바뀌면 연 0.8배 — 상한 2.0 의 절반 이하."""
    events = [lt.RebalanceEvent(ts=datetime(2026, m, 30),
                                added=[f"A{m}{i}" for i in range(5)],
                                removed=[f"R{m}{i}" for i in range(5)])
              for m in (6, 12)]
    turnover = lt.annual_turnover(events, years=1.0)
    assert turnover == pytest.approx(0.8)
    assert turnover <= lt.MAX_ANNUAL_TURNOVER


def test_daily_trading_turnover_would_blow_the_ceiling():
    """폐기한 전략의 실측 회전율(12~54배)이 이 규격에서 통과하지 못함을 고정."""
    for measured in (12.1, 54.4):
        assert measured > lt.MAX_ANNUAL_TURNOVER


def test_zero_years_does_not_divide_by_zero():
    assert lt.annual_turnover([], years=0.0) == 0.0
