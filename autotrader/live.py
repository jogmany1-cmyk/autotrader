"""라이브 트레이더 (모의/실계좌 공통).

브로커·데이터공급자·전략만 갈아끼우면 같은 코드가 그대로 돌아간다.
매 사이클마다:
  1) 유니버스에서 스크리너로 후보 축소
  2) 각 후보에 대해 앙상블 판단
  3) Risk Engine 승인 → 브로커 주문
  4) 보유 포지션은 stop/target/trailing/time 규칙으로 정리
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .broker.base import Broker
from .broker.paper import PaperBroker
from .config import Config
from .data.base import DataProvider
from .models import Bar, Order, Side
from .risk import RiskEngine
from .screener import Screener
from .strategy import (DayBreakout, DayMomentum, DayPullback, Ensemble,
                       MeanReversion, SwingTrend)
from .strategy.base import Strategy, StrategyContext

log = logging.getLogger("autotrader.live")


@dataclass
class CycleReport:
    ts: datetime
    candidates: int
    signals: int
    orders_placed: int
    orders_rejected: int
    closed_trades: int
    details: List[str] = field(default_factory=list)


class LiveTrader:
    def __init__(self, provider: DataProvider, broker: Broker, config: Config,
                 strategies: Optional[Sequence[Strategy]] = None,
                 ensemble_threshold: float = 0.55,
                 ensemble_min_votes: int = 1,
                 trail_pct: float = 0.05,
                 dry_run: bool = True):
        self.provider = provider
        self.broker = broker
        self.config = config
        self.strategies = list(strategies) if strategies else [
            DayBreakout(), DayPullback(), DayMomentum(), SwingTrend(), MeanReversion(),
        ]
        self.ensemble = Ensemble(self.strategies, config.weights,
                                 threshold=ensemble_threshold,
                                 min_votes=ensemble_min_votes)
        self.trail_pct = trail_pct
        self.dry_run = dry_run
        self.risk = RiskEngine(config.risk)

    def cycle(self, now: Optional[datetime] = None) -> CycleReport:
        now = now or datetime.utcnow()
        report = CycleReport(ts=now, candidates=0, signals=0,
                             orders_placed=0, orders_rejected=0, closed_trades=0)

        # 1. Screener
        universe = self.config.universe.symbols or self.provider.universe()
        screen = Screener(self.provider, self.config.universe).rank(universe)
        candidates = [r for r in screen if r.passed]
        report.candidates = len(candidates)

        # 2. 현재 계좌 상태
        positions = self.broker.positions()
        prices: Dict[str, float] = {}
        for sym in list(positions.keys()) + [c.symbol for c in candidates]:
            try:
                prices[sym] = self.provider.last_price(sym)
            except Exception:
                continue
        if isinstance(self.broker, PaperBroker):
            equity = self.broker.equity(prices)
        else:
            equity = self.broker.cash() + sum(
                p.qty * prices.get(sym, p.avg_price) for sym, p in positions.items()
            )
        self.risk.new_day(now.date(), equity)

        # 3. 각 후보에 대해 앙상블
        for cand in candidates:
            if cand.symbol in positions:
                continue
            bars = self.provider.history(cand.symbol, self.config.universe.lookback_days)
            if len(bars) < 60:
                continue
            dec = self.ensemble.evaluate(StrategyContext(cand.symbol, bars, len(bars) - 1))
            if dec.signal.side is not Side.BUY:
                continue
            report.signals += 1
            price = bars[-1].close
            decision = self.risk.evaluate_entry(
                symbol=cand.symbol, price=price, stop_price=dec.stop_hint,
                equity=equity, cash=self.broker.cash(),
                positions=positions, score=dec.score,
            )
            if not decision.allowed:
                report.orders_rejected += 1
                report.details.append(f"{cand.symbol}: {decision.reason}")
                continue
            order = Order(cand.symbol, Side.BUY, decision.qty, tag=dec.signal.reason[:32])
            if self.dry_run:
                report.details.append(f"[DRY] BUY {cand.symbol} x{decision.qty} @ {price:.2f}")
                continue
            try:
                if isinstance(self.broker, PaperBroker):
                    self.broker.submit(order, price_hint=price, ts=now,
                                       stop=dec.stop_hint, target=dec.target_hint)
                else:
                    self.broker.submit(order, price_hint=price)
                report.orders_placed += 1
            except Exception as exc:
                report.orders_rejected += 1
                report.details.append(f"{cand.symbol}: broker error {exc}")

        # 4. 보유 포지션의 청산 규칙 (페이퍼에서만 자동 처리; 실계좌는 스탑주문을
        #    브로커 쪽에 등록해야 정확하다).
        if isinstance(self.broker, PaperBroker):
            bars_today: Dict[str, Bar] = {}
            for sym in positions.keys():
                bars = self.provider.history(sym, 2)
                if bars:
                    bars_today[sym] = bars[-1]
            closed = self.broker.mark(bars_today, now,
                                      trail_pct=self.trail_pct,
                                      max_hold=self.config.execution.max_holding_bars)
            report.closed_trades = len(closed)
            for tr in closed:
                self.risk.register_exit(tr.pnl, now.date())
        return report
