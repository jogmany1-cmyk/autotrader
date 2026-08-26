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
from . import indicators as ind
from .config import Config
from .cooldown import CooldownRegistry
from .data.base import DataProvider
from .market import is_trading_day
from .metrics import CostAudit, PerformanceReport, build_cost_audit, performance_from
from .models import (Bar, EquityPoint, Fill, Order, Position, ScreenResult,
                     Side, Trade)
from .risk import RiskEngine
from .screener import Screener
from .strategy import (DayBreakout, DayMomentum, DayPullback, Ensemble,
                       MeanReversion, SwingTrend)
from .strategy.base import Strategy, StrategyContext
from .tracker import Prediction, PredictionTracker

CANDIDATE_SELECTION = "score-desc-symbol-asc"


@dataclass
class BacktestReport:
    train: PerformanceReport
    val: PerformanceReport
    oos: PerformanceReport
    all: PerformanceReport
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    screen_snapshot: List[ScreenResult] = field(default_factory=list)
    accuracy: Optional[object] = None      # tracker.AccuracyReport
    skipped_days: int = 0                  # 휴장일 등으로 건너뛴 봉 수
    cost_audit: Optional[CostAudit] = None
    entry_funnel: Dict[str, object] = field(default_factory=dict)


class Backtester:
    def __init__(self, provider: DataProvider, config: Config,
                 strategies: Optional[Sequence[Strategy]] = None,
                 ensemble_threshold: float = 0.55,
                 ensemble_min_votes: int = 1,
                 trail_pct: float = 0.05,
                 history_bars: Optional[int] = None,
                 trade_window: Optional[Tuple[datetime, datetime]] = None,
                 flat_at_window_end: bool = True,
                 score_mode: str = "all-weights"):
        self.provider = provider
        self.config = config
        # 거래 창 (닫힌 구간). 창 밖의 봉은 **지표 계산용 이력으로만** 쓰이고
        # 매매도 에쿼티 기록도 하지 않는다. rolling-origin 평가에서 각 구간을
        # 같은 초기자본·무포지션으로 시작시키기 위한 장치다
        # (docs/WALKFORWARD-SPEC.md 불변조건 1·2).
        #
        # 미래 봉 접근은 여전히 막혀 있다. 전략은 StrategyContext.at 까지만 보고,
        # 창 끝을 넘어선 봉은 루프가 break 로 끊어 아예 닿지 않는다.
        self.trade_window = trade_window
        # 창 끝에 남은 포지션을 청산할지. 남겨 두면 그 구간에서 벌인 거래 중
        # 결과가 나쁜 것들이 미청산 상태로 채점을 빠져나간다 — 손실을 창 밖에
        # 숨기는 셈이 된다. 기본값으로 청산한다.
        self.flat_at_window_end = flat_at_window_end
        # 종목당 불러올 봉 수. None 이면 기존 기본값(lookback_days × 4)을 쓴다.
        # 0 이면 있는 데이터 전부. 이 값이 곧 백테스트 구간의 길이가 된다.
        self.history_bars = history_bars
        self.strategies = list(strategies) if strategies else self._default_strategies()
        self.ensemble = Ensemble(self.strategies, config.weights,
                                 threshold=ensemble_threshold,
                                 min_votes=ensemble_min_votes,
                                 score_mode=score_mode)
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
    def _trail_for(self, bars, idx, price):
        """이 포지션에 쓸 트레일링 폭. `--trail 0` 이면 트레일링을 완전히 끈다.

        ATR 트레일은 별도 설정(`trail_atr_mult`)이라, 이 분기가 없으면
        `--trail 0` 을 줘도 종목별 ATR 폭이 그대로 붙는다 — 끄려고 준 옵션이
        안 꺼지는 것이라 실험 결과를 잘못 읽게 된다.
        """
        if not self.trail_pct:
            return None
        return ind.atr_trail_pct(bars, idx, price, self.config.execution)

    def run(self, symbols: Optional[Sequence[str]] = None) -> BacktestReport:
        u = self.config.universe
        symbols = list(symbols) if symbols else (u.symbols or self.provider.universe())

        # 1) 데이터 로딩과 시간축 정렬
        #
        # 종목당 몇 봉을 쓸지가 곧 백테스트 구간이다. 기본값 lookback_days × 4 는
        # "지표 워밍업(250봉) + 그 3배의 검증 구간" 이라는 뜻이지만, 40년치를
        # 수집해 놓고도 1,000봉만 쓰고 91% 를 버리게 된다. history_bars 로
        # 덮어쓸 수 있고, 0 이면 있는 데이터를 전부 쓴다.
        limit = (u.lookback_days * 4 if self.history_bars is None
                 else self.history_bars)
        bars_by_symbol: Dict[str, List[Bar]] = {}
        for s in symbols:
            try:
                bars_by_symbol[s] = self.provider.history(s, limit=limit)
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
        cooldown = CooldownRegistry(default_bars=self.config.risk.cooldown_bars_after_stop)
        tracker = PredictionTracker()
        equity_points: List[EquityPoint] = []
        # 종목별 지표 시리즈 캐시. 같은 종목의 모든 봉이 공유한다.
        # 없으면 매 봉마다 전체를 다시 계산해 봉 수의 제곱으로 느려진다.
        indicator_cache: Dict[str, Dict] = {}
        # (symbol, stop, target, tag, score, votes, detail)
        pending: List[Tuple[str, float, float, str, float, int, Dict[str, float]]] = []
        # 매수 신호가 실제 체결까지 어디서 줄었는지. 성과에는 관여하지 않는
        # 감사 계수이며, 신호 문제와 계좌 제약 문제를 구분하기 위해 남긴다.
        entry_funnel = {
            "strategy_evaluations": 0, "buy_signals": 0,
            "pending_attempts": 0, "entries_filled": 0,
            "no_next_bar": 0, "cooldown_blocked_at_fill": 0,
            "broker_errors": 0, "skipped_already_held": 0,
            "skipped_cooldown_before_signal": 0, "risk_rejections": {},
        }

        first_seen_close: Dict[str, float] = {}
        skipped_days = 0

        # 종목별 마지막 관측 종가. 창 끝 강제청산에서 쓴다 — 마지막 날에 봉이
        # 없는 종목도 청산되도록.
        latest_price: Dict[str, float] = {}
        last_ts = None
        for day_ix, ts in enumerate(timeline):
            # 거래 창 밖은 지표용 이력으로만 쓴다 — 매매·에쿼티 기록 없음.
            # 창 시작 전을 skipped_days 로 세지 않도록 휴장일 판정보다 앞에 둔다.
            if self.trade_window is not None:
                if ts < self.trade_window[0]:
                    continue
                if ts > self.trade_window[1]:
                    break
            # 휴장일이면 사이클 자체 스킵 (블로그 후기 개선판 ①)
            if not is_trading_day(ts.date()):
                skipped_days += 1
                continue

            todays_bars: Dict[str, Bar] = {}
            for sym in symbols:
                idx = _index_at(bars_by_symbol[sym], ts)
                if idx is None:
                    continue
                todays_bars[sym] = bars_by_symbol[sym][idx]
                first_seen_close.setdefault(sym, bars_by_symbol[sym][idx].close)

            if not todays_bars:
                continue

            cooldown.purge_expired(ts.date())

            # 2.1 대기 주문 체결(전일 신호 → 오늘 시가)
            # 포지션 자리가 부족할 때 파일/유니버스 순서가 종목을 결정하면 같은
            # 데이터도 입력 순서만 바꿔 성과가 달라진다. 전일 확정 점수가 높은
            # 후보부터, 동점이면 종목코드 순으로 처리한다.
            pending.sort(key=lambda row: (-row[4], row[0]))
            for sym, stop, target, tag, score, votes, detail in pending:
                entry_funnel["pending_attempts"] += 1
                bar = todays_bars.get(sym)
                if bar is None:
                    entry_funnel["no_next_bar"] += 1
                    continue
                if cooldown.is_blocked(sym, ts.date()):
                    entry_funnel["cooldown_blocked_at_fill"] += 1
                    continue
                price = bar.open
                positions = broker.positions()
                equity = broker.equity({s: b.close for s, b in todays_bars.items()})
                risk.new_day(ts.date(), equity)
                # 직전 봉 수익률 — chase filter 용
                sym_bars = bars_by_symbol[sym]
                sym_idx = _index_at(sym_bars, ts)
                last_ret = None
                if sym_idx is not None and sym_idx >= 1:
                    prev_close = sym_bars[sym_idx - 1].close
                    if prev_close > 0:
                        last_ret = price / prev_close - 1.0
                decision = risk.evaluate_entry(
                    symbol=sym, price=price, stop_price=stop,
                    equity=equity, cash=broker.cash(),
                    positions=positions, score=score,
                    last_bar_return=last_ret,
                    position_prices={s: b.close for s, b in todays_bars.items()},
                )
                if not decision.allowed:
                    reason = (decision.reason.split()[0]
                              if decision.reason else "unknown")
                    rejects = entry_funnel["risk_rejections"]
                    rejects[reason] = rejects.get(reason, 0) + 1
                    continue
                try:
                    broker.submit(
                        Order(sym, Side.BUY, decision.qty, tag=tag),
                        price_hint=price, ts=ts, stop=stop, target=target,
                        trail=self._trail_for(sym_bars, sym_idx - 1, price),
                        entry_score=score, entry_votes=votes,
                    )
                    risk.register_entry()
                    entry_funnel["entries_filled"] += 1
                    tracker.record_entry(Prediction(
                        symbol=sym, entry_ts=ts, entry_price=price,
                        confidence=score, votes=votes,
                        target_price=target, stop_price=stop,
                        reason=tag, factor_detail=dict(detail),
                    ))
                except Exception:
                    entry_funnel["broker_errors"] += 1
                    continue
            pending.clear()

            # 2.2 오늘의 청산 판정 (하드 스톱 → 스탑 → 타깃 → 시간 순).
            #     심볼 유형별 프로파일(ETF/개별주)이 있으면 그것에 맞춰 트레일/보유·손절폭 결정.
            #     구현 편의상 여기서는 계좌 기본값을 쓰고, per-symbol override 는
            #     backtest 진입 단계에서 stop_hint 에 이미 반영되어 있다.
            closed = broker.mark(
                todays_bars, ts,
                trail_pct=self.trail_pct,
                max_hold=self.config.execution.max_holding_bars,
                hard_stop_pct=self.config.risk.hard_stop_loss_pct,
            )
            for tr in closed:
                risk.register_exit(tr.pnl, ts.date())
                cooldown.register_exit(tr.symbol, tr.exit_reason, ts.date(),
                                       pnl=tr.pnl)
                tracker.record_exit(
                    symbol=tr.symbol, exit_ts=tr.exit_ts,
                    exit_price=tr.exit_price, exit_reason=tr.exit_reason,
                )

            # 2.3 오늘 종가 확정 후, 각 종목에 대해 앙상블 판단 → 내일 시가 진입 준비
            positions_now = broker.positions()
            equity_now = broker.equity({s: b.close for s, b in todays_bars.items()})
            risk.new_day(ts.date(), equity_now)
            for sym, bar in todays_bars.items():
                if sym in positions_now:
                    entry_funnel["skipped_already_held"] += 1
                    continue
                if cooldown.is_blocked(sym, ts.date()):
                    entry_funnel["skipped_cooldown_before_signal"] += 1
                    continue
                bars = bars_by_symbol[sym]
                idx = _index_at(bars, ts)
                if idx is None:
                    continue
                entry_funnel["strategy_evaluations"] += 1
                dec = self.ensemble.evaluate(
                    StrategyContext(sym, bars, idx,
                                    cache=indicator_cache.setdefault(sym, {})))
                if dec.signal.side is not Side.BUY:
                    continue
                entry_funnel["buy_signals"] += 1
                pending.append((
                    sym, dec.stop_hint, dec.target_hint,
                    dec.signal.reason[:40], dec.score, dec.votes, dec.detail,
                ))

            # 2.4 에쿼티 스냅샷
            prices = {s: b.close for s, b in todays_bars.items()}
            eq = broker.equity(prices)
            exposure = broker.portfolio.exposure(prices)
            equity_points.append(EquityPoint(ts=ts, equity=round(eq, 2),
                                             cash=round(broker.cash(), 2),
                                             exposure=round(exposure / eq, 4) if eq > 0 else 0.0))
            # 종목별 **마지막으로 관측된** 종가를 누적한다. 창 마지막 날에 봉이
            # 없는 종목(거래정지·상장폐지·희소 거래)도 청산 가격을 갖게 하려는
            # 것이다. 그날의 prices 만 쓰면 그런 종목이 청산되지 않은 채
            # 남는데, 리포트에는 청산된 것처럼 보여 손실이 사라진다.
            latest_price.update(prices)
            last_ts = ts

        # 마지막 날 종가 신호는 다음 봉이 창 밖이라 주문 시도 대상이 아니다.
        entry_funnel["unprocessed_at_window_end"] = len(pending)

        # 2.5 창 끝 강제 청산. 미청산 포지션을 남기면 결과가 나쁜 거래가
        #     채점을 빠져나가 손실이 창 밖에 숨는다.
        if (self.trade_window is not None and self.flat_at_window_end
                and last_ts is not None and broker.portfolio.positions):
            for tr in broker.flat_all(latest_price, last_ts, reason="window_end"):
                risk.register_exit(tr.pnl, last_ts.date())
                cooldown.register_exit(tr.symbol, tr.exit_reason, last_ts.date(),
                                       pnl=tr.pnl)
                tracker.record_exit(symbol=tr.symbol, exit_ts=tr.exit_ts,
                                    exit_price=tr.exit_price,
                                    exit_reason=tr.exit_reason)
            if equity_points:
                # 값을 강제하지 않고 브로커에서 다시 읽는다. 청산되지 못한
                # 포지션이 남아 있으면 exposure 가 0 이 아니어야 하고, 그
                # 사실이 리포트에 드러나야 한다.
                eq = broker.equity(latest_price)
                exposure = broker.portfolio.exposure(latest_price)
                equity_points[-1] = EquityPoint(
                    ts=last_ts, equity=round(eq, 2), cash=round(broker.cash(), 2),
                    exposure=round(exposure / eq, 4) if eq > 0 else 0.0)

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

        # 슬리피지는 리포트에만 더한다. 수익률에는 체결가(paper.py)를 통해 이미
        # 반영돼 있으므로 성과 계산 쪽은 건드리지 않는다 — 이중 차감 금지.
        cost = build_cost_audit(broker.fills, self.config.backtest.initial_cash,
                                self.config.costs.slippage_bp)
        return BacktestReport(
            train=report_train, val=report_val, oos=report_oos, all=report_all,
            trades=list(broker.portfolio.closed_trades),
            equity_curve=equity_points, screen_snapshot=screen,
            accuracy=tracker.report(), skipped_days=skipped_days,
            cost_audit=cost, entry_funnel=entry_funnel,
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

