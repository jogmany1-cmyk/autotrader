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
from . import indicators as ind
from .config import Config
from .cooldown import CooldownRegistry
from .data.base import DataProvider
from .market import is_trading_day, is_extended_market_open, reason_closed, session_of
from .models import Bar, Order, Side, Position
from .risk import RiskEngine
from .screener import Screener
from .strategy import (DayBreakout, DayMomentum, DayPullback, Ensemble,
                       MeanReversion, SwingTrend)
from .strategy.base import Strategy, StrategyContext
from .notify import ConsoleChannel, Notifier
from .registry import StrategyRegistry
from .streaming.base import StreamClient, StreamEvent
from .tracker import Prediction, PredictionTracker
from .market import now_kst

log = logging.getLogger("autotrader.live")


@dataclass
class CycleReport:
    ts: datetime
    candidates: int
    signals: int
    orders_placed: int
    orders_rejected: int
    closed_trades: int
    market_open: bool = True
    skipped_reason: str = ""
    stream_events: int = 0
    flat_closed: int = 0
    details: List[str] = field(default_factory=list)


class LiveTrader:
    def __init__(self, provider: DataProvider, broker: Broker, config: Config,
                 strategies: Optional[Sequence[Strategy]] = None,
                 ensemble_threshold: float = 0.55,
                 ensemble_min_votes: int = 1,
                 trail_pct: float = 0.05,
                 dry_run: bool = True,
                 registry: Optional[StrategyRegistry] = None,
                 validated_only: bool = False):
        self.provider = provider
        self.broker = broker
        self.config = config
        raw_strategies = list(strategies) if strategies else [
            DayBreakout(), DayPullback(), DayMomentum(), SwingTrend(), MeanReversion(),
        ]
        self.registry = registry
        self.validated_only = validated_only
        if validated_only and registry is not None:
            approved = set(registry.validated_names())
            self.strategies = [s_ for s_ in raw_strategies if s_.name in approved]
            if not self.strategies:
                log.warning("validated_only=True 인데 승인된 전략이 없습니다. "
                            "레지스트리에 전략을 등록하거나 --validated-only 를 끄세요.")
        else:
            self.strategies = raw_strategies
        self.ensemble = Ensemble(self.strategies, config.weights,
                                 threshold=ensemble_threshold,
                                 min_votes=ensemble_min_votes)
        self.trail_pct = trail_pct
        self.dry_run = dry_run
        self.risk = RiskEngine(config.risk)
        self.cooldown = CooldownRegistry(default_bars=config.risk.cooldown_bars_after_stop)
        self.tracker = PredictionTracker()
        # NXT 확장 세션 참여 여부. 두 값을 모두 False 로 두면 기존 KRX 정규장만 사용.
        self.allow_pre_market = False
        self.allow_after_market = False
        # 실시간 조건검색·체결 스트림. 없으면 폴링만 사용.
        self.stream: Optional[StreamClient] = None
        # 알림 채널: 기본은 조용, 사용자가 트래이더.notifier.add(...) 로 채운다.
        self.notifier: Notifier = Notifier()
        # 하루에 한 번만 EOD 청산 실행 보장
        self._flat_done_for: Optional[str] = None

    def _trail_for(self, bars, idx, price):
        """이 포지션에 쓸 트레일링 폭. `--trail 0` 이면 트레일링을 완전히 끈다.

        ATR 트레일은 별도 설정(`trail_atr_mult`)이라, 이 분기가 없으면
        `--trail 0` 을 줘도 종목별 ATR 폭이 그대로 붙는다 — 끄려고 준 옵션이
        안 꺼지는 것이라 실험 결과를 잘못 읽게 된다.
        """
        if not self.trail_pct:
            return None
        return ind.atr_trail_pct(bars, idx, price, self.config.execution)

    def cycle(self, now: Optional[datetime] = None) -> CycleReport:
        now = now or now_kst()
        report = CycleReport(ts=now, candidates=0, signals=0,
                             orders_placed=0, orders_rejected=0, closed_trades=0)

        # 0. 시장 세션 판정 — 휴장일이거나 프리·정규·애프터 어디에도 속하지 않으면 스킵.
        #    NXT 프리/애프터 참여 여부는 allow_pre_market / allow_after_market 로 조절.
        if not is_extended_market_open(now,
                                       include_pre=self.allow_pre_market,
                                       include_after=self.allow_after_market):
            report.market_open = False
            report.skipped_reason = f"session={session_of(now)}"
            return report

        self.cooldown.purge_expired(now.date())

        # EOD 일괄 청산 (v0.8): flat_at_time 이 설정되어 있고 지금이 그 시각을 넘었으며
        # 오늘 아직 안 했으면 보유 전량을 즉시 청산한다.
        flat_at = self.config.execution.flat_at_time
        day_key = now.date().isoformat()
        if (flat_at and self._flat_done_for != day_key
                and _time_reached(now, flat_at)
                and isinstance(self.broker, PaperBroker)):
            positions = self.broker.positions()
            if positions:
                prices: Dict[str, float] = {}
                for sym in positions:
                    try:
                        prices[sym] = self.provider.last_price(sym)
                    except Exception:
                        continue
                closed = self.broker.flat_all(prices, now, reason="eod_flat")
                report.flat_closed = len(closed)
                for tr in closed:
                    self.risk.register_exit(tr.pnl, now.date())
                    # 일괄 청산도 청산이다. 여기서 쿨다운을 안 걸면, 손실로
                    # 강제 정리한 종목에 다음 날 아침 바로 되들어간다.
                    self.cooldown.register_exit(tr.symbol, tr.exit_reason,
                                                now.date(), pnl=tr.pnl)
                    self.tracker.record_exit(
                        symbol=tr.symbol, exit_ts=tr.exit_ts,
                        exit_price=tr.exit_price, exit_reason=tr.exit_reason,
                    )
                self.notifier.info(f"[EOD] flat {len(closed)}건",
                                   body=", ".join(t.symbol for t in closed))
            self._flat_done_for = day_key

        # 1. Screener
        universe = self.config.universe.symbols or self.provider.universe()
        screen = Screener(self.provider, self.config.universe).rank(universe)
        candidates = [r for r in screen if r.passed]
        report.candidates = len(candidates)

        # 2. 현재 계좌 상태 — 브로커의 실제 잔고가 진실의 기준 (블로그 후기 개선판 ②).
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
            if self.cooldown.is_blocked(cand.symbol, now.date()):
                report.details.append(f"{cand.symbol}: cooldown")
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
                position_prices=prices,
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
                    # 백테스트와 같은 트레일 폭을 쓴다. 넘기지 않으면 계좌
                    # 기본값(고정 %)으로 돌아가 백테스트를 재현하지 못한다.
                    self.broker.submit(
                        order, price_hint=price, ts=now,
                        stop=dec.stop_hint, target=dec.target_hint,
                        trail=self._trail_for(bars, len(bars) - 1, price))
                else:
                    self.broker.submit(order, price_hint=price)
                report.orders_placed += 1
                # 같은 사이클 안에서 즉시 반영한다. 이게 없으면 후보 10개가
                # 전부 통과해 max_positions·gross_exposure·min_cash 가 한
                # 사이클 동안 무력해진다 — 한도가 있으나 마나가 된다.
                # 실브로커는 접수 ≠ 체결이므로 아직 positions() 에 안 뜬다.
                # 그래서 브로커에 되묻지 않고 "주문 나간 것" 을 잠정 보유로
                # 잡는다. 다음 사이클에 브로커 잔고가 이 값을 덮어쓴다.
                positions[cand.symbol] = Position(
                    symbol=cand.symbol, qty=decision.qty, avg_price=price,
                    opened_at=now, stop_price=dec.stop_hint,
                    take_price=dec.target_hint,
                )
                prices.setdefault(cand.symbol, price)
                self.risk.register_entry()
                self.tracker.record_entry(Prediction(
                    symbol=cand.symbol, entry_ts=now, entry_price=price,
                    confidence=dec.score, votes=dec.votes,
                    target_price=dec.target_hint, stop_price=dec.stop_hint,
                    reason=dec.signal.reason[:32], factor_detail=dict(dec.detail),
                ))
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
                                      max_hold=self.config.execution.max_holding_bars,
                                      hard_stop_pct=self.config.risk.hard_stop_loss_pct)
            report.closed_trades = len(closed)
            for tr in closed:
                self.risk.register_exit(tr.pnl, now.date())
                self.cooldown.register_exit(tr.symbol, tr.exit_reason, now.date(),
                                            pnl=tr.pnl)
                self.tracker.record_exit(
                    symbol=tr.symbol, exit_ts=tr.exit_ts,
                    exit_price=tr.exit_price, exit_reason=tr.exit_reason,
                )

        # 5. 실시간 스트림에서 방금 들어온 이벤트 소진.
        #    스트림이 붙어 있으면 조건검색 히트 종목을 즉시 앙상블 후보로 승격.
        if self.stream is not None:
            events = self.stream.drain()
            report.stream_events = len(events)
            for ev in events:
                if ev.kind != "signal" or not ev.symbol:
                    continue
                if ev.symbol in positions or self.cooldown.is_blocked(ev.symbol, now.date()):
                    continue
                try:
                    bars = self.provider.history(ev.symbol, self.config.universe.lookback_days)
                except Exception:
                    continue
                if len(bars) < 60:
                    continue
                dec = self.ensemble.evaluate(StrategyContext(ev.symbol, bars, len(bars) - 1))
                if dec.signal.side is not Side.BUY:
                    continue
                price = bars[-1].close
                decision = self.risk.evaluate_entry(
                    symbol=ev.symbol, price=price, stop_price=dec.stop_hint,
                    equity=equity, cash=self.broker.cash(),
                    positions=positions, score=dec.score,
                    position_prices=prices,
                )
                if not decision.allowed:
                    report.orders_rejected += 1
                    report.details.append(f"[stream] {ev.symbol}: {decision.reason}")
                    continue
                report.details.append(
                    f"[stream] {ev.symbol}: BUY x{decision.qty} @ {price:.2f}"
                )
                if self.dry_run:
                    continue
                try:
                    order = Order(ev.symbol, Side.BUY, decision.qty,
                                  tag=f"stream:{dec.signal.reason[:24]}")
                    if isinstance(self.broker, PaperBroker):
                        self.broker.submit(order, price_hint=price, ts=now,
                                           stop=dec.stop_hint, target=dec.target_hint)
                    else:
                        self.broker.submit(order, price_hint=price)
                    report.orders_placed += 1
                    self.risk.register_entry()
                    self.tracker.record_entry(Prediction(
                        symbol=ev.symbol, entry_ts=now, entry_price=price,
                        confidence=dec.score, votes=dec.votes,
                        target_price=dec.target_hint, stop_price=dec.stop_hint,
                        reason="stream", factor_detail=dict(dec.detail),
                    ))
                except Exception as exc:
                    report.orders_rejected += 1
                    report.details.append(f"[stream] {ev.symbol}: broker error {exc}")
        return report


def _time_reached(now: datetime, hhmm: str) -> bool:
    """now 의 시간(HH:MM)이 hhmm(예: "15:00") 을 넘었는지."""
    try:
        h, m = [int(x) for x in hhmm.split(":", 1)]
    except Exception:
        return False
    return (now.hour, now.minute) >= (h, m)
