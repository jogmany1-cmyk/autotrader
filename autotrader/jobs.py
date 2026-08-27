"""표준 자동매매 잡의 실제 액션들.

v0.8 스케줄러가 crontab 라인을 뽑아 주지만, 각 라인이 실행할 파이썬 명령이
없으면 무용지물이다. 이 모듈이 그 실행 담당이다. Cron 이 이 잡들을 시간대별로
호출하고, 이 잡들은 우리 시스템의 나머지 컴포넌트(LiveTrader · KiwoomProvider ·
PredictionTracker 등)를 조립해서 실제 일을 한다.

각 잡은 사이드이펙트가 있어 테스트가 어려우므로 얇게 유지한다. 세부 로직은
이미 각 컴포넌트에 있고 여기서는 조립만.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Callable, Dict, Optional

from .broker.paper import PaperBroker
from .config import Config, Costs, KiwoomConfig
from .data import CsvProvider
from .data.base import DataError, DataProvider
from .live import LiveTrader
from .notify import ConsoleChannel, Notifier
from .registry import StrategyRegistry
from .market import now_kst

log = logging.getLogger("autotrader.jobs")

#: 승격 경로가 요구하는 모의투자 최소 기간. CLAUDE.md 의 "새로운 시도는 새
#: 데이터로만 한다 — 최소 60거래일 모의투자" 를 코드로 옮긴 것.
TARGET_SESSIONS = 60


class JobFailed(Exception):
    """잡이 실패했음을 크론에 알린다.

    크론은 종료코드만 본다. 실패를 문자열로 돌려주고 0 으로 끝내면 아무도
    모른 채 다음 잡이 이어진다 — 특히 데이터 무결성 게이트가 그렇게 되면
    깨진 데이터가 모의투자로 그대로 흘러 들어간다.
    """


class JobContext:
    """잡 실행에 필요한 공통 객체를 한곳에서 조립."""

    def __init__(self, cache_dir: str = "./data/kiwoom",
                 registry_path: Optional[str] = None,
                 use_kiwoom: bool = True,
                 runs_dir: str = "./runs"):
        self.cache_dir = cache_dir
        self.registry_path = registry_path
        self.use_kiwoom = use_kiwoom
        self.runs_dir = runs_dir
        self.notifier = Notifier([ConsoleChannel()])
        self._provider: Optional[DataProvider] = None
        self._config: Optional[Config] = None

    def config(self) -> Config:
        if self._config is None:
            self._config = Config.default()
        return self._config

    def provider(self) -> DataProvider:
        """캐시 CSV 를 우선 사용. 키움 자격증명이 있으면 KiwoomProvider."""
        if self._provider is not None:
            return self._provider
        if self.use_kiwoom:
            try:
                from .data import KiwoomProvider
                self._provider = KiwoomProvider(
                    KiwoomConfig.from_env(), cache_dir=self.cache_dir,
                )
                return self._provider
            except DataError:
                log.warning("Kiwoom 자격증명 없음 → CsvProvider 로 폴백")
        self._provider = CsvProvider(self.cache_dir)
        return self._provider

    def registry(self) -> Optional[StrategyRegistry]:
        if not self.registry_path:
            return None
        return StrategyRegistry(self.registry_path)

    # --- 세션 산출물 경로 ------------------------------------------------
    # 한곳에서 파생시킨다. 잡마다 따로 조립하면 크론 라인과 어긋나서,
    # 계좌는 A 에 쓰고 리포트는 B 를 읽는 상태가 조용히 생긴다.

    @property
    def account_path(self) -> str:
        return os.path.join(self.runs_dir, "account.json")

    @property
    def state_path(self) -> str:
        return os.path.join(self.runs_dir, "state.json")

    @property
    def order_log_path(self) -> str:
        return os.path.join(self.runs_dir, "orders.jsonl")

    @property
    def journal_path(self) -> str:
        """세션 일지. 하루 한 줄씩 append 되며 60거래일 진척을 센다."""
        return os.path.join(self.runs_dir, "sessions.jsonl")


# ------------------------------------------------------- 잡 액션들 -------

def job_morning_entry(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """09:30 진입 사이클 — LiveTrader.cycle() 한 번 실행."""
    now = now or now_kst()
    cfg = ctx.config()
    provider = ctx.provider()
    if not cfg.universe.symbols:
        cfg.universe.symbols = provider.universe()
    broker = PaperBroker(cfg.backtest.initial_cash, cfg.costs)
    trader = LiveTrader(provider, broker, cfg,
                        registry=ctx.registry(),
                        validated_only=ctx.registry() is not None,
                        dry_run=True)
    trader.notifier = ctx.notifier
    rep = trader.cycle(now=now)
    msg = (f"morning-entry: market={'open' if rep.market_open else 'closed'} "
           f"cand={rep.candidates} sig={rep.signals} placed={rep.orders_placed}")
    ctx.notifier.info(msg)
    return msg


def job_eod_flat(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """15:00 EOD 일괄 청산 — flat_at_time 을 지금으로 강제 세팅 후 사이클."""
    now = now or now_kst()
    cfg = ctx.config()
    cfg.execution.flat_at_time = now.strftime("%H:%M")
    provider = ctx.provider()
    if not cfg.universe.symbols:
        cfg.universe.symbols = provider.universe()
    broker = PaperBroker(cfg.backtest.initial_cash, cfg.costs)
    trader = LiveTrader(provider, broker, cfg, dry_run=True)
    trader.notifier = ctx.notifier
    rep = trader.cycle(now=now)
    msg = f"eod-flat: closed={rep.flat_closed}"
    ctx.notifier.info(msg)
    return msg


def job_collect_daily(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """장 마감 후 일봉 수집 — KiwoomProvider.refresh_all()."""
    prov = ctx.provider()
    if not hasattr(prov, "refresh_all"):
        msg = "collect-daily: KiwoomProvider 아님 (스킵)"
        ctx.notifier.warn(msg)
        return msg
    ok, fail = prov.refresh_all(limit=500)  # type: ignore[attr-defined]
    msg = f"collect-daily: ok={ok} fail={fail}"
    ctx.notifier.info(msg)
    return msg


def job_collect_5m(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """5분봉 수집 — KiwoomProvider.refresh_minutes(interval=5)."""
    prov = ctx.provider()
    if not hasattr(prov, "refresh_minutes"):
        msg = "collect-5m: KiwoomProvider 아님 (스킵)"
        ctx.notifier.warn(msg)
        return msg
    ok, fail = prov.refresh_minutes(interval=5, limit=500)  # type: ignore[attr-defined]
    msg = f"collect-5m: ok={ok} fail={fail}"
    ctx.notifier.info(msg)
    return msg


def job_post_analysis(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """장 마감 후 사후 리포트 — 오늘 청산된 트레이드·정확도 요약."""
    reg = ctx.registry()
    if reg is None:
        msg = "post-analysis: registry 없음 (승인 전략 없음)"
    else:
        names = reg.validated_names()
        msg = f"post-analysis: validated={len(names)} strategies={','.join(names) or '<none>'}"
    ctx.notifier.info(msg)
    return msg



# ---------------------------------------------- 60거래일 모의투자 세트 ----
#
# 아래 세 잡이 "새 데이터로만 다시 시도한다" 는 결정을 실제로 굴리는 축이다.
# 순서가 곧 승격 경로다: 수집 → 무결성 게이트 → 모의매매.
#
# 위쪽의 morning-entry / eod-flat 은 **매일 전량청산 데이트레이딩**을 전제한다.
# 그 회전율(연 343배)이면 왕복비용만으로 연 57~125% 를 내야 해서, 우리가
# 측정한 우위(거래당 -27.9bp ~ +8.0bp)로는 성립하지 않는다. 남겨는 두되
# 표준 스케줄에서는 뺐다 — docs/STRATEGY-RESET-2026-08-26.md 참고.


def job_validate_data(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """데이터 무결성 게이트. 결함이 있으면 JobFailed 로 크론을 멈춘다.

    CsvProvider 는 파싱 실패한 행을 조용히 버리고 정렬까지 해버린다. 이
    게이트가 없으면 깨진 데이터가 아무 불평 없이 모의투자에 도달하고,
    60거래일을 쌓은 뒤에야 결과를 못 믿게 된다.
    """
    from .dataquality import DataQualityChecker, QualityLimits

    rep = DataQualityChecker(QualityLimits()).check_csv_dir(ctx.cache_dir, None)
    if not rep.symbols and not rep.unreadable:
        raise JobFailed(f"validate-data: 검사할 CSV 가 없습니다 ({ctx.cache_dir}) "
                        "— 수집이 먼저 돌았는지 확인하세요")
    if not rep.passed(strict=False):
        codes = ", ".join(f"{k}={v}" for k, v in rep.counts_by_code().items())
        raise JobFailed(f"validate-data: FAIL — {rep.summary()} [{codes}]")
    msg = f"validate-data: PASS — {rep.summary()}"
    ctx.notifier.info(msg)
    return msg


def _mark_prices(provider: DataProvider, symbols) -> Dict[str, float]:
    """보유 종목의 최신 종가. 못 구하면 그 종목만 빠진다(평단가로 평가됨)."""
    out: Dict[str, float] = {}
    for sym in symbols:
        try:
            bars = provider.history(sym, limit=2)
        except DataError:
            continue
        if bars:
            out[sym] = bars[-1].close
    return out


def job_paper_session(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """하루치 모의매매 한 세션. 계좌·상태·주문장부가 프로세스를 건넌다.

    승인된 전략만 돌린다. 레지스트리가 없거나 승인된 전략이 없으면 실패로
    끝낸다 — 조용히 아무것도 안 하고 0 으로 끝나면, 60일 뒤에 빈 계좌를 보고
    "전략이 나빴다" 고 오진하게 된다. 실제로는 시작조차 안 한 것이다.
    """
    now = now or now_kst()
    cfg = ctx.config()
    provider = ctx.provider()
    reg = ctx.registry()
    if reg is None:
        raise JobFailed(
            "paper-session: --registry 가 필요합니다. 승인되지 않은 전략으로 "
            "60거래일을 쌓아도 승격 경로에서 쓸 수 없습니다")
    approved = reg.validated_names()
    if not approved:
        raise JobFailed(
            "paper-session: 레지스트리에 승인된 전략이 없습니다. "
            "`autotrader validate --registry ...` 로 먼저 확인하세요. "
            "(폐기된 일봉 5종은 되살리지 않습니다 — CLAUDE.md 참고)")

    if not cfg.universe.symbols:
        cfg.universe.symbols = provider.universe()
    broker = PaperBroker(cfg.backtest.initial_cash, cfg.costs)
    trader = LiveTrader(provider, broker, cfg,
                        registry=reg, validated_only=True, dry_run=False,
                        order_log=ctx.order_log_path,
                        state_path=ctx.state_path,
                        account_path=ctx.account_path)
    trader.notifier = ctx.notifier
    for note in trader.recover(now=now):
        log.info(note)

    rep = trader.cycle(now=now)
    trader.save_state(now=now)

    prices = _mark_prices(provider, broker.positions().keys())
    equity = broker.equity(prices)
    row = {
        "date": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
        "market_open": rep.market_open,
        "equity": round(equity, 2),
        "cash": round(broker.cash(), 2),
        "positions": len(broker.positions()),
        "closed_trades_total": len(broker.portfolio.closed_trades),
        "signals": rep.signals,
        "placed": rep.orders_placed,
        "rejected": rep.orders_rejected,
        "strategies": approved,
    }
    _append_journal(ctx.journal_path, row)
    sessions = _count_sessions(ctx.journal_path)
    msg = (f"paper-session: {sessions}/{TARGET_SESSIONS}거래일 "
           f"equity={equity:,.0f} pos={row['positions']} "
           f"sig={rep.signals} placed={rep.orders_placed}")
    ctx.notifier.info(msg)
    return msg


def job_session_report(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """모의투자 진척과 누적 성과. 60거래일에 도달하면 그 사실을 알린다."""
    rows = _read_journal(ctx.journal_path)
    if not rows:
        msg = "session-report: 세션 기록이 없습니다 (paper-session 이 아직 안 돌았습니다)"
        ctx.notifier.warn(msg)
        return msg
    traded = [r for r in rows if r.get("market_open")]
    sessions = len({r["date"] for r in traded})
    first, last = rows[0], rows[-1]
    start_equity = ctx.config().backtest.initial_cash
    ret = (last["equity"] / start_equity - 1.0) if start_equity else 0.0
    done = "  ** 60거래일 도달 — 결과를 판정할 수 있습니다 **" if sessions >= TARGET_SESSIONS else ""
    msg = (f"session-report: {sessions}/{TARGET_SESSIONS}거래일 "
           f"({first['date']} ~ {last['date']}) "
           f"equity={last['equity']:,.0f} ({ret:+.2%}) "
           f"청산 {last.get('closed_trades_total', 0)}건{done}")
    ctx.notifier.info(msg)
    return msg


# ---------------------------------------------------------- 세션 일지 ----

def _append_journal(path: str, row: Dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_journal(path: str):
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                # 한 줄이 깨져도 나머지는 살린다. append 중 정전이면 마지막
                # 줄만 반쪽이 된다 — 그것 때문에 60일치를 잃을 이유는 없다.
                log.warning("세션 일지에서 못 읽은 줄을 건너뜁니다: %s", path)
    return rows


def _count_sessions(path: str) -> int:
    """장이 열린 날만 센다. 휴장일에 크론이 돌아도 진척이 되면 안 된다."""
    return len({r["date"] for r in _read_journal(path) if r.get("market_open")})


# ------------------------------------------------------- 디스패처 --------

JOBS: Dict[str, Callable[[JobContext, Optional[datetime]], str]] = {
    # 60거래일 모의투자 세트 — 표준 스케줄이 쓰는 것
    "validate-data": job_validate_data,
    "paper-session": job_paper_session,
    "session-report": job_session_report,
    "collect-daily": job_collect_daily,
    # 데이트레이딩 잡 — 표준 스케줄에서는 빠졌다. 회전율이 성립하지 않는다.
    "morning-entry": job_morning_entry,
    "eod-flat": job_eod_flat,
    "collect-5m": job_collect_5m,
    "post-analysis": job_post_analysis,
}


def run(name: str, ctx: Optional[JobContext] = None,
        now: Optional[datetime] = None) -> str:
    """이름으로 잡을 실행. 알 수 없는 이름이면 KeyError."""
    if name not in JOBS:
        raise KeyError(f"알 수 없는 잡: {name}. 등록된 잡: {list(JOBS)}")
    return JOBS[name](ctx or JobContext(), now)
