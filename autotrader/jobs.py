"""표준 자동매매 잡의 실제 액션들.

v0.8 스케줄러가 crontab 라인을 뽑아 주지만, 각 라인이 실행할 파이썬 명령이
없으면 무용지물이다. 이 모듈이 그 실행 담당이다. Cron 이 이 잡들을 시간대별로
호출하고, 이 잡들은 우리 시스템의 나머지 컴포넌트(LiveTrader · KiwoomProvider ·
PredictionTracker 등)를 조립해서 실제 일을 한다.

각 잡은 사이드이펙트가 있어 테스트가 어려우므로 얇게 유지한다. 세부 로직은
이미 각 컴포넌트에 있고 여기서는 조립만.
"""
from __future__ import annotations

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

log = logging.getLogger("autotrader.jobs")


class JobContext:
    """잡 실행에 필요한 공통 객체를 한곳에서 조립."""

    def __init__(self, cache_dir: str = "./data/kiwoom",
                 registry_path: Optional[str] = None,
                 use_kiwoom: bool = True):
        self.cache_dir = cache_dir
        self.registry_path = registry_path
        self.use_kiwoom = use_kiwoom
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


# ------------------------------------------------------- 잡 액션들 -------

def job_morning_entry(ctx: JobContext, now: Optional[datetime] = None) -> str:
    """09:30 진입 사이클 — LiveTrader.cycle() 한 번 실행."""
    now = now or datetime.utcnow()
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
    now = now or datetime.utcnow()
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


# ------------------------------------------------------- 디스패처 --------

JOBS: Dict[str, Callable[[JobContext, Optional[datetime]], str]] = {
    "morning-entry": job_morning_entry,
    "eod-flat": job_eod_flat,
    "collect-daily": job_collect_daily,
    "collect-5m": job_collect_5m,
    "post-analysis": job_post_analysis,
}


def run(name: str, ctx: Optional[JobContext] = None,
        now: Optional[datetime] = None) -> str:
    """이름으로 잡을 실행. 알 수 없는 이름이면 KeyError."""
    if name not in JOBS:
        raise KeyError(f"알 수 없는 잡: {name}. 등록된 잡: {list(JOBS)}")
    return JOBS[name](ctx or JobContext(), now)
