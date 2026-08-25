"""Walk-forward 비교 규격을 못박는다 — 러너를 짓기 전에.

이 테스트가 존재하는 이유는 러너의 버그를 잡기 위해서가 아니다. **규격이
결과를 본 뒤에 조용히 바뀌는 것을 막기 위해서**다. 창 길이나 판정 기준을
누가 나중에 손대면 여기서 실패한다.

fold 표는 실행 전에 합의된 값이며, 아래 EXPECTED 는 그 표를 그대로 옮긴 것이다.
"""
import pytest

from autotrader import walkforward as wf
from autotrader.registry import ValidationThresholds

# 합의된 배치표 (1-기반 봉 번호, 닫힌 구간). 2,500봉 기준 정확히 4 fold.
EXPECTED = [
    # fold, train,        purge1,        validation,    purge2,        oos
    (1, (1, 1000), (1001, 1020), (1021, 1270), (1271, 1290), (1291, 1540)),
    (2, (1, 1250), (1251, 1270), (1271, 1520), (1521, 1540), (1541, 1790)),
    (3, (1, 1500), (1501, 1520), (1521, 1770), (1771, 1790), (1791, 2040)),
    (4, (1, 1750), (1751, 1770), (1771, 2020), (2021, 2040), (2041, 2290)),
]


# ---- 배치 -------------------------------------------------------------------

def test_2500_bars_yields_exactly_the_agreed_table():
    folds = wf.build_folds(2500)
    assert len(folds) == 4, "2,500봉에서는 정확히 4개 fold 여야 한다"
    for f, (idx, train, p1, val, p2, oos) in zip(folds, EXPECTED):
        assert (f.index, f.train, f.purge_after_train, f.validation,
                f.purge_after_validation, f.oos) == (idx, train, p1, val, p2, oos)


def test_short_fifth_fold_is_not_invented():
    """2291~2500 의 210봉은 규격 길이(250)를 못 채우므로 fold 가 되지 않는다.

    짧은 fold 를 끼우면 표본이 작아 판정이 흔들리고, '몇 개까지 만들지' 가
    결과를 보고 정하는 손잡이가 된다.
    """
    folds = wf.build_folds(2500)
    assert folds[-1].oos[1] == 2290
    assert wf.unused_tail(2500) == (2291, 2500)
    assert 2500 - 2290 == 210 < wf.OOS_BARS


def test_train_is_expanding_from_bar_one():
    for f in wf.build_folds(2500):
        assert f.train[0] == 1, "시작점은 1일로 고정 (expanding)"
    ends = [f.train[1] for f in wf.build_folds(2500)]
    assert ends == [1000, 1250, 1500, 1750]
    assert all(b - a == wf.STEP_BARS for a, b in zip(ends, ends[1:]))


@pytest.mark.parametrize("f", wf.build_folds(2500), ids=lambda f: f"fold{f.index}")
def test_purge_sits_on_both_boundaries_with_no_gap_or_overlap(f):
    """TRAIN → purge 20 → VALIDATION → purge 20 → OOS, 빈틈도 겹침도 없이."""
    for lo, hi in (f.purge_after_train, f.purge_after_validation):
        assert hi - lo + 1 == wf.PURGE_BARS
    assert f.purge_after_train[0] == f.train[1] + 1
    assert f.validation[0] == f.purge_after_train[1] + 1
    assert f.purge_after_validation[0] == f.validation[1] + 1
    assert f.oos[0] == f.purge_after_validation[1] + 1


@pytest.mark.parametrize("f", wf.build_folds(2500), ids=lambda f: f"fold{f.index}")
def test_window_lengths_match_spec(f):
    assert f.validation[1] - f.validation[0] + 1 == wf.VALIDATION_BARS
    assert f.oos[1] - f.oos[0] + 1 == wf.OOS_BARS


def test_oos_windows_do_not_overlap_each_other():
    """OOS 가 겹치면 같은 거래가 두 번 채점돼 합산 PF 가 부풀려진다."""
    oos = [f.oos for f in wf.build_folds(2500)]
    for (a_lo, a_hi), (b_lo, _) in zip(oos, oos[1:]):
        assert b_lo > a_hi


def test_slice_conversion_is_off_by_one_safe():
    assert wf.as_slice((1, 1000)) == (0, 1000)
    assert wf.as_slice((1291, 1540)) == (1290, 1540)
    bars = list(range(1, 2501))          # 봉 1..2500
    lo, hi = wf.as_slice((1291, 1540))
    window = bars[lo:hi]
    assert window[0] == 1291 and window[-1] == 1540 and len(window) == wf.OOS_BARS


def test_too_short_history_yields_no_folds():
    assert wf.build_folds(1000) == []
    assert wf.build_folds(1539) == []     # OOS 가 한 봉 모자람
    assert len(wf.build_folds(1540)) == 1
    assert wf.unused_tail(1000) == (0, 0)


# ---- 채점 규칙 --------------------------------------------------------------

def test_combined_pf_is_pooled_not_averaged():
    """합산 PF = 전체 총이익 ÷ 전체 총손실. fold 별 PF 의 평균이 아니다."""
    fold_a = [10.0, -5.0]                 # PF 2.0
    fold_b = [1.0, -100.0]                # PF 0.01
    pooled = wf.combined_profit_factor([fold_a, fold_b])
    assert pooled == pytest.approx(11.0 / 105.0)
    naive_mean = (2.0 + 0.01) / 2
    assert pooled < naive_mean, "평균을 쓰면 작은 fold 가 과대 대표된다"


def test_combined_pf_handles_lossless_fold():
    """손실이 0 인 fold 하나가 평균을 무한대로 오염시키지 않는지."""
    assert wf.combined_profit_factor([[5.0], [1.0, -2.0]]) == pytest.approx(3.0)
    assert wf.combined_profit_factor([[5.0]]) == float("inf")
    assert wf.combined_profit_factor([[-5.0]]) == 0.0
    assert wf.combined_profit_factor([[]]) == 0.0


def test_gross_profit_excludes_losses_and_zeros():
    pnls = [10.0, -3.0, 0.0, 2.5]
    assert wf.gross_profit(pnls) == pytest.approx(12.5)
    assert wf.gross_loss(pnls) == pytest.approx(3.0)


def test_concentration_uses_gross_profit_not_net():
    """분모는 fold 별 총이익(양수 PnL 합)의 합. 순이익이 아니다."""
    # fold1 총이익 90, fold2 총이익 10 → 집중도 0.9. 순이익으로 재면 값이 달라진다.
    folds = [[90.0, -80.0], [10.0]]
    assert wf.profit_concentration(folds) == pytest.approx(0.9)
    assert wf.profit_concentration(folds) > wf.MAX_PROFIT_CONCENTRATION


def test_zero_total_profit_fails_stability():
    """총이익이 0 이면 집중도를 잴 수 없다 — 통과가 아니라 실패로 붙인다."""
    assert wf.profit_concentration([[-1.0], [-2.0]]) == 1.0
    assert wf.profit_concentration([[], []]) == 1.0


# ---- 기존 승격 기준을 낮추지 않았는가 ---------------------------------------

def test_thresholds_do_not_loosen_the_existing_gate():
    assert wf.MIN_TOTAL_OOS_PROFIT_FACTOR == ValidationThresholds().min_oos_profit_factor
    assert wf.MIN_TOTAL_OOS_PROFIT_FACTOR == 1.20
    assert wf.MAX_DRAWDOWN == -0.25
    assert wf.MIN_TOTAL_TRADES == 100
    assert wf.MIN_TRADES_PER_FOLD == 20
    assert wf.MIN_FOLDS_WITH_PF_ABOVE_ONE == 3
    assert wf.MAX_PROFIT_CONCENTRATION == 0.50
    assert wf.MIN_TOTAL_OOS_NET_PROFIT == 0.0
