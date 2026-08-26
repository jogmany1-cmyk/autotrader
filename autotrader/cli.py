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
import os
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
        print(f"  총 매매대금      {c.total_gross_volume:>16,.0f}")
        print(f"  총 수수료        {c.total_fees:>16,.2f}")
        print(f"  총 거래세        {c.total_taxes:>16,.2f}")
        # 슬리피지는 체결가에 이미 녹아 있어 실측이 불가능하다. 설정값으로
        # 되짚은 추정치임을 줄마다 밝힌다 — 실측으로 오해하면 안 된다.
        print(f"  슬리피지(추정)   {c.total_slippage_est:>16,.2f}"
              f"   ← 설정 {c.slippage_bp:g}bp × 매매대금")
        print(f"  총 비용          {c.total_cost:>16,.2f}")
        print(f"  평균 체결        {c.avg_trade_size:>16,.0f}")
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
                # 비용 감사가 JSON 에 없으면 나중에 성적만 놓고 비교하게 되고,
                # 회전율·비용이 다른 두 실행이 같은 조건으로 오해된다.
                "cost_audit": (rep.cost_audit.to_dict()
                               if rep.cost_audit is not None else None),
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
                        registry=reg, validated_only=args.validated_only,
                        order_log=args.order_log, state_path=args.state)
    trader.allow_pre_market = args.allow_pre_market
    trader.allow_after_market = args.allow_after_market
    # 재시작 복구. 건너뛰면 이미 들고 있는 종목에 또 들어가고, 일일 진입
    # 상한이 0 부터 다시 세어지고, 손절선을 몰라 스탑이 안 걸린다.
    if args.state:
        for note in trader.recover():
            print(f"  {note}")
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
        # 매 사이클 끝에 남긴다. 종료 시점에만 저장하면 강제 종료·정전에서
        # 그 사이 진입한 포지션의 손절선을 통째로 잃는다.
        trader.save_state()
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
    if not symbols:
        # 유니버스가 비었는데 종료코드 0 을 주면, 크론은 "수집 성공" 으로 보고
        # 다음 단계(백테스트)가 빈 캐시로 돌아간다. 조용한 성공이 가장 나쁘다.
        print("[ERROR] 수집할 종목이 없습니다 (--symbol 을 주거나 유니버스를 확인하세요)")
        return 2
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
    if ok == 0:
        return 2      # 전부 실패했는데 fail 만 세고 0 을 주면 안 된다
    return 0 if fail == 0 else 1


# walkforward CLI 의 --score-mode 선택지. walkforward 모듈을 최상단에서
# 임포트하지 않으려고 이름만 복사하지 않고, 파서 구성 시점에 읽어 온다.
from .walkforward import INDEPENDENT_STRATEGIES as _WF_SOLO
from .walkforward import SCORE_MODES as _WF_MODES


def cmd_walkforward(args) -> int:
    """시간 구간별 안정성 평가. **채점은 OOS 만** 한다 (규격 §2).

    종료코드 0 = 사전 등록 기준 전부 충족, 1 = 하나라도 미달, 2 = 실행 불가.
    """
    from . import walkforward as wf

    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    try:
        rep = wf.run_walkforward(
            provider, cfg, symbols=args.symbol, threshold=args.threshold,
            min_votes=args.votes, trail=args.trail, history_bars=args.bars,
            score_mode=args.score_mode, strategy=args.strategy)
    except (RuntimeError, ValueError, NotImplementedError) as exc:
        print(f"[ERROR] {exc}")
        return 2

    s, c = rep["settings"], rep["combined_oos"]
    print(f"== 구간별 안정성 평가 ({rep['evaluation']}) ==")
    print(f"  fit_mode={rep['fit_mode']}  score_mode={rep['score_mode']}  "
          f"임계={s['threshold']}  min_votes={s['min_votes']}")
    print(f"  [탐색] 동일 OOS 참조 {s['exploratory_reference_round']}회차 · "
          "최종 판정은 이후 새 데이터 60거래일 모의투자에서만 수행")
    if rep["strategy"]:
        print(f"  [단독] 전략 {rep['strategy']} · 최대 보유 "
              f"{rep['max_holding_bars']}봉 (손절·목표가·트레일링은 유지)")
    print(f"  종목 {s['n_symbols']:,}개 · 시간축 {s['n_bars_timeline']:,}봉 "
          f"· fold {len(rep['folds'])}개")
    tail = rep["excluded_tail_bars"]
    if tail != [0, 0]:
        print(f"  채점 제외 자투리: 봉 {tail[0]}~{tail[1]} "
              f"({tail[1] - tail[0] + 1}봉, 규격 길이 미달)")
    print()
    print(f"  {'fold':>5}{'OOS 구간':>26}{'거래':>7}{'PF':>9}{'총이익':>14}")
    print("  " + "-" * 60)
    for f in rep["folds"]:
        o = f["oos_scored"]
        print(f"  {f['fold']['index']:>5}  {o['start']} ~ {o['end']}"
              f"{o['n_trades']:>7}{o['profit_factor']:>9.3f}"
              f"{o['gross_profit']:>14,.0f}")
    print()
    print(f"  합산 OOS: PF={c['profit_factor']:.3f}  순수익={c['net_profit']:,.0f}  "
          f"거래={c['n_trades']}  집중도={c['profit_concentration']:.3f}")
    d = c["diagnostics"]
    print(f"  거래 진단: 승률={d['win_rate']:.1%}  평균승={d['avg_win']:,.0f}  "
          f"평균패={d['avg_loss']:,.0f}  평균보유={d['avg_bars_held']:.1f}봉")
    print(f"  비용 진단: 비용후={d['net_profit']:,.0f}  "
          f"총비용={d['total_cost']:,.0f}  "
          f"비용전(추정)={d['estimated_pre_cost_net']:,.0f}")
    funnel = c["entry_funnel"]
    print(f"  진입 흐름: 평가={funnel['strategy_evaluations']:,}  "
          f"매수신호={funnel['buy_signals']:,}({funnel['buy_signal_rate']:.2%})  "
          f"주문시도={funnel['pending_attempts']:,}  "
          f"체결={funnel['entries_filled']:,}({funnel['fill_rate_from_attempts']:.1%})")
    if funnel["risk_rejections"]:
        rejects = ", ".join(
            f"{reason}={count:,}"
            for reason, count in sorted(funnel["risk_rejections"].items(),
                                        key=lambda item: (-item[1], item[0])))
        print(f"  리스크 거절: {rejects}")
    print(f"  진입 점수: 평균={d['avg_entry_score']:.3f}  "
          f"중앙={d['median_entry_score']:.3f}  평균투표={d['avg_entry_votes']:.2f}")
    print("  점수 구간별:")
    for bucket, row in d["by_entry_score_bucket"].items():
        pf = "-" if row["profit_factor"] is None else f"{row['profit_factor']:.2f}"
        print(f"    {bucket:<9} {row['n_trades']:>5}건  승률 {row['win_rate']:>6.1%}  "
              f"PF {pf:>6}  손익 {row['net_profit']:>12,.0f}")
    if d["by_entry_factor"]:
        print("  진입 조건별 (hard_stop 집중 확인):")
        for factor in d["by_entry_factor"].values():
            print(f"    [{factor['label']}]")
            for bucket, row in factor["buckets"].items():
                pf = ("-" if row["profit_factor"] is None
                      else f"{row['profit_factor']:.2f}")
                print(f"      {bucket:<12} {row['n_trades']:>5}건  "
                      f"hard_stop {row['hard_stop_rate']:>6.1%}  "
                      f"PF {pf:>6}  손익 {row['net_profit']:>12,.0f}")
    print("  청산 사유별 (손익이 나쁜 순):")
    reasons = sorted(d["by_exit_reason"].items(),
                     key=lambda item: item[1]["net_profit"])
    for reason, row in reasons:
        pf = "-" if row["profit_factor"] is None else f"{row['profit_factor']:.2f}"
        print(f"    {reason:<12} {row['n_trades']:>5}건  "
              f"승률 {row['win_rate']:>6.1%}  PF {pf:>6}  "
              f"손익 {row['net_profit']:>12,.0f}  "
              f"보유 {row['avg_bars_held']:>5.1f}봉")
    print()
    for chk in rep["verdict"]["checks"]:
        print(f"  [{'PASS' if chk['ok'] else 'FAIL'}] {chk['name']:<22} {chk['detail']}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, ensure_ascii=False)
        print(f"\n  → {args.output} 에 저장")

    ok = rep["verdict"]["passed"]
    print(f"\n  판정: {'PASS' if ok else 'FAIL'}")
    # Windows PowerShell 의 기본 cp949 출력에서는 U+26A0 경고 기호를 인코딩할
    # 수 없어, 계산을 모두 마친 뒤 여기서 명령이 실패한다. ASCII 표식을 쓴다.
    print(f"  [WARN] {rep['caveat']}")
    return 0 if ok else 1


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
        # 폴더 자체가 없는 경우와 CSV 만 없는 경우를 구분한다. 전자를
        # "종목이 없습니다" 로 끝내면 --csv 경로 오타를 데이터 문제로 착각한다.
        if args.csv and not os.path.isdir(args.csv):
            print(f"[ERROR] 폴더가 없습니다: {args.csv}")
        else:
            print(f"[ERROR] 검사할 CSV 가 없습니다: {source} "
                  "(`{종목}.csv` 형식의 파일이 필요합니다)")
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


def cmd_edge(args) -> int:
    """진입 신호에 우위가 있는지 측정. 청산 규칙은 끄고 신호만 본다.

    백테스트 성적이 나쁠 때 "진입이 문제인가, 청산이 문제인가" 를 가른다.
    둘은 정반대의 대응을 요구하므로, 이걸 모르고 설정을 만지면 과최적화가 된다.
    """
    from .edge import EdgeAnalyzer, default_ensemble

    provider = _provider(args.csv)
    cfg = _config(args.config, provider)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    try:
        ens = default_ensemble(cfg, args.threshold, args.votes,
                               only=args.strategy)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2
    an = EdgeAnalyzer(provider, cfg, ensemble=ens, threshold=args.threshold,
                      min_votes=args.votes, horizons=horizons,
                      warmup=args.warmup)
    rep = an.run(symbols=args.symbol, bars=args.bars)
    if not rep.n_signals:
        print(f"[EDGE] 신호가 하나도 없습니다 (임계 {args.threshold}). "
              f"--threshold 를 낮춰 보세요.")
        return 2

    from .edge import STRATEGY_NAMES

    print(f"== 진입 신호 우위 측정 (임계 {rep.threshold}, "
          f"봉 {args.bars or '전체'}) ==")
    if len(rep.strategies) < len(STRATEGY_NAMES):
        # 격리 측정임을 눈에 띄게 남긴다. 하나만 켠 결과를 전체 앙상블 결과로
        # 착각하면 엉뚱한 전략을 고치게 된다.
        print(f"  [격리] 전략 {len(rep.strategies)}개만 켬: "
              f"{', '.join(rep.strategies) or '<없음>'}")
        print("         점수 = 이 전략들의 가중평균이라 전체 앙상블과 "
              "같은 임계값이라도 의미가 다르다.")
    print(f"  {rep.summary()}")
    print()
    print(f"  {'지평선':>6}{'신호평균':>11}{'기준평균':>11}{'우위':>11}{'t값':>8}  판정")
    print("  " + "-" * 56)
    for h in rep.horizons:
        print("  " + h.as_line())

    if rep.buckets:
        print()
        print("  점수 구간별 (점수가 높을수록 수익이 커야 점수가 의미 있다)")
        hs = [h.horizon for h in rep.horizons]
        head = "".join(f"{h}일".rjust(10) for h in hs)
        print(f"  {'구간':>13}{'건수':>6}{head}")
        for b in rep.buckets:
            means = "".join(f"{b.means[h]:>+9.2%}" for h in hs)
            print(f"  {b.lo:.2f}~{b.hi:.2f}".rjust(15) + f"{b.n:>6}{means}")

    if rep.adverse:
        print()
        print(f"  역행 비율 ({rep.adverse_horizon}일 안에 저가가 그만큼 밀린 신호)")
        for lv, ratio in sorted(rep.adverse.items()):
            print(f"    -{lv:.0%} 이하: {ratio:>6.1%}"
                  + ("   ← 손절이 이 폭보다 좁으면 이길 자리에서 잘린다"
                     if ratio >= 0.25 else ""))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(rep.as_dict(), fh, ensure_ascii=False, indent=2)
        print(f"\n  → {args.output} 에 저장")

    if args.min_t is not None:
        # 지평선 4개 중 최대값으로 판정하면 전부 잡음이어도 하나는 커 보인다.
        # 게이트는 지평선을 하나 정해 그것만 본다.
        t = rep.gate_t(args.gate_horizon)
        h = args.gate_horizon or (rep.horizons[-1].horizon if rep.horizons else 0)
        ok = t >= args.min_t
        print(f"\n  판정: {'PASS' if ok else 'FAIL'} "
              f"({h}일 t={t:.2f}, 기준 {args.min_t})")
        return 0 if ok else 1
    return 0


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
    p_pp.add_argument("--state", default=None,
                      help="재시작 복구용 상태 파일 (예: runs/state.json). 지정하면 "
                           "손절선·일일카운터·쿨다운·EOD 수행여부가 재시작을 건넌다")
    p_pp.add_argument("--order-log", default=None,
                      help="미결 주문 장부 JSONL (예: runs/orders.jsonl)")
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

    p_ed = sub.add_parser("edge",
                          help="진입 신호에 우위가 있는지 측정 (청산 규칙 제외)")
    p_ed.add_argument("--symbol", action="append",
                      help="검사할 종목 (반복 지정). 미지정 시 유니버스 전체")
    p_ed.add_argument("--strategy", action="append",
                      help="이 전략만 켜고 측정 (반복 지정). 미지정 시 전체 앙상블. "
                           "앙상블이 지고 있을 때 어느 전략이 주범인지 가른다. "
                           "가능: day_breakout, day_pullback, day_momentum, "
                           "swing_trend, mean_reversion")
    p_ed.add_argument("--bars", type=int, default=0,
                      help="종목당 사용할 봉 수. 0(기본)이면 전체 이력")
    p_ed.add_argument("--horizons", default="1,5,10,20",
                      help="측정할 보유일수, 쉼표 구분 (기본 1,5,10,20)")
    p_ed.add_argument("--warmup", type=int, default=250,
                      help="지표 워밍업에 쓸 앞부분 봉 수 (기본 250)")
    p_ed.add_argument("--gate-horizon", type=int, default=None,
                      help="--min-t 판정에 쓸 지평선(일). 미지정 시 가장 긴 것")
    p_ed.add_argument("--min-t", type=float, default=None,
                      help="이 t값 미만이면 종료코드 1. 지정 시 게이트로 동작")
    p_ed.add_argument("--output", help="결과 JSON 저장 경로")
    p_ed.set_defaults(func=cmd_edge)

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

    p_wf = sub.add_parser(
        "walkforward",
        help="시간 구간별 안정성 평가 (expanding rolling-origin). "
             "규격은 docs/WALKFORWARD-SPEC.md — 실행 후 설정 재조정 금지")
    p_wf.add_argument("--bars", type=int, default=2500,
                      help="종목당 사용할 봉 수 (기본 2500 = 규격값)")
    p_wf.add_argument("--score-mode", default="all-weights",
                      choices=list(_WF_MODES),
                      help="앙상블 점수 방식. all-weights 가 현재 방식(기본값)")
    p_wf.add_argument("--strategy", choices=list(_WF_SOLO),
                      help="전략 하나만 단독 실행 (규격 §6). 지정하면 그 전략의 "
                           "최대 보유기간이 함께 적용된다: "
                           + ", ".join(f"{k}={v}봉" for k, v in _WF_SOLO.items()))
    p_wf.add_argument("--symbol", action="append", help="종목 지정 (반복)")
    p_wf.add_argument("--output", help="결과 JSON 저장 경로")
    p_wf.set_defaults(func=cmd_walkforward)

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
