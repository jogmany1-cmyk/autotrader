"""이벤트 기반 백테스트.

원칙:
- 봉 N 의 판단은 봉 N 의 데이터까지만 사용해서 만든다.
- 진입/청산 체결은 다음 봉 시가에 일어난다 (look-ahead 방지).
- 수수료·거래세·슬리피지·현금 부족·체결 실패가 모두 반영된다.
- 백테스트는 train / val / oos 로 자동 분할되며, 파라미터 튜닝은 val 까지만,
  최종 성적은 oos 로 판단한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .broker import PaperBroker
from .config import Config
from .data.base import DataProvider
from .metrics import PerformanceReport, performance_from
from .models import (Bar, EquityPoint, Fill, Order, Position, ScreenResult,
                     Side, Trade)
from .risk import RiskEngine
from .screener import Screener
from .strategy import (DayBreakout, DayMomentum, DayPullback, Ensemble,
                       MeanReversion, SwingTrend)
from .strategy.base import Strategy, StrategyContext


@dataclass
class BacktestReport:
    train: PerformanceReport
    val: PerformanceReport
    oos: PerformanceReport
    all: PerformanceReport
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    screen_snapshot: List[ScreenResult] = field(default_factory=list)


class Backtester:
    def __init__(self, provider: DataProvider, config: Config,
                 strategies: Optional[Sequence[Strategy]] = None,
                 ensemble_threshold: float = 0.55,
                 ensemble_min_votes: int = 1,
                 trail_pct: float = 0.05):
        self.provider = provider
        self.config = config
        self.strategies = list(strategies) if strategies else self._default_strategies()
        self.ensemble = Ensemble(self.strategies, config.weights,
                                 threshold=ensemble_threshold,
                                 min_votes=ensemble_min_votes)
        self.trail_pct = trail_pct

    def _default_strategies(self) -> List[Strategy]:
        return [
            DayBreakout(),
            DayPullback(),
            DayMomentum(),
            SwingTrend(),
            MeanReversion(),
        ]

    # ------------------------------------------------------------------ run
    def run(self, symbols: Optional[Sequence[str]] = None) -> BacktestReport:
        u = self.config.universe
        symbols = list(symbols) if symbols else (u.symbols or self.provider.universe())

        # 1) 데이터 로딩과 시간축 정렬
        bars_by_symbol: Dict[str, List[Bar]] = {}
        for s in symbols:
            try:
                bars_by_symbol[s] = self.provider.history(s, limit=u.lookback_days * 4)
            except Exception:
                continue
        symbols = list(bars_by_symbol.keys())
        if not symbols:
            raise RuntimeError("백테스트에 사용할 심볼 데이터가 없습니다")
        timeline = _merge_timeline(bars_by_symbol)
        if not timeline:
            raise RuntimeError("공통 시간축을 만들 수 없습니다")

        # 2) 브로커·리스크·기록기 초기화
        broker = PaperBroker(self.config.backtest.initial_cash, self.config.costs)
        risk = RiskEngine(self.config.risk)
        equity_points: List[EquityPoint] = []
        pending: List[Tuple[str, float, float, str]] = []  # (symbol, stop, target, tag)

        first_seen_close: Dict[str, float] = {}

        for day_ix, ts in enumerate(timeline):
            todays_bars: Dict[str, Bar] = {}
            for sym in symbols:
                idx = _index_at(bars_by_symbol[sym], ts)
                if idx is None:
                    continue
                todays_bars[sym] = bars_by_symbol[sym][idx]
                first_seen_close.setdefault(sym, bars_by_symbol[sym][idx].close)

            if not todays_bars:
                continue

            # 2.1 대기 주문 체결(전일 신호 → 오늘 시가)
            for sym, stop, target, tag in pending:
                bar = todays_bars.get(sym)
                if bar is None:
                    continue
                price = bar.open
                positions = broker.positions()
                equity = broker.equity({s: b.close for s, b in todays_bars.items()})
                risk.new_day(ts.date(), equity)
                decision = risk.evaluate_entry(
                    symbol=sym, price=price, stop_price=stop,
                    equity=equity, cash=broker.cash(),
                    positions=positions, score=1.0,
                )
                if not decision.allowed:
                    continue
                try:
                    broker.submit(
                        Order(sym, Side.BUY, decision.qty, tag=tag),
                        price_hint=price, ts=ts, stop=stop, target=target,
                    )
                except Exception:
                    continue
            pending.clear()

            # 2.2 오늘의 exit trigger (스탑/타깃/시간청산). 트레일링도 여기서.
            closed = broker.mark(todays_bars, ts,
                                 trail_pct=self.trail_pct,
                                 max_hold=self.config.execution.max_holding_bars)
            for tr in closed:
                risk.register_exit(tr.pnl, ts.date())

            # 2.3 오늘 종가 확정 후, 각 종목에 대해 앙상블 판단 → 내일 시가 진입 준비
            positions_now = broker.positions()
            equity_now = broker.equity({s: b.close for s, b in todays_bars.items()})
            risk.new_day(ts.date(), equity_now)
            for sym, bar in todays_bars.items():
                if sym in positions_now:
                    continue
                bars = bars_by_symbol[sym]
                idx = _index_at(bars, ts)
                if idx is None:
                    continue
                dec = self.ensemble.evaluate(StrategyContext(sym, bars, idx))
                if dec.signal.side is not Side.BUY:
                    continue
                pending.append((sym, dec.stop_hint, dec.target_hint, dec.signal.reason[:40]))

            # 2.4 에쿼티 스냅샷
            prices = {s: b.close for s, b in todays_bars.items()}
            eq = broker.equity(prices)
            exposure = broker.portfolio.exposure(prices)
            equity_points.append(EquityPoint(ts=ts, equity=round(eq, 2),
                                             cash=round(broker.cash(), 2),
                                             exposure=round(exposure / eq, 4) if eq > 0 else 0.0))

        # 3) 성과 분해
        splits = self.config.backtest.splits(len(equity_points))
        report_all = performance_from(equity_points, broker.portfolio.closed_trades)
        report_train = performance_from(
            equity_points[splits["train"]],
            [t for t in broker.portfolio.closed_trades if _in_slice(t.exit_ts, equity_points, splits["train"])],
        )
        report_val = performance_from(
            equity_points[splits["val"]],
            [t for t in broker.portfolio.closed_trades if _in_slice(t.exit_ts, equity_points, splits["val"])],
        )
        report_oos = performance_from(
            equity_points[splits["oos"]],
            [t for t in broker.portfolio.closed_trades if _in_slice(t.exit_ts, equity_points, splits["oos"])],
        )

        screen = Screener(self.provider, self.config.universe).rank(symbols)

        return BacktestReport(
            train=report_train, val=report_val, oos=report_oos, all=report_all,
            trades=list(broker.portfolio.closed_trades),
            equity_curve=equity_points, screen_snapshot=screen,
        )


def _merge_timeline(bars_by_symbol: Dict[str, List[Bar]]) -> List[datetime]:
    seen: Dict[datetime, None] = {}
    for bars in bars_by_symbol.values():
        for b in bars:
            seen[b.ts] = None
    return sorted(seen)


def _index_at(bars: List[Bar], ts: datetime) -> Optional[int]:
    # 봉이 정렬돼 있다는 전제 하에서 이분탐색. 심볼당 O(log n).
    lo, hi = 0, len(bars) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].ts == ts:
            return mid
        if bars[mid].ts < ts:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def _in_slice(ts: datetime, points: List[EquityPoint], sl: slice) -> bool:
    if not points:
        return False
    lo = points[sl.start].ts if sl.start < len(points) else points[-1].ts
    hi = points[sl.stop - 1].ts if 0 < sl.stop <= len(points) else points[-1].ts
    return lo <= ts <= hi
