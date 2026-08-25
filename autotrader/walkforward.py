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
