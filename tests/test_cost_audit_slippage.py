"""비용 감사가 슬리피지를 빠뜨리고 있었다.

`build_cost_audit` 은 `total_slippage_est` 를 항상 0.0 으로 두고
`cost_to_capital_ratio` 를 수수료+세금만으로 계산했다. 콘솔 `[COST]` 줄도
같았고, `backtest.json` 에는 `cost_audit` 자체가 저장되지 않았다.

실데이터에서 그 차이가 드러났다. 리포트는 cost/capital 9.67% 를 찍었지만
매매대금 924,406,396 원에 설정 슬리피지 5bp 를 적용하면 462,203 원이 더 붙어
실제는 14.29% 였다. **게이트로 쓰라고 만든 숫자가 4.6%p 를 빠뜨리고 있었다.**

여기서 고정하는 것:
  - 슬리피지가 총비용과 cost/capital 에 들어가는가
  - `slippage_bp` 를 넘기지 않으면 **호출이 실패하는가** (조용한 과소보고 차단)
  - 추정치임이 값으로 드러나는가 (설정값이 리포트에 남는가)
  - **수익률에서 이중 차감하지 않는가** ← 가장 중요. 슬리피지는 이미 체결가에
    반영돼 있으므로, 감사 리포트를 고치다 성과 계산을 건드리면 같은 비용을
    두 번 빼게 된다.
"""
from datetime import datetime, timedelta

import pytest

from autotrader.backtest import Backtester
from autotrader.config import Config
from autotrader.data.synthetic import SyntheticProvider
from autotrader.metrics import build_cost_audit
from autotrader.models import Fill, Side

FILLS = [
    Fill(datetime(2026, 8, 24), "A", Side.BUY, 10, 1000, fee=100),
    Fill(datetime(2026, 8, 24), "A", Side.SELL, 10, 1050, fee=105, tax=189),
]
GROSS = 10 * 1000 + 10 * 1050          # 20,500
FEES_TAX = 100 + 105 + 189             # 394


def test_slippage_bp_is_required():
    """기본값을 두면 호출부가 빠뜨렸을 때 조용히 과소보고된다 — 그게 이 버그였다."""
    with pytest.raises(TypeError):
        build_cost_audit(FILLS, 100_000)        # type: ignore[call-arg]


def test_slippage_enters_total_and_ratio():
    a = build_cost_audit(FILLS, initial_capital=100_000, slippage_bp=5.0)
    expected_slip = GROSS * 5.0 / 10_000        # 10.25
    assert a.total_slippage_est == pytest.approx(expected_slip)
    assert a.total_cost == pytest.approx(FEES_TAX + expected_slip)
    # 코드가 비율을 소수점 6자리로 반올림하므로 그 폭까지만 따진다.
    assert a.cost_to_capital_ratio == pytest.approx(
        (FEES_TAX + expected_slip) / 100_000, abs=1e-6)


def test_zero_slippage_reproduces_old_number():
    """0bp 면 예전 값과 같다 — 달라진 것은 '빠뜨림'이지 '계산식'이 아니다."""
    a = build_cost_audit(FILLS, initial_capital=100_000, slippage_bp=0.0)
    assert a.total_slippage_est == 0.0
    assert a.cost_to_capital_ratio == pytest.approx(FEES_TAX / 100_000)


def test_estimate_is_labelled_as_such():
    """추정에 쓴 설정값이 남아야 나중에 이 숫자가 실측인지 아닌지 알 수 있다."""
    a = build_cost_audit(FILLS, initial_capital=100_000, slippage_bp=5.0)
    assert a.slippage_bp == 5.0
    d = a.to_dict()
    assert d["slippage_bp"] == 5.0
    assert "total_slippage_est" in d and "total_cost" in d
    assert a.as_line().startswith("[COST]")


def test_empty_fills_still_records_the_setting():
    a = build_cost_audit([], initial_capital=100_000, slippage_bp=5.0)
    assert a.n_fills == 0 and a.total_cost == 0.0
    assert a.cost_to_capital_ratio == 0.0
    assert a.slippage_bp == 5.0


# ---- 이중 차감 금지 ---------------------------------------------------------

def _run(slippage_bp: float):
    cfg = Config()
    cfg.costs.slippage_bp = slippage_bp
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    return Backtester(SyntheticProvider(), cfg, ensemble_threshold=0.45,
                      history_bars=600).run()


def test_audit_is_report_only_and_does_not_touch_returns():
    """슬리피지를 리포트에 더해도 성과 숫자는 그대로여야 한다.

    슬리피지는 PaperBroker 가 체결가에 이미 반영한다. 감사 리포트가 그것을
    한 번 더 빼면 같은 비용을 두 번 물리게 된다. 같은 설정으로 두 번 돌려
    성과가 동일한지 확인한다.
    """
    a, b = _run(5.0), _run(5.0)
    assert a.all.net_return == b.all.net_return
    assert a.all.profit_factor == b.all.profit_factor
    assert a.cost_audit is not None and b.cost_audit is not None
    assert a.cost_audit.total_cost == b.cost_audit.total_cost


def test_higher_slippage_hurts_returns_through_fills_not_twice():
    """슬리피지를 올리면 수익률이 나빠진다 — 체결가 경로로. 그 크기가
    이중 차감 수준(2배)이 아닌지도 함께 본다."""
    lo, hi = _run(0.0), _run(50.0)      # 0bp vs 50bp
    assert hi.all.net_return < lo.all.net_return, "체결가에 반영이 안 되고 있다"

    audit = hi.cost_audit
    assert audit is not None and audit.n_fills > 0
    # 리포트상 슬리피지는 매매대금 × 50bp 여야 한다.
    # 두 값 모두 소수점 2자리로 반올림돼 저장되므로 그 폭까지만 따진다.
    assert audit.total_slippage_est == pytest.approx(
        audit.total_gross_volume * 50.0 / 10_000, abs=0.01)

    # 수익률 악화폭이 슬리피지 추정치의 2배를 넘으면 어딘가에서 두 번 빼고 있다.
    capital = Config().backtest.initial_cash
    worsening = (lo.all.net_return - hi.all.net_return) * capital
    assert worsening <= audit.total_slippage_est * 2.0, (
        f"수익률 악화 {worsening:,.0f} 원이 슬리피지 추정치 "
        f"{audit.total_slippage_est:,.0f} 원의 2배를 넘는다 — 이중 차감 의심")
