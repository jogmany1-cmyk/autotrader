"""과최적화 보정 — 시도 횟수를 세고, 샤프를 그만큼 깎는다.

왜 필요한가. 이 저장소는 이미 같은 4개 OOS fold 를 네 번 들여다봤고, 그때마다
전략·점수식·후보선택을 바꿔 가며 재실행했다. 그 과정에서 나오는 "제일 좋은
설정"의 샤프는 **통계량이 아니라 순서통계량(order statistic)** 이다.

Bailey, Borwein, López de Prado & Zhu (Notices of the AMS 61(5), 2014):

    "5년 데이터만 있다면 45개 이상의 독립 설정을 시도해서는 안 된다.
     그러지 않으면 표본 내 연환산 샤프 1.0 / 표본 외 기댓값 0 인 전략을
     거의 확실히 만들어낸다."

그리고 더 중요한 것 — 메모리 효과가 있는 계열에서 과최적화의 표본 외 기대수익은
**0 이 아니라 음수**다. 노이즈에 맞춰진 설정을 고르는 행위 자체가 체계적으로
평균회귀의 반대편을 선택하기 때문이다.

여기서 구현하는 것:

  - `expected_max_sharpe(n_trials, n_obs)` : 아무 우위도 없는 전략 N 개를
    시도했을 때 순전히 운으로 나오는 **최고 샤프**.
  - `deflated_sharpe(...)` : 그 기준선을 넘을 확률 (Bailey & López de Prado,
    JPM 40(5), 2014). 0.95 미만이면 "운으로 설명 가능"으로 읽는다.

표준 라이브러리만 쓴다 (`statistics.NormalDist` 가 정규분포 CDF/역함수를
제공한다). 이 저장소의 stdlib 전용 제약을 지키기 위해서다.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence, Tuple

#: 오일러-마스케로니 상수. 극단값 분포 근사에 쓰인다.
EULER_MASCHERONI = 0.5772156649015329

#: 이 값 미만이면 "다중검정을 감안하면 운으로 설명된다" 로 읽는다.
DSR_THRESHOLD = 0.95

_N = NormalDist()


def sharpe_ratio(returns: Sequence[float]) -> float:
    """표본 샤프(주기당). 연환산하지 않는다 — 연환산은 호출부 책임이다."""
    vals = [float(r) for r in returns]
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var)


def _moments(returns: Sequence[float]) -> Tuple[float, float]:
    """(왜도, 첨도). 첨도는 **초과첨도가 아니라 원(raw) 첨도** — 정규분포 3.0.

    DSR 공식이 원첨도를 받으므로 여기서 변환하지 않는다. 초과첨도를 넣으면
    분모가 작아져 DSR 이 과대평가된다.
    """
    vals = [float(r) for r in returns]
    n = len(vals)
    if n < 3:
        return 0.0, 3.0
    mean = sum(vals) / n
    m2 = sum((v - mean) ** 2 for v in vals) / n
    if m2 <= 0:
        return 0.0, 3.0
    m3 = sum((v - mean) ** 3 for v in vals) / n
    m4 = sum((v - mean) ** 4 for v in vals) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def expected_max_sharpe(n_trials: int, n_obs: int) -> float:
    """우위가 전혀 없는 전략 `n_trials` 개 중 최고 샤프의 기댓값 (주기당).

    독립 시행을 가정한 극단값 근사다. 실제 전략 그리드는 서로 상관되어 있어
    이 값은 **보수적으로 큰 쪽**이 아니라 오히려 작을 수 있다 — 상관이 높으면
    유효 시행 수가 줄기 때문이다. 즉 여기서 계산한 기준선을 넘지 못하면
    상관을 고려해도 넘지 못한다고 보아도 된다(그 역은 성립하지 않는다).
    """
    if n_trials <= 1 or n_obs < 2:
        return 0.0
    # 무우위 전략의 샤프 추정치 표준오차 ≈ 1/√n
    sigma = 1.0 / math.sqrt(n_obs)
    n = float(n_trials)
    q1 = _N.inv_cdf(1.0 - 1.0 / n)
    q2 = _N.inv_cdf(1.0 - 1.0 / (n * math.e))
    return sigma * ((1.0 - EULER_MASCHERONI) * q1 + EULER_MASCHERONI * q2)


def probabilistic_sharpe(observed: float, benchmark: float, n_obs: int,
                         skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """관측 샤프가 `benchmark` 를 진짜로 넘을 확률 (Bailey & López de Prado).

    표본 길이·왜도·첨도를 함께 보정한다. 음의 왜도와 두꺼운 꼬리는 샤프를
    부풀리므로 확률을 낮춘다 — 손절 기반 전략은 대개 음의 왜도를 갖는다.
    """
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * observed + (kurtosis - 1.0) / 4.0 * observed ** 2
    if denom <= 0:
        return 0.0
    z = (observed - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return _N.cdf(z)


def deflated_sharpe(returns: Sequence[float], n_trials: int) -> float:
    """Deflated Sharpe Ratio. `n_trials` 개를 시도해 고른 결과라는 사실을 반영.

    반환값은 확률이다. `DSR_THRESHOLD`(0.95) 미만이면 다중검정을 감안했을 때
    운으로 설명 가능하다는 뜻이며, **승격 근거로 쓰면 안 된다**.
    """
    n_obs = len(returns)
    if n_obs < 2:
        return 0.0
    sr = sharpe_ratio(returns)
    skew, kurt = _moments(returns)
    benchmark = expected_max_sharpe(n_trials, n_obs)
    return probabilistic_sharpe(sr, benchmark, n_obs, skew, kurt)


def min_backtest_length_years(n_trials: int, target_sharpe: float = 1.0) -> float:
    """MinBTL — 이만큼의 연수가 없으면 `n_trials` 시도는 자기기만이다.

    Bailey et al. (2014): MinBTL < 2·ln(N) / E[max SR_N]².
    연환산 샤프 기준이며, 목표 샤프 1.0 에서 N=45 이면 약 5년이 나온다.
    """
    if n_trials <= 1 or target_sharpe <= 0:
        return 0.0
    return 2.0 * math.log(n_trials) / (target_sharpe ** 2)
