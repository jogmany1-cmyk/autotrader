"""v1.0 — 잡 디스패처 + KRX 유니버스 + 분봉 캐시 테스트."""
import os
import tempfile
from datetime import date, datetime

import pytest

from autotrader import jobs
from autotrader.data.krx_universe import KrxUniverse, UniverseSnapshot


# --- KRX 유니버스 ------------------------------------------------------

def test_universe_snapshot_roundtrip_via_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "u.jsonl")
        u = KrxUniverse(path)
        u.add(UniverseSnapshot(date(2024, 1, 2), "KOSPI", ["A", "B"]))
        u.add(UniverseSnapshot(date(2024, 1, 2), "KOSDAQ", ["C"]))
        u.save()
        u2 = KrxUniverse(path)
        assert u2.snapshot_dates() == [date(2024, 1, 2)]
        assert set(u2.symbols_on(date(2024, 1, 2), "ALL")) == {"A", "B", "C"}


def test_symbols_on_uses_most_recent_prior_snapshot():
    with tempfile.TemporaryDirectory() as d:
        u = KrxUniverse(os.path.join(d, "u.jsonl"))
        u.add(UniverseSnapshot(date(2024, 1, 2), "KOSPI", ["A"]))
        u.add(UniverseSnapshot(date(2024, 6, 1), "KOSPI", ["A", "B"]))
        # 3월 15일에는 1월 2일 스냅샷을 써야 함
        assert u.symbols_on(date(2024, 3, 15), "KOSPI") == ["A"]
        assert set(u.symbols_on(date(2024, 7, 1), "KOSPI")) == {"A", "B"}


def test_union_between_captures_delisted_symbols():
    with tempfile.TemporaryDirectory() as d:
        u = KrxUniverse(os.path.join(d, "u.jsonl"))
        # A 는 6월에 상장폐지된 종목이라 가정
        u.add(UniverseSnapshot(date(2024, 1, 2), "KOSPI", ["A", "B"]))
        u.add(UniverseSnapshot(date(2024, 7, 1), "KOSPI", ["B"]))
        # 연간 전체에서는 A 가 살아 있는 시점이 있었으므로 합집합에 포함돼야 함
        assert set(u.union_between(date(2024, 1, 1), date(2024, 12, 31), "KOSPI")) == {"A", "B"}


def test_no_snapshot_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        u = KrxUniverse(os.path.join(d, "u.jsonl"))
        assert u.symbols_on(date(2024, 1, 2)) == []


# --- 잡 디스패처 ------------------------------------------------------

def test_run_unknown_job_raises():
    with pytest.raises(KeyError):
        jobs.run("NOPE")


def test_morning_entry_runs_with_csv_fallback():
    with tempfile.TemporaryDirectory() as d:
        ctx = jobs.JobContext(cache_dir=d, use_kiwoom=False)
        msg = jobs.job_morning_entry(ctx, now=datetime(2026, 8, 24, 10, 0))
        assert "morning-entry" in msg


def test_eod_flat_runs_with_csv_fallback():
    with tempfile.TemporaryDirectory() as d:
        ctx = jobs.JobContext(cache_dir=d, use_kiwoom=False)
        msg = jobs.job_eod_flat(ctx, now=datetime(2026, 8, 24, 15, 0))
        assert "eod-flat" in msg


def test_post_analysis_reports_registry_status():
    with tempfile.TemporaryDirectory() as d:
        ctx = jobs.JobContext(cache_dir=d, use_kiwoom=False)
        msg = jobs.job_post_analysis(ctx)
        assert "post-analysis" in msg


def test_all_registered_jobs_have_callables():
    for name, fn in jobs.JOBS.items():
        assert callable(fn), f"{name} not callable"


# --- KiwoomProvider 분봉 검증 (인터페이스만) ---------------------------

def test_kiwoom_provider_history_minutes_rejects_bad_interval():
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider
    from autotrader.data.base import DataError
    with tempfile.TemporaryDirectory() as d:
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                            cache_dir=d)
        with pytest.raises(DataError):
            kp.history_minutes("005930", interval=7)  # 지원 안 되는 간격


def test_kiwoom_provider_has_refresh_minutes_method():
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider
    with tempfile.TemporaryDirectory() as d:
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                            cache_dir=d)
        assert callable(getattr(kp, "refresh_minutes", None))
