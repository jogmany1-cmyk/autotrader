"""KiwoomProvider — 캐시·병합·인터페이스 검증. 실 네트워크 호출은 안 함."""
import csv
import os
import tempfile
from datetime import datetime

import pytest

from autotrader.config import KiwoomConfig
from autotrader.data import KiwoomProvider
from autotrader.data.base import DataError, DataProvider
from autotrader.data.kiwoom import _merge_bars
from autotrader.models import Bar


def test_missing_credentials_raise_dataerror():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(DataError):
            KiwoomProvider(KiwoomConfig(), cache_dir=d)


def test_selects_paper_url_by_default():
    with tempfile.TemporaryDirectory() as d:
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                            cache_dir=d)
        assert "mockapi" in kp.base


def test_selects_real_url_when_not_paper():
    with tempfile.TemporaryDirectory() as d:
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y",
                                          is_paper=False),
                            cache_dir=d)
        assert kp.base.startswith("https://api.kiwoom.com")


def test_conforms_to_dataprovider_interface():
    with tempfile.TemporaryDirectory() as d:
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                            cache_dir=d)
        assert isinstance(kp, DataProvider)


def test_cache_dir_is_created():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "nested", "kiwoom")
        KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                       cache_dir=target)
        assert os.path.isdir(target)


def test_merge_deduplicates_by_date_and_prefers_fresh():
    old = [Bar(datetime(2026, 8, 24), 100, 105, 99, 102, 1000)]
    new = [Bar(datetime(2026, 8, 24), 200, 205, 199, 202, 2000),
           Bar(datetime(2026, 8, 25), 210, 215, 209, 212, 2100)]
    merged = _merge_bars(old, new)
    assert len(merged) == 2
    # 같은 날짜는 new 가 이겼는지
    assert merged[0].close == 202
    assert merged[1].close == 212
    # 정렬됐는지
    assert merged[0].ts < merged[1].ts


def test_cache_roundtrip_reads_previously_written_bars():
    with tempfile.TemporaryDirectory() as d:
        # 캐시 파일을 미리 만들어두고 provider 가 읽을 수 있는지
        path = os.path.join(d, "005930.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            w.writerow(["2026-08-20", 70000, 71000, 69500, 70500, 12345])
            w.writerow(["2026-08-21", 70600, 71200, 70100, 71000, 15000])
        kp = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                            cache_dir=d)
        cached = kp._load_cache("005930")
        assert len(cached) == 2 and cached[0].close == 70500
        # 순서 검증
        assert cached[0].ts < cached[1].ts
