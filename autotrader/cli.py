"""명령줄 진입점.

  python -m autotrader backtest [--csv DIR] [--config PATH] [--top N]
  python -m autotrader screen   [--csv DIR] [--config PATH] [--top N]
  python -m autotrader signal   [--csv DIR] [--config PATH] [--symbol S]
  python -m autotrader paper    [--csv DIR] [--config PATH] [--cycles N]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from .backtest import Backtester
from .broker import PaperBroker
from .config import Config
from .data import CsvProvider, SyntheticProvider
from .data.base import DataProvider
from .live import LiveTrader
from .registry import StrategyRegistry
from .screener import Screener


def _provider(csv_dir: Optional[str]) -> DataProvider:
    if csv_dir:
        p = CsvProvider(csv_dir)
        if not p.universe():
            raise SystemExit(f"CSV 유니버스가 비어 있습니다: {csv_dir}")
        return p
    return SyntheticProvider()


def _config(path: Optional[str], provider: DataProvider) -> Config:
    cfg = Config.load(path) if path else Config.default()
    if not cfg.universe.symbols:
        cfg.universe.symbols = provider.universe()
    return cfg


def cmd_backtest(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    # 데모 데이터에서는 진입장벽을 낮춰야 신호가 잡힌다.
    if isinstance(provider, SyntheticProvider):
        cfg.universe.min_price = 0
        cfg.universe.min_avg_dollar_vol = 0
    bt = Backtester(provider, cfg,
                    ensemble_threshold=args.threshold,
                    ensemble_min_votes=args.votes,
                    trail_pct=args.trail,
                    history_bars=args.bars)
    rep = bt.run()
    print("== 전체 성과 =====================================")
    _dump_report(rep.all)
    print("== TRAIN =========================================")
    _dump_report(rep.train)
    print("== VALIDATION ====================================")
    _dump_report(rep.val)
    print("== OUT-OF-SAMPLE  (실제 판단 근거) ================")
    _dump_report(rep.oos)
    print(f"trades={len(rep.trades)}  bars={len(rep.equity_curve)}  "
          f"skipped(holiday)={rep.skipped_days}")
    if rep.cost_audit is not None and rep.cost_audit.n_fills:
        print("== 비용 감사 (실패 사례에서 배운 항목) =================")
        c = rep.cost_audit
        print(f"  {c.as_line()}")
        print(f"  총 매매대금 {c.total_gross_volume:>16,.0f}")
        print(f"  총 수수료   {c.total_fees:>16,.2f}")
        print(f"  총 거래세   {c.total_taxes:>16,.2f}")
        print(f"  평균 체결   {c.avg_trade_size:>16,.0f}")
    if rep.accuracy is not None and getattr(rep.accuracy, "n", 0):
        a = rep.accuracy
        print("== AI 예측 정확도 ================================")
        print(f"  n={a.n}  win_rate={a.win_rate:.3f}  avg_return={a.avg_return:.4f}  "
              f"target_hit={a.target_hit_rate:.3f}  stop_hit={a.stop_hit_rate:.3f}")
        for k, v in a.by_confidence_bucket.items():
            print(f"    conf {k:<6} n={v['n']:>3}  win={v['win_rate']:.3f}  ret={v['avg_return']:+.4f}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump({
                "all": rep.all.to_dict(),
                "train": rep.train.to_dict(),
                "val": rep.val.to_dict(),
                "oos": rep.oos.to_dict(),
                "n_trades": len(rep.trades),
                "n_bars": len(rep.equity_curve),
            }, fh, indent=2, ensure_ascii=False)
    return 0


def cmd_screen(args) -> int:
    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    if isinstance(provider, SyntheticProvider):
        cfg.universe.min_price = 0
        cfg.universe.min_avg_dollar_vol = 0
    screener = Screener(provider, cfg.universe, top_n=args.top)
    sc = screener.rank()
    if screener.last_stats is not None:
        print(screener.last_stats.as_line())
    print(f"{'RANK':<5}{'SYMBOL':<10}{'SCORE':>8}   factors")
    for i, r in enumerate([x for x in sc if x.passed], 1):
        f = " ".join(f"{k}={v:+.2f}" for k, v in r.factors.items())
        print(f"{i:<5}{r.symbol:<10}{r.score:>8.3f}   {f}")
    rejects = [x for x in sc if not x.passed]
    if rejects:
        print("\n제외:", ", ".join(f"{r.symbol}({r.reject_reason})" for r in rejects))
    return 0


def cmd_signal(args) -> int:
    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    from .strategy import (DayBreakout, DayPullback, DayMomentum, SwingTrend,
                           MeanReversion, Ensemble)
    from .strategy.base import StrategyContext
    strats = [DayBreakout(), DayPullback(), DayMomentum(), SwingTrend(), MeanReversion()]
    ens = Ensemble(strats, cfg.weights,
                   threshold=args.threshold, min_votes=args.votes)
    syms = [args.symbol] if args.symbol else provider.universe()
    for sym in syms:
        try:
            bars = provider.history(sym, cfg.universe.lookback_days)
        except Exception:
            continue
        if len(bars) < 60:
            continue
        dec = ens.evaluate(StrategyContext(sym, bars, len(bars) - 1))
        tag = "BUY" if dec.signal.side.value == "BUY" else "----"
        print(f"{sym:<8} {tag}  score={dec.score:.2f} votes={dec.votes}  "
              f"stop={dec.stop_hint:.0f}  target={dec.target_hint:.0f}  "
              f"reason={dec.signal.reason}")
    return 0


def cmd_paper(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    if isinstance(provider, SyntheticProvider):
        cfg.universe.min_price = 0
        cfg.universe.min_avg_dollar_vol = 0
    broker = PaperBroker(cfg.backtest.initial_cash, cfg.costs)
    reg = StrategyRegistry(args.registry) if args.registry else None
    trader = LiveTrader(provider, broker, cfg,
                        ensemble_threshold=args.threshold,
                        ensemble_min_votes=args.votes,
                        trail_pct=args.trail, dry_run=args.dry_run,
                        registry=reg, validated_only=args.validated_only)
    trader.allow_pre_market = args.allow_pre_market
    trader.allow_after_market = args.allow_after_market
    if reg is not None:
        active = ", ".join(s_.name for s_ in trader.strategies) or "<none>"
        print(f"[REGISTRY] validated_only={args.validated_only}  active={active}")
    for i in range(args.cycles):
        rep = trader.cycle()
        state = "closed" if not rep.market_open else "open"
        print(f"[{i+1}] market={state} cand={rep.candidates} sig={rep.signals} "
              f"placed={rep.orders_placed} rej={rep.orders_rejected} "
              f"closed={rep.closed_trades}")
        for line in rep.details[:5]:
            print(f"    · {line}")
    acc = trader.tracker.report()
    if acc.n:
        print(f"\n예측 정확도: n={acc.n} 승률={acc.win_rate:.2f} "
              f"평균수익={acc.avg_return:+.3f} 목표달성률={acc.target_hit_rate:.2f}")
    return 0


def cmd_fetch(args) -> int:
    """키움 REST API 로 종목 시세를 캐시 폴더에 자동 수집.
    자격증명은 환경변수(KIWOOM_APP_KEY / KIWOOM_APP_SECRET / KIWOOM_MODE)로."""
    from .config import KiwoomConfig
    from .data import KiwoomProvider
    from .data.base import DataError

    cfg = KiwoomConfig.from_env()
    if args.real:
        cfg.is_paper = False
    try:
        provider = KiwoomProvider(cfg, cache_dir=args.cache)
    except DataError as exc:
        print(f"[ERROR] {exc}")
        return 2

    provider.debug = args.debug
    if args.min_interval is not None:   # 미지정이면 모드별 기본값을 그대로 둔다
        provider.min_interval = args.min_interval
    symbols = args.symbol or provider.universe()
    kind = f"{args.minutes}m" if args.minutes else "daily"
    print(f"[FETCH] {len(symbols)}개 심볼 · {kind} → 캐시 {args.cache} "
          f"(mode={'real' if args.real else 'paper'})")
    if args.minutes:
        ok, fail = provider.refresh_minutes(symbols, interval=args.minutes, limit=args.limit)
    else:
        ok, fail = provider.refresh_all(symbols, limit=args.limit)
    print(f"[DONE ] ok={ok} fail={fail}")
    # 실패 개수만 찍고 끝내면 무엇이 잘못됐는지 알 수 없다. 사유를 그대로 보여준다.
    for sym, reason in provider.last_failures[:20]:
        print(f"  [FAIL] {sym}: {reason}")
    if len(provider.last_failures) > 20:
        print(f"  … 그 외 {len(provider.last_failures) - 20}건")
    return 0 if fail == 0 else 1


def cmd_run_job(args) -> int:
    """v0.8 스케줄러가 crontab 에서 호출할 표준 잡 실행."""
    from .jobs import JobContext, run
    ctx = JobContext(cache_dir=args.cache, registry_path=args.registry)
    try:
        msg = run(args.name, ctx)
    except KeyError as exc:
        print(f"[ERROR] {exc}")
        return 2
    print(msg)
    return 0


def cmd_validate(args) -> int:
    reg = StrategyRegistry(args.registry)
    th = reg.thresholds
    print(f"승인 기준: PF>={th.min_oos_profit_factor} trades>={th.min_oos_trades} "
          f"net>{th.min_oos_net_return:+.2%} DD>={th.max_oos_drawdown} "
          f"age<={th.max_age_days}d")
    validated = set(reg.validated_names())
    for rec in reg.all_records():
        mark = "PASS" if rec.name in validated else "FAIL"
        net = ("미측정" if rec.oos_net_return is None
               else f"{rec.oos_net_return:+.2%}")
        print(f"  [{mark}] {rec.name:<20} PF={rec.oos_profit_factor:.2f} "
              f"trades={rec.oos_trades:>4} net={net:>8} "
              f"DD={rec.oos_max_drawdown:+.3f}")
    return 0


def cmd_validate_data(args) -> int:
    """실데이터 무결성 검사. 승격 경로 2단계의 실행 가능한 게이트.

    종료코드 0 = 통과, 1 = 결함 발견(ERROR, --strict 면 WARN 포함), 2 = 대상 없음.
    크론이나 CI 에서 `autotrader --csv data/kiwoom validate-data || exit 1` 로
    쓰면 더러운 데이터가 백테스트로 흘러 들어가는 것을 막을 수 있다.
    """
    from datetime import date as _date

    from .dataquality import ERROR, DataQualityChecker, QualityLimits

    limits = QualityLimits(min_bars=args.min_bars, jump_pct=args.jump_pct,
                           long_gap_days=args.long_gap_days,
                           stale_days=args.stale_days)
    as_of = _date.fromisoformat(args.as_of) if args.as_of else None
    checker = DataQualityChecker(limits, as_of=as_of)

    if args.csv:
        rep = checker.check_csv_dir(args.csv, args.symbol, limit=args.bars)
        source = args.csv
    else:
        provider = SyntheticProvider()
        rep = checker.check_provider(provider, args.symbol, limit=args.bars)
        source = "합성 데이터 (실데이터 검증에는 --csv 를 쓰세요)"
    if not rep.symbols and not rep.unreadable:
        print(f"[ERROR] 검사할 종목이 없습니다: {source}")
        return 2

    print(f"== 데이터 무결성 검사 ({source}, 기준일 {rep.as_of}) ==")
    print(f"  {rep.summary()}")
    for sym, reason in rep.unreadable:
        print(f"  [ERROR] {sym:<10} unreadable             -  {reason}")
    shown = 0
    for sym_rep in rep.symbols:
        for issue in sorted(sym_rep.issues, key=lambda i: (i.severity != ERROR,)):
            if args.show and shown >= args.show:
                break
            print("  " + issue.as_line())
            shown += 1
        if args.show and shown >= args.show:
            break
    # n_errors/n_warnings 는 발생 "횟수" 라 묶인 항목까지 센다. 여기서 세야 하는
    # 것은 아직 출력하지 못한 "줄 수" 이므로 all_issues 길이를 기준으로 삼는다.
    remaining = len(rep.all_issues) - shown
    if args.show and remaining > 0:
        print(f"  … 그 외 {remaining}건 (--show 0 으로 전체 출력)")
    if rep.all_issues:
        print("  코드별 집계: " + ", ".join(f"{k}={v}" for k, v in rep.counts_by_code().items()))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(rep.as_dict(), fh, ensure_ascii=False, indent=2)
        print(f"  → {args.output} 에 상세 리포트 저장")

    ok = rep.passed(strict=args.strict)
    print(f"  판정: {'PASS' if ok else 'FAIL'}"
          f"{' (--strict: WARN 도 불허)' if args.strict else ''}")
    if not ok:
        print("  이 데이터로는 백테스트 결과를 신뢰할 수 없습니다. "
              "원본을 고치거나 다시 수집하세요.")
    return 0 if ok else 1


def cmd_schedule(args) -> int:
    """실전 자동매매 표준 크론잡 세트를 crontab 형식으로 출력.
    이 라인들을 `crontab -e` 로 등록하거나 systemd timer 로 변환해 쓴다."""
    from .scheduler import JobRegistry
    reg = JobRegistry()
    reg.register("collect-5m", "*/5 9-15 * * 0-4", lambda t: None,
                 description="평일 장중 5분봉 수집")
    reg.register("collect-daily", "45 15 * * 0-4", lambda t: None,
                 description="장 마감 후 일봉 수집")
    reg.register("morning-entry", "30 9 * * 0-4", lambda t: None,
                 description="09:30 진입 사이클")
    reg.register("eod-flat", "0 15 * * 0-4", lambda t: None,
                 description="15:00 EOD 일괄 청산")
    reg.register("post-analysis", "30 15 * * 0-4", lambda t: None,
                 description="장 마감 후 사후 분석 리포트")
    print("# autotrader 표준 자동매매 크론잡 (crontab -e 에 붙여 넣기)")
    for line in reg.crontab_lines(prefix_command=args.prefix):
        print(line)
    return 0


def cmd_reconcile(args) -> int:
    from .reconciler import SourceReconciler
    a = CsvProvider(args.primary)
    b = CsvProvider(args.secondary)
    universe = sorted(set(a.universe()) | set(b.universe()))
    if not universe:
        raise SystemExit("두 원천 어디에도 종목이 없습니다")
    # 데모용 조건: 최근 종가 > 20일 전 종가 (실전에서는 사용자 조건식으로 대체)
    def predicate(view):
        bars = view.bars()
        if len(bars) < 21:
            return False
        return bars[-1].close > bars[-21].close
    report = SourceReconciler(a, b).reconcile(universe, predicate)
    print(report.summary())
    if report.only_in_secondary:
        print("누수 후보(only in secondary):", ", ".join(report.only_in_secondary[:20]))
    return 0


def _dump_report(rep) -> None:
    d = rep.to_dict()
    order = ["n_trades", "win_rate", "net_return", "cagr", "max_drawdown",
             "sharpe", "sortino", "profit_factor", "expectancy",
             "payoff_ratio", "avg_win", "avg_loss",
             "max_consecutive_losses", "exposure_avg", "days"]
    for k in order:
        v = d[k]
        if isinstance(v, float):
            print(f"  {k:<24}{v:>12.4f}")
        else:
            print(f"  {k:<24}{v:>12}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser("autotrader")
    parser.add_argument("--csv", help="CSV 데이터 디렉터리")
    parser.add_argument("--config", help="config.yaml 경로")
    parser.add_argument("--threshold", type=float, default=0.55, help="앙상블 매수 임계값")
    parser.add_argument("--votes", type=int, default=1, help="필요 최소 전략 수")
    parser.add_argument("--trail", type=float, default=0.05, help="트레일링 스탑 비율")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--output", help="결과 JSON 파일 경로")
    p_bt.add_argument("--bars", type=int, default=None,
                      help="종목당 사용할 봉 수 = 백테스트 구간. "
                           "0 이면 있는 데이터 전부. 미지정 시 1000봉(약 4년)")
    p_bt.set_defaults(func=cmd_backtest)

    p_sc = sub.add_parser("screen")
    p_sc.add_argument("--top", type=int, default=20)
    p_sc.set_defaults(func=cmd_screen)

    p_sg = sub.add_parser("signal")
    p_sg.add_argument("--symbol")
    p_sg.set_defaults(func=cmd_signal)

    p_pp = sub.add_parser("paper")
    p_pp.add_argument("--cycles", type=int, default=3)
    p_pp.add_argument("--dry-run", action="store_true", default=False)
    p_pp.add_argument("--registry", help="StrategyRegistry JSON 경로")
    p_pp.add_argument("--validated-only", action="store_true", default=False,
                      help="레지스트리에서 승인된 전략만 실행")
    p_pp.add_argument("--allow-pre-market", action="store_true", default=False,
                      help="NXT 프리마켓(08:00~08:59) 참여")
    p_pp.add_argument("--allow-after-market", action="store_true", default=False,
                      help="NXT 애프터마켓(15:30~20:00) 참여")
    p_pp.set_defaults(func=cmd_paper)

    p_val = sub.add_parser("validate")
    p_val.add_argument("--registry", required=True, help="레지스트리 JSON 경로")
    p_val.set_defaults(func=cmd_validate)

    p_vd = sub.add_parser("validate-data",
                          help="실데이터 무결성 검사 (백테스트 이전 관문)")
    p_vd.add_argument("--symbol", action="append",
                      help="검사할 종목 (반복 지정 가능). 미지정 시 유니버스 전체")
    p_vd.add_argument("--bars", type=int, default=0,
                      help="종목당 검사할 최근 봉 수. 0(기본)이면 전체 이력. "
                           "백테스트와 같은 값을 주면 실제로 쓸 구간만 검사한다")
    p_vd.add_argument("--min-bars", type=int, default=200,
                      help="종목당 최소 봉 수 (미만이면 short_history 경고)")
    p_vd.add_argument("--jump-pct", type=float, default=0.30,
                      help="이 비율 이상 종가가 튀면 분할/오류로 지목 (기본 30%%)")
    p_vd.add_argument("--long-gap-days", type=int, default=5,
                      help="이 일수 이상 연속 결측이면 개별 지목")
    p_vd.add_argument("--stale-days", type=int, default=5,
                      help="마지막 봉이 이보다 오래되면 수집 정체로 판정")
    p_vd.add_argument("--as-of", help="기준일 YYYY-MM-DD (기본 오늘)")
    p_vd.add_argument("--show", type=int, default=40,
                      help="출력할 최대 항목 수. 0 이면 전체")
    p_vd.add_argument("--strict", action="store_true",
                      help="WARN 이 하나라도 있으면 실패 처리")
    p_vd.add_argument("--output", help="상세 리포트 JSON 저장 경로")
    p_vd.set_defaults(func=cmd_validate_data)

    p_rec = sub.add_parser("reconcile", help="두 데이터 원천 대조로 누락 종목 감지")
    p_rec.add_argument("--primary", required=True, help="주 데이터 CSV 디렉터리 (예: KRX)")
    p_rec.add_argument("--secondary", required=True, help="부 데이터 CSV 디렉터리 (예: KRX+NXT 통합)")
    p_rec.set_defaults(func=cmd_reconcile)

    p_sch = sub.add_parser("schedule", help="표준 자동매매 크론잡을 crontab 라인으로 출력")
    p_sch.add_argument("--prefix", default="python -m autotrader run-job ",
                       help="crontab 명령 프리픽스")
    p_sch.set_defaults(func=cmd_schedule)

    p_ft = sub.add_parser("fetch", help="키움 REST API 로 시세 자동 수집 (KiwoomProvider)")
    p_ft.add_argument("--cache", default="./data/kiwoom",
                      help="CSV 캐시 디렉터리")
    p_ft.add_argument("--symbol", action="append",
                      help="종목코드 (반복 지정 가능). 미지정 시 종목 마스터 전체")
    p_ft.add_argument("--limit", type=int, default=500,
                      help="종목당 최소 확보할 봉 수")
    p_ft.add_argument("--minutes", type=int, default=0,
                      help="분봉 간격(1/3/5/10/15/30/45/60). 0 이면 일봉 수집.")
    p_ft.add_argument("--real", action="store_true",
                      help="실전 서버 사용 (기본은 모의)")
    p_ft.add_argument("--min-interval", type=float, default=None,
                      help="요청 사이 최소 대기 초. 미지정 시 모드별 기본값 "
                           "(모의 1.1초 / 실서버 0.25초). 429 가 계속 나면 올린다")
    p_ft.add_argument("--debug", action="store_true",
                      help="응답 본문 일부를 출력 (벤더 필드 불일치 진단용). "
                           "요청 헤더는 출력하지 않으므로 앱키는 노출되지 않음")
    p_ft.set_defaults(func=cmd_fetch)

    p_rj = sub.add_parser("run-job", help="스케줄러가 크론에서 호출하는 표준 잡 실행")
    p_rj.add_argument("name", choices=["morning-entry", "eod-flat",
                                        "collect-daily", "collect-5m",
                                        "post-analysis"],
                      help="실행할 잡 이름")
    p_rj.add_argument("--cache", default="./data/kiwoom",
                      help="데이터 캐시 디렉터리")
    p_rj.add_argument("--registry", help="StrategyRegistry JSON 경로")
    p_rj.set_defaults(func=cmd_run_job)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
