from autotrader.data.base import DataError
from autotrader.data.synthetic import SyntheticProvider
from autotrader.reconciler import SourceReconciler


def _pred_up(view):
    bars = view.bars()
    return bars[-1].close > bars[0].close


def test_reconcile_partitions_sets_correctly():
    a = SyntheticProvider(seed=7)
    b = SyntheticProvider(seed=13)
    universe = a.universe()
    report = SourceReconciler(a, b).reconcile(universe, _pred_up)
    # 세 집합은 서로 겹치지 않아야 하며 …
    both = set(report.in_both)
    only_a = set(report.only_in_primary)
    only_b = set(report.only_in_secondary)
    assert not (both & only_a) and not (both & only_b) and not (only_a & only_b)
    # … "합쳐도" 유니버스 크기를 넘지 않는다 (False 인 종목은 어느 셋에도 안 들어감).
    assert len(both | only_a | only_b) <= len(universe)


class _Ghost(SyntheticProvider):
    def history(self, sym, limit=500):
        if sym == "GHOST":
            raise DataError("no data")
        return super().history(sym, limit)


def test_reconcile_reports_missing_symbols_per_source():
    a = SyntheticProvider(seed=7)
    b = _Ghost(seed=7)
    universe = list(a.universe()) + ["GHOST"]
    r = SourceReconciler(a, b).reconcile(universe, _pred_up)
    assert "GHOST" in r.secondary_missing_data
    assert "GHOST" in r.primary_missing_data  # A 도 GHOST 없음
    assert r.leak_count >= 0


def test_identical_sources_produce_no_leak():
    a = SyntheticProvider(seed=7)
    b = SyntheticProvider(seed=7)
    r = SourceReconciler(a, b).reconcile(a.universe(), _pred_up)
    assert not r.only_in_primary and not r.only_in_secondary
