"""비용 모델 정정 고정 — 2026 세율 + 틱 기반 슬리피지.

왜 이 파일이 있는가. 어떤 전략의 거래당 순수 우위(비용 전)를 역산했더니
+8.0bp 였는데, 백테스트가 쓰던 왕복비용은 31bp 였다. 그런데 그 31bp 자체가
두 곳에서 낙관적이었다:

  1. 세금 18bp → 2026-01-01 부터 코스피·코스닥 모두 **20bp**
  2. 슬리피지 편도 5bp 고정 → 다음 봉 시가 체결은 시장가이고, KRX 틱 하나가
     가격의 5~25bp 다. 5bp 는 "항상 중간가 체결"이라는 도달 불가능한 가정.

즉 채점판이 두 방향으로 관대했다. 여기서 고정해 두지 않으면 앞으로 무엇을
시험하든 같은 방식으로 과대평가된다.
"""
from datetime import datetime

import pytest

from autotrader.config import Costs
from autotrader.market import KRX_TICK_ABOVE_TOP, krx_tick_bp, krx_tick_size
from autotrader.metrics import build_cost_audit
from autotrader.models import Fill, Side


# ---- 세율 -----------------------------------------------------------------

def test_sell_tax_is_the_2026_rate():
    """2026-01-01 시행: 코스피 0.05%+농특세 0.15%, 코스닥 0.20% — 둘 다 20bp."""
    assert Costs().tax_sell_bp == 20.0


def test_round_trip_floor_is_dominated_by_tax():
    """수수료·슬리피지를 0 으로 만들어도 세금 20bp 는 남는다.

    이것이 '일 단위 매매' 틀의 하한이다. 거래당 순수 우위가 20bp 미만이면
    체결비용을 아무리 최적화해도 적자다.
    """
    c = Costs(commission_bp=0.0, slippage_bp=0.0, slippage_ticks=0.0)
    buy_side = c.commission_bp + c.slippage_bp_at(10_000)
    sell_side = c.commission_bp + c.tax_sell_bp + c.slippage_bp_at(10_000)
    assert buy_side + sell_side == pytest.approx(20.0)


# ---- 틱 그리드 -------------------------------------------------------------

@pytest.mark.parametrize("price,tick", [
    (1_999, 1), (2_000, 5), (4_999, 5), (5_000, 10), (19_999, 10),
    (20_000, 50), (49_999, 50), (50_000, 100), (199_999, 100),
    (200_000, 500), (499_999, 500), (500_000, KRX_TICK_ABOVE_TOP),
])
def test_krx_tick_grid_boundaries(price, tick):
    """2023-01-25 개정 호가단위. 경계값이 어긋나면 여기서 실패한다."""
    assert krx_tick_size(price) == tick


def test_relative_tick_is_worst_at_bracket_bottom():
    """구간 하단이 가장 비싸다 — 틱이 막 커진 직후이기 때문이다.

    이 성질 때문에 '고정 bp 슬리피지'가 특히 저가주에서 크게 빗나간다.
    """
    assert krx_tick_bp(2_000) == pytest.approx(25.0)     # 5원 / 2,000원
    assert krx_tick_bp(4_900) == pytest.approx(10.204, abs=1e-3)
    assert krx_tick_bp(1_999) < krx_tick_bp(2_000)       # 틱 경계를 넘는 순간 5배


def test_tick_size_never_zero_for_degenerate_price():
    assert krx_tick_size(0) > 0
    assert krx_tick_size(-5) > 0
    assert krx_tick_bp(0) == 0.0


# ---- 슬리피지 모드 ---------------------------------------------------------

def test_tick_mode_charges_more_than_the_old_flat_assumption():
    """정정의 핵심: 저가주에서 구 모델(5bp)은 실제의 몇 분의 일이었다."""
    c = Costs()                       # 기본 = tick 모드, 1틱
    assert c.slippage_bp_at(2_000) == pytest.approx(25.0)   # 구 모델의 5배
    assert c.slippage_bp_at(4_900) > 10.0                   # 구 모델의 2배 이상


def test_flat_bp_acts_as_a_floor_not_a_replacement():
    """호가단위가 촘촘한 고가주라도 체결비용이 0 은 아니다."""
    c = Costs(slippage_bp=5.0)
    # 19,999원: 틱 10원 = 5.0bp → 하한과 같은 수준
    assert c.slippage_bp_at(19_999) >= 5.0
    # 하한을 올리면 틱보다 커진 만큼 그대로 반영된다
    assert Costs(slippage_bp=30.0).slippage_bp_at(19_999) == 30.0


def test_fixed_mode_reproduces_the_old_behaviour():
    """구버전 결과와 비교하려면 예전 모델을 재현할 수 있어야 한다."""
    c = Costs(slippage_mode="fixed", slippage_bp=5.0)
    for price in (1_000, 2_000, 50_000, 500_000):
        assert c.slippage_bp_at(price) == 5.0


def test_more_ticks_crossed_costs_proportionally_more():
    """호가가 2~5틱 벌어지는 코스닥 중소형주를 모델링할 수 있어야 한다."""
    one = Costs(slippage_ticks=1.0, slippage_bp=0.0).slippage_bp_at(4_900)
    three = Costs(slippage_ticks=3.0, slippage_bp=0.0).slippage_bp_at(4_900)
    assert three == pytest.approx(one * 3)


# ---- 비용 감사 -------------------------------------------------------------

def _fill(price, qty, side=Side.BUY):
    gross = price * qty
    return Fill(ts=datetime(2026, 1, 5), symbol="A", side=side, qty=qty,
                price=price, fee=gross * 0.00015,
                tax=gross * 0.0020 if side is Side.SELL else 0.0)


def test_cost_audit_uses_per_fill_tick_slippage_when_costs_given():
    """저가주와 고가주가 섞이면 단일 bp 근사가 과소보고한다."""
    # 2,000원은 구간 하단이라 25bp, 190,000원은 구간 상단 근처라 5.26bp.
    # (200,000원을 쓰면 그쪽도 구간 하단이라 똑같이 25bp 가 되어 대비가 사라진다.)
    fills = [_fill(2_000, 95), _fill(190_000, 1)]     # 각각 19만원어치
    flat = build_cost_audit(fills, 10_000_000, slippage_bp=5.0)
    tick = build_cost_audit(fills, 10_000_000, slippage_bp=5.0, costs=Costs())
    assert tick.total_slippage_est > flat.total_slippage_est
    assert 5.0 < tick.slippage_bp < 25.0             # 두 값의 가중평균


def test_cost_audit_records_the_effective_rate_not_the_config_value():
    """tick 모드에서 설정값만 남기면 무엇이 적용됐는지 알 수 없다."""
    fills = [_fill(2_000, 100)]
    audit = build_cost_audit(fills, 10_000_000, slippage_bp=5.0, costs=Costs())
    assert audit.slippage_bp == pytest.approx(25.0)      # 설정값 5.0 이 아니다


def test_cost_audit_without_costs_keeps_the_old_single_rate_path():
    """`costs` 를 안 주면 기존 동작 그대로 — 호출부 호환."""
    fills = [_fill(2_000, 100)]
    audit = build_cost_audit(fills, 10_000_000, slippage_bp=5.0)
    assert audit.slippage_bp == 5.0
    assert audit.total_slippage_est == pytest.approx(200_000 * 0.0005)


def test_slippage_bp_still_required():
    """빠뜨리면 조용히 과소보고되던 버그 — 방어를 깨지 않았는지 확인."""
    with pytest.raises(TypeError):
        build_cost_audit([], 100_000)      # type: ignore[call-arg]
