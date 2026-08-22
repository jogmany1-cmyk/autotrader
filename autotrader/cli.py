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
                    trail_pct=args.trail)
    rep = bt.run()
    print("== 전체 성과 =====================================")
    _dump_report(rep.all)
    print("== TRAIN =========================================")
    _dump_report(rep.train)
    print("== VALIDATION ====================================")
    _dump_report(rep.val)
    print("== OUT-OF-SAMPLE  (실제 판단 근거) ================")
    _dump_report(rep.oos)
    print(f"trades={len(rep.trades)}  bars={len(rep.equity_curve)}")
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
    sc = Screener(provider, cfg.universe, top_n=args.top).rank()
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
    trader = LiveTrader(provider, broker, cfg,
                        ensemble_threshold=args.threshold,
                        ensemble_min_votes=args.votes,
                        trail_pct=args.trail, dry_run=args.dry_run)
    for i in range(args.cycles):
        rep = trader.cycle()
        print(f"[{i+1}] cand={rep.candidates} sig={rep.signals} "
              f"placed={rep.orders_placed} rej={rep.orders_rejected} "
              f"closed={rep.closed_trades}")
        for line in rep.details[:5]:
            print(f"    · {line}")
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
    p_pp.set_defaults(func=cmd_paper)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
