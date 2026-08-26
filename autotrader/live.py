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
from .exits import evaluate_exit_live
from .broker.base import BrokerError
from .orderbook import OrderBook
from .orders import OrderStatus, entry_order_id, exit_order_id
from .portfolio import update_trailing_stop
from .recovery import (SessionState, reconcile_positions,
                       snapshot_positions)
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
                 validated_only: bool = False,
                 order_log: Optional[str] = None,
                 state_path: Optional[str] = None):
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
        # 미결 주문 장부. 경로를 주면 재시작 후에도 살아남는다.
        self.book = OrderBook(order_log)
        # 주문 id 에 섞는 전략 식별자. 앙상블 구성이 바뀌면
        # 같은 종목·같은 시각이라도 다른 주문으로 본다.
        self.ensemble_name = "+".join(sorted(s_.name for s_ in self.strategies))
        # 재시작을 건너뛰어야 하는 값들. 브로커에 물어볼 수 없는 것만 담는다.
        self.state_path = state_path

    # ---- 재시작 복구 ---------------------------------------------------

    def recover(self, now: Optional[datetime] = None) -> List[str]:
        """저장된 상태와 브로커 잔고를 합쳐 재시작 직후의 진실을 세운다.

        브로커가 이기는 것: 종목 존재 여부, 수량, 평균매입가, 현금.
        우리가 채우는 것: 손절가, 목표가, 트레일 폭, 진입 시각, 최고가.

        브로커는 우리 손절선을 모른다. 복구하지 않으면 재시작 이후 그 포지션에는
        **스탑이 영원히 걸리지 않는다.** 일일 진입 카운트도 0 부터 다시 세어져
        상한이 하루에 두 번 열린다.

        반환값은 사람이 읽어야 할 경고 목록이다. 조용히 넘기면 안 되는 것들만
        담긴다 — 특히 "브로커에는 있는데 우리 기록에 없는 종목".
        """
        now = now or now_kst()
        notes: List[str] = []
        state = SessionState.load(self.state_path) if self.state_path else SessionState()

        try:
            broker_positions = self.broker.positions()
        except Exception as exc:
            # 잔고를 못 읽으면 복구가 불가능하다. 빈 상태로 매매를 시작하면
            # 이미 들고 있는 종목에 또 들어간다 — 조용히 넘기면 안 된다.
            raise BrokerError(f"복구 실패: 브로커 잔고를 읽을 수 없습니다 ({exc})")

        res = reconcile_positions(broker_positions, state)
        notes.extend(res.notes)
        self.recovered_positions = res.positions

        # 페이퍼는 포트폴리오가 우리 안에 있으므로 직접 되살린다.
        if isinstance(self.broker, PaperBroker):
            for sym, pos in res.positions.items():
                self.broker.portfolio.positions[sym] = pos

        # 리스크 카운터. 날짜가 바뀌었으면 이어받지 않는다 — 어제 카운트를
        # 오늘로 가져오면 상한이 잘못된 방향으로 좁아진다.
        today = now.date()
        if state.day == today:
            self.risk.state.day = today
            self.risk.state.day_start_equity = state.day_start_equity
            self.risk.state.day_realized_pnl = state.day_realized_pnl
            self.risk.state.day_new_entries = state.day_new_entries
            self._flat_done_for = state.flat_done_for
            notes.append(
                f"[복구] 오늘 진입 {state.day_new_entries}건 · "
                f"실현손익 {state.day_realized_pnl:,.0f}원 이어받음")
        elif state.day is not None:
            notes.append(f"[복구] 저장된 상태는 {state.day} 자 — 오늘 카운터는 새로 시작")
        self.risk.state.consecutive_losses = state.consecutive_losses
        self.risk.state.cooldown_until = state.cooldown_until
        self.cooldown.entries.update(state.cooldowns)

        # 미결 주문. 장부는 생성자에서 이미 읽었다.
        open_orders = self.book.open_orders()
        if open_orders:
            notes.append(f"[복구] 미결 주문 {len(open_orders)}건 — "
                         f"체결 여부를 브로커에 확인해야 한다")
        for note in notes:
            log.info(note)
        return notes

    def save_state(self, now: Optional[datetime] = None) -> None:
        """종료 전(그리고 매 사이클 끝에) 상태를 남긴다.

        저장하지 않으면 다음 재시작에서 손절선이 사라진다.
        """
        if not self.state_path:
            return
        now = now or now_kst()
        try:
            positions = self.broker.positions()
        except Exception:
            positions = getattr(self, "recovered_positions", {}) or {}
        st = SessionState(
            day=self.risk.state.day,
            day_start_equity=self.risk.state.day_start_equity,
            day_realized_pnl=self.risk.state.day_realized_pnl,
            day_new_entries=self.risk.state.day_new_entries,
            consecutive_losses=self.risk.state.consecutive_losses,
            cooldown_until=self.risk.state.cooldown_until,
            flat_done_for=self._flat_done_for,
            cooldowns=dict(self.cooldown.entries),
            position_meta=snapshot_positions(
                self.broker.portfolio.positions
                if isinstance(self.broker, PaperBroker) else
                self._merged_positions(positions)),
        )
        st.save(self.state_path)

    def _merged_positions(self, broker_positions):
        """실브로커 잔고에 우리가 아는 손절선 등을 다시 입힌 것.

        브로커 잔고를 그대로 저장하면 stop_price 가 전부 None 이라, 저장할
        때마다 손절선을 스스로 지우게 된다.
        """
        known = getattr(self, "recovered_positions", {}) or {}
        out = {}
        for sym, bp in broker_positions.items():
            k = known.get(sym)
            if k is None:
                out[sym] = bp
                continue
            k.qty = bp.qty
            k.avg_price = bp.avg_price
            out[sym] = k
        return out

    def _dispatch_real_exits(self, positions, now: datetime, report) -> int:
        """실브로커: 청산 조건에 걸린 포지션에 매도 주문을 낸다.

        페이퍼처럼 "판정 즉시 청산 완료" 가 아니다. 주문을 내는 것까지가 여기
        할 일이고, 실제 청산은 체결통보가 와야 성립한다. 그래서 여기서는
        risk/cooldown/tracker 를 건드리지 않는다 — 체결도 안 됐는데 손익을
        기록하면 그것이 곧 유령 포지션이다.

        브로커가 스탑 주문을 직접 받아 주면 그쪽이 낫다(우리 프로세스가 죽어도
        살아 있다). 다만 지원 여부가 브로커마다 다르므로, 직접 감시해 청산
        주문을 내는 이 경로는 항상 필요하다.
        """
        sent = 0
        max_hold = self.config.execution.max_holding_bars
        hard = self.config.risk.hard_stop_loss_pct
        for sym, pos in list(positions.items()):
            try:
                price = self.provider.last_price(sym)
            except Exception as exc:
                # 시세를 못 받으면 청산 판정을 할 수 없다. 조용히 넘기면
                # 스탑이 걸려야 할 포지션이 방치된다 — 반드시 남긴다.
                report.details.append(f"[EXIT] {sym}: 시세 조회 실패 ({exc})")
                continue
            sig = evaluate_exit_live(pos, price, max_hold=max_hold,
                                     hard_stop_pct=hard)
            if sig is None:
                continue
            if self.dry_run:
                report.details.append(
                    f"[DRY][EXIT] SELL {sym} x{sig.qty} ({sig.reason}) @ {price:.0f}")
                continue
            order = Order(sym, Side.SELL, sig.qty, tag=sig.reason,
                          client_order_id=exit_order_id(sym, sig.reason, now,
                                                        pos.opened_at))
            try:
                bo = self.broker.submit(order, price_hint=price)
            except Exception as exc:
                report.details.append(f"[EXIT] {sym}: broker error {exc}")
                continue
            self.book.add(bo)
            if bo.status is OrderStatus.REJECTED:
                report.details.append(
                    f"[EXIT] {sym}: 청산 주문 거부 ({bo.reject_reason})")
                continue
            sent += 1
            report.details.append(
                f"[EXIT] SELL {sym} x{sig.qty} ({sig.reason}) @ {price:.0f}")
        return sent

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
            # 같은 신호로 두 번 주문이 나가지 않게 결정적 id 를 붙인다.
            # 재시도·재시작·스트림 중복 어디에서 와도 같은 id 가 나온다.
            coid = entry_order_id(self.ensemble_name, cand.symbol, now, Side.BUY)
            if self.book.get(coid) is not None:
                report.details.append(f"{cand.symbol}: 중복 주문 차단")
                continue
            order = Order(cand.symbol, Side.BUY, decision.qty,
                          tag=dec.signal.reason[:32], client_order_id=coid)
            if self.dry_run:
                report.details.append(f"[DRY] BUY {cand.symbol} x{decision.qty} @ {price:.2f}")
                continue
            try:
                if isinstance(self.broker, PaperBroker):
                    # 백테스트와 같은 트레일 폭을 쓴다. 넘기지 않으면 계좌
                    # 기본값(고정 %)으로 돌아가 백테스트를 재현하지 못한다.
                    bo = self.broker.submit(
                        order, price_hint=price, ts=now,
                        stop=dec.stop_hint, target=dec.target_hint,
                        trail=self._trail_for(bars, len(bars) - 1, price),
                        entry_score=dec.score, entry_votes=dec.votes)
                else:
                    bo = self.broker.submit(order, price_hint=price)
                self.book.add(bo)
                if bo.status is OrderStatus.REJECTED:
                    # 거부는 예외가 아니라 상태다. 여기서 걸러내지 않으면
                    # 거부된 주문이 보유 포지션으로 기록된다.
                    report.orders_rejected += 1
                    report.details.append(
                        f"{cand.symbol}: 주문 거부 ({bo.reject_reason})")
                    continue
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
                    entry_score=dec.score, entry_votes=dec.votes,
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

        # 4. 보유 포지션의 청산 규칙.
        #
        #    예전에는 이 블록 전체가 PaperBroker 전용이었다. 실브로커로 돌리면
        #    손절·트레일링·시간청산이 **하나도 동작하지 않았다** — 백테스트에서
        #    검증한 것과 전혀 다른 것이 실계좌에서 돈다는 뜻이다. 판정은
        #    exits 모듈이 하고, 집행 방식만 브로커별로 갈린다.
        if not isinstance(self.broker, PaperBroker):
            # 트레일링 스탑은 누군가 stop_price 를 끌어올려야 존재한다.
            # 페이퍼는 mark() 안에서 하지만 실계좌에는 그 경로가 없었다.
            if self.trail_pct > 0 and prices:
                for sym, pos in positions.items():
                    if sym in prices:
                        update_trailing_stop(pos, prices[sym], self.trail_pct)
            report.closed_trades += self._dispatch_real_exits(positions, now, report)
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
                                           stop=dec.stop_hint, target=dec.target_hint,
                                           entry_score=dec.score,
                                           entry_votes=dec.votes)
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
