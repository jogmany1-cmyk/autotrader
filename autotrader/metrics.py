"""성과지표 — 승률 하나로 전략을 판단하지 않기 위한 도구.

수익률 시계열이나 트레이드 리스트에서 다음을 뽑아 낸다:
- Net Return / CAGR
- Max Drawdown
- Sharpe / Sortino
- Profit Factor / Expectancy / Payoff Ratio
- Win Rate / Max Consecutive Losses / Trade Count

모든 함수는 numpy 없이 동작한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Sequence

from .indicators import drawdown_series
from .models import EquityPoint, Fill, Trade


@dataclass
class CostAudit:
    """자동매매 실패 사례에서 가장 큰 킬러 — 회전율과 총 비용.

    "1,000만원인데 하루 4,000만원 회전" 같은 상황을 숫자로 보이게.

    **슬리피지는 추정치다.** `PaperBroker` 는 체결가에 슬리피지를 녹여 넣고
    (paper.py), `Fill` 은 주문 기준가를 남기지 않는다. 그래서 실측할 방법이
    없고 매매대금 × 설정 slippage_bp 로 되짚을 수밖에 없다. 이름의 `_est`
    와 출력의 "(추정)" 표기가 그 뜻이다.

    이 값은 **리포트 전용**이다. 수익률에는 체결가를 통해 이미 반영돼 있으므로
    어디서도 다시 빼면 안 된다 (이중 차감 금지).
    """
    total_gross_volume: float          # 총 매매대금 (매수+매도 절댓값 합)
    total_fees: float                  # 총 수수료
    total_taxes: float                 # 총 거래세 (매도)
    total_slippage_est: float          # 슬리피지 **추정치** — 실측 아님
    cost_to_capital_ratio: float       # (fees + taxes + slippage_est) / initial_capital
    turnover_ratio: float              # total_gross_volume / initial_capital
    avg_trade_size: float
    n_fills: int
    slippage_bp: float = 0.0           # 추정에 쓴 설정값. 재현·감사용으로 남긴다

    @property
    def total_cost(self) -> float:
        """수수료 + 세금 + 슬리피지(추정). 게이트가 봐야 할 숫자."""
        return self.total_fees + self.total_taxes + self.total_slippage_est

    def to_dict(self) -> Dict[str, float]:
        d = asdict(self)
        d["total_cost"] = round(self.total_cost, 2)
        return d

    def as_line(self) -> str:
        return (f"[COST] fills={self.n_fills} turnover×{self.turnover_ratio:.2f} "
                f"cost={self.total_cost:,.0f} "
                f"(cost/capital={self.cost_to_capital_ratio*100:.2f}%)")


def build_cost_audit(fills: Sequence[Fill], initial_capital: float,
                     slippage_bp: float) -> CostAudit:
    """체결 리스트에서 비용 감사 리포트 생성.

    `slippage_bp` 는 `Costs.slippage_bp` 를 그대로 받는다. **기본값을 두지
    않는다** — 호출부가 빠뜨리면 비용이 조용히 과소보고되고, 실제로 그렇게
    되어 있었다. 실데이터 백테스트에서 리포트는 cost/capital 9.67% 를
    찍었지만 슬리피지를 포함한 실제는 14.29% 였다. 게이트로 쓰라고 만든
    숫자가 4.6%p 를 빠뜨리고 있었던 셈이다.

    슬리피지 추정에는 근사가 하나 들어간다. `gross` 는 이미 슬리피지가 반영된
    체결가 기준이라 엄밀히는 매수 `gross×s/(1+s)`, 매도 `gross×s/(1-s)` 이지만,
    s=5bp 에서 둘의 오차는 슬리피지 금액의 0.05% 수준이라 상쇄되고 무시된다.
    """
    n = len(fills)
    if n == 0 or initial_capital <= 0:
        return CostAudit(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0,
                         slippage_bp=slippage_bp)
    gross = sum(f.gross for f in fills)
    fees = sum(f.fee for f in fills)
    taxes = sum(f.tax for f in fills)
    slip_est = gross * (slippage_bp / 10_000)
    return CostAudit(
        total_gross_volume=round(gross, 2),
        total_fees=round(fees, 2),
        total_taxes=round(taxes, 2),
        total_slippage_est=round(slip_est, 2),
        cost_to_capital_ratio=round((fees + taxes + slip_est) / initial_capital, 6),
        turnover_ratio=round(gross / initial_capital, 3),
        avg_trade_size=round(gross / n, 2),
        n_fills=n,
        slippage_bp=slippage_bp,
    )


@dataclass
class PerformanceReport:
    n_trades: int
    win_rate: float
    net_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    profit_factor: float
    expectancy: float
    payoff_ratio: float
    avg_win: float
    avg_loss: float
    max_consecutive_losses: int
    exposure_avg: float
    days: int

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


def performance_from(equity: Sequence[EquityPoint], trades: Sequence[Trade],
                     periods_per_year: int = 252) -> PerformanceReport:
    if not equity:
        return _empty()
    equity_vals = [p.equity for p in equity]
    start = equity_vals[0]
    end = equity_vals[-1]
    net = end / start - 1.0 if start > 0 else 0.0
    days = len(equity)
    years = max(days / periods_per_year, 1e-9)
    cagr = (end / start) ** (1 / years) - 1.0 if start > 0 else 0.0
    dd = min(drawdown_series(equity_vals)) if equity_vals else 0.0

    daily_returns = []
    for i in range(1, len(equity_vals)):
        prev = equity_vals[i - 1]
        daily_returns.append(equity_vals[i] / prev - 1.0 if prev > 0 else 0.0)
    sharpe = _sharpe(daily_returns, periods_per_year)
    sortino = _sortino(daily_returns, periods_per_year)

    # Trade-level metrics
    if trades:
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        wr = len(wins) / len(trades)
        expectancy = sum(t.pnl for t in trades) / len(trades)
        payoff = (avg_win / -avg_loss) if avg_loss < 0 else float("inf") if avg_win > 0 else 0.0
        max_consec = _max_consecutive_losses(trades)
    else:
        avg_win = avg_loss = pf = wr = expectancy = payoff = 0.0
        max_consec = 0

    exposure_avg = sum(p.exposure for p in equity) / len(equity) if equity else 0.0

    return PerformanceReport(
        n_trades=len(trades),
        win_rate=round(wr, 4),
        net_return=round(net, 4),
        cagr=round(cagr, 4),
        max_drawdown=round(dd, 4),
        sharpe=round(sharpe, 3),
        sortino=round(sortino, 3),
        profit_factor=round(pf, 3) if pf != float("inf") else pf,
        expectancy=round(expectancy, 2),
        payoff_ratio=round(payoff, 3) if payoff != float("inf") else payoff,
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        max_consecutive_losses=max_consec,
        exposure_avg=round(exposure_avg, 4),
        days=days,
    )


def _sharpe(rets: Sequence[float], ann: int) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(ann)


def _sortino(rets: Sequence[float], ann: int) -> float:
    if not rets:
        return 0.0
    neg = [r for r in rets if r < 0]
    if not neg:
        return 0.0
    downside = math.sqrt(sum(r * r for r in neg) / len(neg))
    if downside == 0:
        return 0.0
    mean = sum(rets) / len(rets)
    return mean / downside * math.sqrt(ann)


def _max_consecutive_losses(trades: Sequence[Trade]) -> int:
    best = cur = 0
    for t in trades:
        if t.pnl <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _empty() -> PerformanceReport:
    return PerformanceReport(
        n_trades=0, win_rate=0.0, net_return=0.0, cagr=0.0,
        max_drawdown=0.0, sharpe=0.0, sortino=0.0, profit_factor=0.0,
        expectancy=0.0, payoff_ratio=0.0, avg_win=0.0, avg_loss=0.0,
        max_consecutive_losses=0, exposure_avg=0.0, days=0,
    )
