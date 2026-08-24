"""게이트가 조용히 통과되는 경로들에 대한 회귀 테스트.

여기 모인 것들은 전부 "실패했는데 성공으로 보고된다" 는 한 가지 실패 양식이다.
크론이나 CI 가 종료코드만 보고 다음 단계로 넘어가기 때문에, 조용한 성공은
시끄러운 실패보다 훨씬 비싸다.
"""
import io
import os
from datetime import date, datetime, timedelta

import pytest

from autotrader.dataquality import DataQualityChecker
from autotrader.market import now_kst


# ---- 수집이 0건인데 종료코드 0 --------------------------------------

def _fetch_args(tmp_path, symbols):
    class A:
        cache = str(tmp_path)
        symbol = symbols
        real = False
        debug = False
        min_interval = None
        minutes = 0
        limit = 100
    return A()


def test_fetch_with_empty_universe_does_not_report_success(tmp_path, monkeypatch, capsys):
    """유니버스가 비면 크론이 '수집 성공' 으로 읽으면 안 된다."""
    from autotrader import cli

    class FakeProvider:
        debug = False
        min_interval = 1.0
        last_failures = []

        def __init__(self, *a, **k):
            pass

        def universe(self):
            return []

        def refresh_all(self, symbols, limit=0):
            raise AssertionError("빈 유니버스로 수집을 시도하면 안 된다")

    import autotrader.data as data_mod
    monkeypatch.setattr(data_mod, "KiwoomProvider", FakeProvider)
    from autotrader.config import KiwoomConfig
    monkeypatch.setattr(KiwoomConfig, "from_env", classmethod(lambda cls: cls(
        app_key="k", app_secret="s")))

    rc = cli.cmd_fetch(_fetch_args(tmp_path, None))
    assert rc != 0, "빈 유니버스인데 성공(0)으로 끝났다"
    assert "종목이 없습니다" in capsys.readouterr().out


def test_fetch_all_failed_is_not_success(tmp_path, monkeypatch):
    """ok=0 fail=N 이면 실패다."""
    from autotrader import cli

    class FakeProvider:
        debug = False
        min_interval = 1.0
        last_failures = [("005930", "시세 없음")]

        def __init__(self, *a, **k):
            pass

        def universe(self):
            return ["005930"]

        def refresh_all(self, symbols, limit=0):
            return 0, 1

    import autotrader.data as data_mod
    monkeypatch.setattr(data_mod, "KiwoomProvider", FakeProvider)
    from autotrader.config import KiwoomConfig
    monkeypatch.setattr(KiwoomConfig, "from_env", classmethod(lambda cls: cls(
        app_key="k", app_secret="s")))

    # != 0 만 보면 약하다. fail>0 경로가 이미 1 을 주므로, ok==0 분기를
    # 지워도 통과해 버린다. "전부 실패" 는 부분 실패(1)와 구분되는 2 여야 한다.
    assert cli.cmd_fetch(_fetch_args(tmp_path, None)) == 2


# ---- 깨진 파일 하나가 명령 전체를 죽인다 ---------------------------

def _write(path, rows, encoding="utf-8"):
    with io.open(path, "w", encoding=encoding, newline="") as fh:
        fh.write("date,open,high,low,close,volume\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")


def _clean_rows(n=30, start=date(2024, 1, 1)):
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        out.append((d.isoformat(), 100, 101, 99, 100, 1000))
    return out


def test_one_broken_encoding_does_not_kill_the_whole_check(tmp_path):
    """cp949 CSV 한 개 때문에 나머지 종목 결과까지 사라지면 게이트가 아니다."""
    _write(tmp_path / "000001.csv", _clean_rows())
    with io.open(tmp_path / "000002.csv", "w", encoding="cp949", newline="") as fh:
        fh.write("날짜,시가,고가,저가,종가,거래량\n")
        fh.write("2024-01-01,100,101,99,100,1000\n")
        fh.write("2024-01-02,한글깨짐,101,99,100,1000\n")

    rep = DataQualityChecker().check_csv_dir(str(tmp_path))
    checked = {r.symbol for r in rep.symbols} | {s for s, _ in rep.unreadable}
    assert "000001" in checked, "멀쩡한 종목의 검사 결과가 사라졌다"
    assert "000002" in checked, "읽지 못한 종목이 보고에서 누락됐다"


# ---- 분봉 캐시를 일봉 종목으로 오인 --------------------------------

def test_minute_cache_files_are_not_mistaken_for_symbols(tmp_path):
    """`{종목}_5m.csv` 를 종목으로 집으면 duplicate_date 로 멀쩡한 폴더가 FAIL 난다."""
    _write(tmp_path / "005930.csv", _clean_rows())
    _write(tmp_path / "005930_5m.csv", [
        ("2024-01-02 09:05:00", 100, 101, 99, 100, 10),
        ("2024-01-02 09:10:00", 100, 101, 99, 100, 10),
    ])
    rep = DataQualityChecker().check_csv_dir(str(tmp_path))
    assert {r.symbol for r in rep.symbols} == {"005930"}
    assert not any(i.code == "duplicate_date" for i in rep.all_issues)


def test_minute_file_can_still_be_checked_explicitly(tmp_path):
    """이름을 직접 주면 분봉도 검사한다 — 숨기는 게 아니라 기본에서 빼는 것."""
    _write(tmp_path / "005930_5m.csv", [
        ("2024-01-02 09:05:00", 100, 101, 99, 100, 10),
    ])
    rep = DataQualityChecker().check_csv_dir(str(tmp_path), ["005930_5m"])
    assert [r.symbol for r in rep.symbols] == ["005930_5m"]


# ---- UTC 컨테이너에서 오늘 봉이 미래 봉으로 찍힌다 -----------------

def test_as_of_follows_kst_not_the_host_clock():
    """UTC 서버에서 KST 밤 9시 이후면 date.today() 가 어제를 준다.

    그러면 오늘 수집한 봉이 'future_bar' ERROR 가 되어, 정상 데이터가
    무결성 검사에서 반려된다.
    """
    assert DataQualityChecker().as_of == now_kst().date()


def test_bar_collected_today_kst_is_not_flagged_as_future():
    today = now_kst().date()
    bars_rows = []
    for i in range(30, -1, -1):
        d = today - timedelta(days=i)
        bars_rows.append((d, 100.0, 101.0, 99.0, 100.0, 1000))
    from autotrader.models import Bar
    bars = [Bar(datetime(d.year, d.month, d.day), o, h, l, c, v)
            for d, o, h, l, c, v in bars_rows]
    rep = DataQualityChecker().check_bars("005930", bars)
    assert not any(i.code == "future_bar" for i in rep.issues)
