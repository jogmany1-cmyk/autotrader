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
from .models import EquityPoint, Trade


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
