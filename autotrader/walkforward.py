"""Walk-forward 비교 규격 — 배치와 채점 규칙.

이 모듈은 **규격을 코드로 못박는 자리**다. 러너보다 먼저 존재해야 한다.
숫자를 본 뒤에 창 길이나 판정 기준을 손대면 그 비교는 사후 정당화가 된다.

## 왜 고정 3분할로는 부족한가

`Backtester` 는 TRAIN/VAL/OOS 를 한 번만 자른다. 그 OOS 한 구간의 성적은
"그 시기 시장이 어땠나" 에 크게 좌우되고, 여러 변형을 같은 3분할로 비교하면
분할 경계에 우연히 맞는 변형이 이긴다. 창을 밀어 가며 여러 OOS 를 만들고
그 전체를 합산해 판정하는 이유다.

## 경계 purge 가 필요한 이유

20봉 보유 전략은 구간 경계를 넘는다. 경계를 딱 붙여 놓으면 TRAIN 마지막 날
진입한 포지션의 결과가 VALIDATION 에 실려 두 구간이 정보를 공유한다. 그래서
구간 사이에 20거래일을 버린다 — 양쪽 경계 모두.

## 이 규격의 한계 (반드시 함께 읽을 것)

**이 walk-forward 는 확증이 아니라 탐색 검증이다.** 2,500봉 전체 결과를 이미
본 뒤에 설계됐기 때문이다. 같은 데이터에서 변형을 여러 개 비교할수록
백테스트 과최적화 위험이 커진다 (Bailey et al., "The Probability of Backtest
Overfitting", https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659).

따라서 이 규격을 통과해도 **실전 승격 근거가 되지 않는다.** 승격에는
(a) 이후 새로 수집되는 미사용 데이터와 (b) 최소 60거래일 페이퍼 트레이딩이
별도로 필요하다. 그 두 가지 없이 이 결과만으로 승격을 권고할 수 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# ---- 배치 규격 (실행 전 고정. 결과를 보고 바꾸지 않는다) --------------------
TRAIN_MIN_BARS = 1000      # fold 1 의 TRAIN 길이. expanding 이므로 시작점은 항상 1
VALIDATION_BARS = 250
OOS_BARS = 250
STEP_BARS = 250            # 창 이동 폭 = TRAIN 확장 폭
PURGE_BARS = 20            # 각 경계에서 버리는 거래일 수 (양쪽 모두)

# ---- 판정 규격 (기존 승격 기준을 낮추지 않는다) ----------------------------
MIN_TOTAL_OOS_PROFIT_FACTOR = 1.20   # registry.ValidationThresholds 와 같은 값
MIN_TOTAL_OOS_NET_PROFIT = 0.0       # 초과여야 함 (> 0)
MAX_DRAWDOWN = -0.25                 # 이보다 깊으면 실패
MIN_TOTAL_TRADES = 100
MIN_TRADES_PER_FOLD = 20
MIN_FOLDS_WITH_PF_ABOVE_ONE = 3      # 4개 중
MAX_PROFIT_CONCENTRATION = 0.50      # 한 fold 가 전체 gross profit 의 이 비율 초과 시 실패


@dataclass(frozen=True)
class Fold:
    """1부터 시작하는 봉 번호 기준의 닫힌 구간 [start, end].

    슬라이스로 쓸 때는 `as_slice()` 로 0-기반 반개구간으로 바꾼다. 1-기반을
    쓰는 이유는 규격 표(문서)와 코드가 한 글자도 어긋나지 않게 하기 위해서다.
    """
    index: int                       # 1..N
    train: Tuple[int, int]
    purge_after_train: Tuple[int, int]
    validation: Tuple[int, int]
    purge_after_validation: Tuple[int, int]
    oos: Tuple[int, int]

    def as_dict(self) -> Dict[str, object]:
        return {"index": self.index, "train": list(self.train),
                "purge_after_train": list(self.purge_after_train),
                "validation": list(self.validation),
                "purge_after_validation": list(self.purge_after_validation),
                "oos": list(self.oos)}


def as_slice(window: Tuple[int, int]) -> Tuple[int, int]:
    """1-기반 닫힌 구간 → 0-기반 반개구간 (파이썬 슬라이스용)."""
    lo, hi = window
    return lo - 1, hi


def build_folds(n_bars: int) -> List[Fold]:
    """expanding train walk-forward 배치.

    시작점은 1일로 고정하고 끝점만 STEP_BARS 씩 확장한다. OOS 가 규격 길이
    (OOS_BARS)를 채우지 못하는 마지막 자투리는 **fold 로 만들지 않는다** —
    짧은 fold 를 하나 더 끼우면 그 fold 의 표본이 작아 판정이 흔들리고,
    "몇 개까지 만들지" 가 결과를 보고 정하는 손잡이가 된다.
    """
    folds: List[Fold] = []
    train_end = TRAIN_MIN_BARS
    while True:
        p1 = (train_end + 1, train_end + PURGE_BARS)
        val = (p1[1] + 1, p1[1] + VALIDATION_BARS)
        p2 = (val[1] + 1, val[1] + PURGE_BARS)
        oos = (p2[1] + 1, p2[1] + OOS_BARS)
        if oos[1] > n_bars:
            break
        folds.append(Fold(index=len(folds) + 1, train=(1, train_end),
                          purge_after_train=p1, validation=val,
                          purge_after_validation=p2, oos=oos))
        train_end += STEP_BARS
    return folds


def unused_tail(n_bars: int) -> Tuple[int, int]:
    """마지막 fold 이후 남는 봉 구간. 비어 있으면 (0, 0).

    채점에서 제외된다는 사실을 리포트에 남기기 위한 값이다. 조용히 버리면
    "전 구간을 썼다" 고 오해하게 된다.
    """
    folds = build_folds(n_bars)
    if not folds:
        return (0, 0)
    last = folds[-1].oos[1]
    return (last + 1, n_bars) if last < n_bars else (0, 0)


# ---- 채점 ------------------------------------------------------------------

def gross_profit(pnls: Sequence[float]) -> float:
    """양수 PnL 의 합. PF 의 분자와 같은 정의를 쓴다.

    PnL 은 수수료·거래세·슬리피지가 모두 반영된 체결 기준이다.
    """
    return sum(p for p in pnls if p > 0)


def gross_loss(pnls: Sequence[float]) -> float:
    """음수 PnL 절댓값의 합. PF 의 분모."""
    return sum(-p for p in pnls if p < 0)


def combined_profit_factor(pnls_by_fold: Sequence[Sequence[float]]) -> float:
    """합산 OOS PF = 전체 총이익 ÷ 전체 총손실.

    **fold 별 PF 의 평균이 아니다.** 평균을 쓰면 거래가 두 건뿐인 fold 의
    PF 가 200건짜리 fold 와 같은 무게를 갖고, 손실이 0 인 fold 하나가 무한대로
    평균을 오염시킨다.
    """
    profit = sum(gross_profit(p) for p in pnls_by_fold)
    loss = sum(gross_loss(p) for p in pnls_by_fold)
    if loss <= 0:
        return float("inf") if profit > 0 else 0.0
    return profit / loss


def profit_concentration(pnls_by_fold: Sequence[Sequence[float]]) -> float:
    """가장 큰 fold 의 총이익 ÷ 전체 fold 총이익 합.

    전체 총이익이 0 이면 1.0 을 돌려준다 — 집중도를 잴 수 없는 상태는
    안정성 통과로 볼 수 없으므로 자동 실패 쪽으로 붙인다.
    """
    per_fold = [gross_profit(p) for p in pnls_by_fold]
    total = sum(per_fold)
    if total <= 0:
        return 1.0
    return max(per_fold) / total


# ---- 러너 ------------------------------------------------------------------
#
# fit_mode = "none". TRAIN 에서 아무것도 적합하지 않는다.
#
# 엄밀히 이것은 walk-forward *optimization* 이 아니라 **expanding
# rolling-origin evaluation** 이다. 고정된 설정을 시간 구간을 밀어 가며
# 적용해 안정성만 본다. 나중에 누군가 TRAIN 자동 튜닝을 붙이면 그것은
# 다른 실험이므로, 리포트의 fit_mode 로 두 결과가 섞이지 않게 한다.

FIT_MODE = "none"

SCORE_MODES = ("all-weights", "active-voters")


@dataclass
class SegmentResult:
    """한 fold 의 한 구간(train/validation/oos) 실행 결과."""
    name: str
    window: Tuple[int, int]          # 1-기반 봉 번호
    start: str                       # ISO 날짜
    end: str
    n_trades: int
    trade_pnls: List[float]          # 수수료·세금·슬리피지 반영된 체결 기준
    net_return: float
    profit_factor: float
    max_drawdown: float
    cost: Dict[str, float]
    # 창 끝 강제청산 건수와 그 PnL. 인위적 청산이 성적의 얼마를 차지하는지
    # 보이지 않으면, 창 경계가 결과를 만들고 있어도 알 수 없다.
    window_end_trades: int = 0
    window_end_pnl: float = 0.0
    unclosed_exposure: float = 0.0    # 청산 못 한 포지션이 남았는지

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "window": list(self.window),
                "start": self.start, "end": self.end,
                "n_trades": self.n_trades,
                "gross_profit": round(gross_profit(self.trade_pnls), 2),
                "gross_loss": round(gross_loss(self.trade_pnls), 2),
                "net_return": self.net_return,
                "profit_factor": self.profit_factor,
                "max_drawdown": self.max_drawdown, "cost": self.cost,
                "window_end_trades": self.window_end_trades,
                "window_end_pnl": round(self.window_end_pnl, 2),
                "unclosed_exposure": self.unclosed_exposure}


@dataclass
class FoldResult:
    fold: Fold
    train: SegmentResult
    validation: SegmentResult
    oos: SegmentResult

    def as_dict(self) -> Dict[str, object]:
        return {"fold": self.fold.as_dict(),
                # TRAIN·VALIDATION 은 참고용이다. 채점은 OOS 만 한다.
                "train_reference": self.train.as_dict(),
                "validation_reference": self.validation.as_dict(),
                "oos_scored": self.oos.as_dict()}


@dataclass
class Verdict:
    passed: bool
    checks: List[Tuple[str, bool, str]]

    def as_dict(self) -> Dict[str, object]:
        return {"passed": self.passed,
                "checks": [{"name": n, "ok": ok, "detail": d}
                           for n, ok, d in self.checks]}


def judge(oos_pnls_by_fold: Sequence[Sequence[float]],
          max_drawdown: float) -> Verdict:
    """사전 등록된 기준으로만 판정한다. 기준은 docs/WALKFORWARD-SPEC.md §5."""
    pf = combined_profit_factor(oos_pnls_by_fold)
    net = sum(sum(p) for p in oos_pnls_by_fold)
    total_trades = sum(len(p) for p in oos_pnls_by_fold)
    per_fold_pf = [combined_profit_factor([p]) for p in oos_pnls_by_fold]
    n_pf_above_one = sum(1 for v in per_fold_pf if v > 1.0)
    conc = profit_concentration(oos_pnls_by_fold)
    checks = [
        ("합산 OOS PF ≥ 1.20", pf >= MIN_TOTAL_OOS_PROFIT_FACTOR, f"{pf:.3f}"),
        ("합산 OOS 순수익 > 0", net > MIN_TOTAL_OOS_NET_PROFIT, f"{net:,.0f}"),
        ("MDD ≥ -25%", max_drawdown >= MAX_DRAWDOWN, f"{max_drawdown:.4f}"),
        ("합산 거래 ≥ 100", total_trades >= MIN_TOTAL_TRADES, f"{total_trades}"),
        ("각 fold 거래 ≥ 20",
         all(len(p) >= MIN_TRADES_PER_FOLD for p in oos_pnls_by_fold),
         ", ".join(str(len(p)) for p in oos_pnls_by_fold)),
        ("PF > 1.0 인 fold ≥ 3", n_pf_above_one >= MIN_FOLDS_WITH_PF_ABOVE_ONE,
         f"{n_pf_above_one}/{len(oos_pnls_by_fold)}"),
        ("집중도 ≤ 0.50", conc <= MAX_PROFIT_CONCENTRATION, f"{conc:.3f}"),
    ]
    return Verdict(passed=all(ok for _, ok, _ in checks), checks=checks)


def _segment(name: str, window: Tuple[int, int], timeline, provider, config,
             threshold: float, min_votes: int, trail: float,
             history_bars: int, symbols, score_mode: str) -> "SegmentResult":
    """구간 하나를 독립 실행한다 — 같은 초기자본, 무포지션 시작.

    창 밖의 봉은 지표 계산용 이력으로만 남는다. 이전 구간의 포지션은
    이월되지 않는다 (Backtester 를 새로 만들므로 브로커·포트폴리오가 새 것).
    """
    from .backtest import Backtester

    lo, hi = window
    start, end = timeline[lo - 1], timeline[hi - 1]
    bt = Backtester(provider, config, ensemble_threshold=threshold,
                    ensemble_min_votes=min_votes, trail_pct=trail,
                    history_bars=history_bars, trade_window=(start, end),
                    score_mode=score_mode)
    rep = bt.run(symbols=symbols)
    trades = [t for t in rep.trades if start <= t.exit_ts <= end]
    forced = [t for t in trades if t.exit_reason == "window_end"]
    c = rep.cost_audit
    return SegmentResult(
        name=name, window=window,
        start=start.date().isoformat(), end=end.date().isoformat(),
        n_trades=len(trades), trade_pnls=[t.pnl for t in trades],
        net_return=rep.all.net_return, profit_factor=rep.all.profit_factor,
        max_drawdown=rep.all.max_drawdown,
        cost=(c.to_dict() if c is not None else {}),
        window_end_trades=len(forced),
        window_end_pnl=sum(t.pnl for t in forced),
        # 강제청산 뒤에도 노출이 남았다면 청산되지 못한 포지션이 있다는 뜻이다.
        unclosed_exposure=(rep.equity_curve[-1].exposure
                           if rep.equity_curve else 0.0),
    )


def run_walkforward(provider, config, *, symbols=None, threshold: float = 0.45,
                    min_votes: int = 1, trail: float = 0.05,
                    history_bars: int = 2500,
                    score_mode: str = "all-weights") -> Dict[str, object]:
    """규격대로 rolling-origin 평가를 돌리고 리포트 dict 를 돌려준다.

    **봉 번호는 병합 시간축 기준이다.** 종목마다 상장일과 봉 수가 다르므로
    "1291번째 봉" 을 종목별 인덱스로 잡으면 종목마다 다른 날짜가 된다. 모든
    종목의 날짜 합집합을 정렬한 축 위에서 fold 를 자른다.
    """
    from .backtest import _merge_timeline

    if score_mode not in SCORE_MODES:
        raise ValueError(f"score_mode 는 {SCORE_MODES} 중 하나여야 합니다")
    syms = list(symbols) if symbols else provider.universe()
    bars_by_symbol = {}
    for s in syms:
        try:
            bars_by_symbol[s] = provider.history(s, limit=history_bars)
        except Exception:
            continue
    if not bars_by_symbol:
        raise RuntimeError("사용할 심볼 데이터가 없습니다")
    # 종목별 history(limit=N) 의 **합집합**은 N 을 넘을 수 있다. 상장·폐지
    # 시점이 달라 종목마다 다른 N 일을 들고 오기 때문이다. 그대로 두면 fold 가
    # 4개보다 많이 생겨 사전 등록한 규격이 달라진다 — 실행 후 규격이 바뀌는
    # 것과 같다. 그래서 채점 시간축을 **최근 history_bars 개 글로벌 거래일**로
    # 자른다. 잘라낸 앞부분은 리포트에 남긴다.
    full_timeline = _merge_timeline(bars_by_symbol)
    timeline = full_timeline[-history_bars:] if history_bars else full_timeline
    trimmed = len(full_timeline) - len(timeline)
    n_bars = len(timeline)
    folds = build_folds(n_bars)
    if not folds:
        need = TRAIN_MIN_BARS + 2 * PURGE_BARS + VALIDATION_BARS + OOS_BARS
        raise RuntimeError(
            f"봉 {n_bars}개로는 fold 를 만들 수 없습니다 (규격상 최소 {need}봉)")

    syms = list(bars_by_symbol)
    results: List[FoldResult] = []
    for f in folds:
        results.append(FoldResult(
            fold=f,
            train=_segment("train", f.train, timeline, provider, config,
                           threshold, min_votes, trail, history_bars, syms,
                           score_mode),
            validation=_segment("validation", f.validation, timeline, provider,
                                config, threshold, min_votes, trail,
                                history_bars, syms, score_mode),
            oos=_segment("oos", f.oos, timeline, provider, config,
                         threshold, min_votes, trail, history_bars, syms,
                         score_mode),
        ))

    oos_pnls = [r.oos.trade_pnls for r in results]
    worst_dd = min((r.oos.max_drawdown for r in results), default=0.0)
    verdict = judge(oos_pnls, worst_dd)
    tail = unused_tail(n_bars)
    return {
        # 이 실험이 무엇이었는지. 나중에 TRAIN 튜닝이 붙어도 섞이지 않게.
        "fit_mode": FIT_MODE,
        "evaluation": "expanding rolling-origin (walk-forward optimization 아님)",
        "score_mode": score_mode,
        "settings": {"threshold": threshold, "min_votes": min_votes,
                     "trail": trail, "history_bars": history_bars,
                     "n_symbols": len(syms), "n_bars_timeline": n_bars,
                     # 실제로 채점한 축이 어디부터 어디까지인지, 그리고 합집합
                     # 에서 몇 일을 잘라냈는지. 이게 없으면 두 실행이 같은
                     # 구간을 봤는지 나중에 확인할 수 없다.
                     "timeline_start": timeline[0].date().isoformat(),
                     "timeline_end": timeline[-1].date().isoformat(),
                     "merged_union_bars": len(full_timeline),
                     "trimmed_leading_bars": trimmed,
                     "costs": {"commission_bp": config.costs.commission_bp,
                               "tax_sell_bp": config.costs.tax_sell_bp,
                               "slippage_bp": config.costs.slippage_bp}},
        "spec": {"train_min_bars": TRAIN_MIN_BARS,
                 "validation_bars": VALIDATION_BARS, "oos_bars": OOS_BARS,
                 "step_bars": STEP_BARS, "purge_bars": PURGE_BARS},
        "folds": [r.as_dict() for r in results],
        "combined_oos": {
            "profit_factor": round(combined_profit_factor(oos_pnls), 4),
            "net_profit": round(sum(sum(p) for p in oos_pnls), 2),
            "gross_profit": round(sum(gross_profit(p) for p in oos_pnls), 2),
            "gross_loss": round(sum(gross_loss(p) for p in oos_pnls), 2),
            "n_trades": sum(len(p) for p in oos_pnls),
            "worst_fold_max_drawdown": round(worst_dd, 4),
            "profit_concentration": round(profit_concentration(oos_pnls), 4),
        },
        # 조용히 버리면 "전 구간을 썼다" 고 오해하게 된다.
        "excluded_tail_bars": list(tail),
        "verdict": verdict.as_dict(),
        "caveat": ("탐색 검증이다. 통과해도 승격 근거가 아니며 미사용 데이터와 "
                   "최소 60거래일 페이퍼 트레이딩이 별도로 필요하다."),
    }
