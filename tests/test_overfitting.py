"""과최적화 보정과 회전율 게이트 고정.

이 저장소는 같은 4개 OOS fold 를 이미 네 번 재사용했다. 그런 상태에서 나온
"제일 좋은 설정"의 샤프는 통계량이 아니라 순서통계량이다. 여기서 고정하는
것은 그 사실을 리포트가 **자동으로 드러내게** 만드는 장치다.
"""
import math

import pytest

from autotrader import walkforward as wf
from autotrader.overfitting import (DSR_THRESHOLD, deflated_sharpe,
                                    expected_max_sharpe,
                                    min_backtest_length_years,
                                    probabilistic_sharpe, sharpe_ratio)


# ---- Bailey et al. 의 인용 재현 --------------------------------------------

def test_reproduces_the_forty_five_trials_claim():
    """Bailey, Borwein, López de Prado & Zhu (AMS 2014) 의 대표 인용:

        "5년 데이터만 있다면 45개 이상의 독립 설정을 시도해서는 안 된다.
         그러지 않으면 표본 내 연환산 샤프 1.0 / 표본 외 0 인 전략을 거의
         확실히 만들어낸다."

    구현이 맞다면 N=45, 5년 일간(1,260관측)에서 연환산 최고샤프가 ~1.0 이 나온다.
    """
    per_period = expected_max_sharpe(45, 252 * 5)
    assert per_period * math.sqrt(252) == pytest.approx(1.0, abs=0.05)


def test_more_trials_raise_the_luck_benchmark():
    n_obs = 252 * 5
    a, b, c = (expected_max_sharpe(n, n_obs) for n in (10, 100, 1000))
    assert a < b < c


def test_longer_samples_lower_the_luck_benchmark():
    """표본이 길수록 운으로 높은 샤프를 내기 어렵다."""
    assert expected_max_sharpe(100, 2000) < expected_max_sharpe(100, 250)


def test_single_trial_needs_no_deflation():
    assert expected_max_sharpe(1, 1000) == 0.0


def test_min_backtest_length_grows_with_trials():
    assert (min_backtest_length_years(45)
            < min_backtest_length_years(100)
            < min_backtest_length_years(1000))


# ---- DSR 의 실제 효과 ------------------------------------------------------

def _returns(mean, sd, n, seed=1):
    """결정론적 의사난수 — 표준 라이브러리 random 대신 재현 가능한 수열."""
    out, x = [], seed
    for _ in range(n):
        x = (1103515245 * x + 12345) % (2 ** 31)
        u1 = (x % 10000 + 1) / 10001.0
        x = (1103515245 * x + 12345) % (2 ** 31)
        u2 = (x % 10000 + 1) / 10001.0
        z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        out.append(mean + sd * z)
    return out


def test_deflation_kills_a_result_that_survived_without_correction():
    """핵심 성질: 시도 횟수를 정직하게 넣으면 같은 성과가 탈락할 수 있다."""
    # 보정 없이는 0.978 로 통과하지만, 1,000회 시도를 선언하면 0.108 로 떨어진다.
    rets = _returns(mean=0.10, sd=1.0, n=400)
    undeflated = deflated_sharpe(rets, n_trials=1)
    deflated = deflated_sharpe(rets, n_trials=1000)
    assert undeflated > deflated
    assert undeflated >= DSR_THRESHOLD > deflated


def test_negative_skew_lowers_the_probability():
    """손절 기반 전략은 음의 왜도를 갖는다 — 샤프가 그만큼 부풀려져 있다."""
    plain = probabilistic_sharpe(0.10, 0.0, 500, skew=0.0, kurtosis=3.0)
    skewed = probabilistic_sharpe(0.10, 0.0, 500, skew=-1.5, kurtosis=3.0)
    assert skewed < plain


def test_fat_tails_lower_the_probability():
    plain = probabilistic_sharpe(0.10, 0.0, 500, skew=0.0, kurtosis=3.0)
    fat = probabilistic_sharpe(0.10, 0.0, 500, skew=0.0, kurtosis=12.0)
    assert fat < plain


def test_losing_strategy_never_passes():
    assert deflated_sharpe(_returns(-0.05, 1.0, 400), n_trials=1) < DSR_THRESHOLD


def test_degenerate_inputs_do_not_crash():
    assert sharpe_ratio([]) == 0.0
    assert sharpe_ratio([1.0]) == 0.0
    assert sharpe_ratio([2.0, 2.0, 2.0]) == 0.0     # 분산 0
    assert deflated_sharpe([], 10) == 0.0


# ---- 회전율 게이트 ---------------------------------------------------------

def _pnls(n_per_fold=30, value=100.0):
    return [[value] * n_per_fold for _ in range(4)]


def test_turnover_gate_matches_novy_marx_velikov_line():
    """월 편도 50% = 연 양방향 1,200% = turnover_ratio 12.0."""
    assert wf.MAX_ANNUAL_TURNOVER == 12.0


def test_high_turnover_fails_even_when_everything_else_passes():
    """비용이 우위를 잡아먹는 쪽에 있다는 것 자체가 탈락 사유다."""
    good = wf.judge(_pnls(), max_drawdown=-0.10, annual_turnover=5.0)
    bad = wf.judge(_pnls(), max_drawdown=-0.10, annual_turnover=54.4)
    names = [c[0] for c in bad.checks]
    assert any("회전율" in n for n in names)
    assert good.passed and not bad.passed


def test_turnover_check_is_omitted_when_not_supplied():
    """구버전 리포트를 다시 판정할 때 없던 항목으로 결과가 뒤집히면 안 된다."""
    v = wf.judge(_pnls(), max_drawdown=-0.10)
    assert not any("회전율" in c[0] for c in v.checks)
    assert v.passed


def test_the_five_discarded_runs_would_all_have_been_flagged():
    """폐기한 5안의 실측 회전율 — 전부 이 선 위이거나 걸쳐 있었다."""
    for measured in (12.1, 12.5, 23.1, 54.0, 54.4):
        v = wf.judge(_pnls(), max_drawdown=-0.10, annual_turnover=measured)
        turnover_ok = [ok for name, ok, _ in v.checks if "회전율" in name][0]
        assert not turnover_ok, f"{measured}배가 통과해서는 안 된다"


# ---- 리포트 배선 -----------------------------------------------------------

def test_report_block_defaults_to_uncorrected_and_says_so():
    block = wf._overfitting_block(_pnls(), n_trials=1)
    assert block["n_trials_declared"] == 1
    assert block["expected_max_sharpe_from_luck"] == 0.0
    assert "n_trials" in block["note"]


def test_report_block_deflates_when_trials_declared():
    rets = [_returns(0.06, 1.0, 100, seed=s) for s in (1, 2, 3, 4)]
    low = wf._overfitting_block(rets, n_trials=1)
    high = wf._overfitting_block(rets, n_trials=2000)
    assert high["deflated_sharpe"] < low["deflated_sharpe"]
    assert high["trade_sharpe"] == low["trade_sharpe"]   # 성과 자체는 불변
