"""데이터 무결성 검증기 회귀 테스트.

각 검사는 "이 결함이 실제로 잡히는가" 와 "정상 데이터에 거짓 경보를 내지 않는가"
두 방향으로 고정한다. 거짓 경보를 내는 검증기는 아무도 안 보게 되고, 그러면
검증기가 없는 것과 같기 때문이다.
"""
import csv
import json
from datetime import date, datetime, timedelta

from autotrader.cli import main
from autotrader.dataquality import (ERROR, WARN, DataQualityChecker,
                                    QualityLimits)
from autotrader.market import is_trading_day
from autotrader.models import Bar

AS_OF = date(2025, 6, 30)


def _trading_days(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(days, closes=None, volume=100_000.0):
    """종가 리스트(기본: 완만한 상승)로 OHLC 가 정합한 봉 시퀀스를 만든다."""
    bars = []
    prev = None
    for i, d in enumerate(days):
        c = closes[i] if closes is not None else 10_000.0 * (1.001 ** i)
        o = prev if prev is not None else c
        bars.append(Bar(ts=datetime(d.year, d.month, d.day),
                        open=o, high=max(o, c) * 1.005, low=min(o, c) * 0.995,
                        close=c, volume=volume))
        prev = c
    return bars


def _checker(as_of=AS_OF, **limit_kw):
    kw = {"min_bars": 10}
    kw.update(limit_kw)
    return DataQualityChecker(QualityLimits(**kw), as_of=as_of)


def _codes(report):
    return {i.code for i in report.issues}


# ---- 정상 데이터에는 아무 말도 하지 않는다 -----------------------------

def test_clean_series_reports_nothing():
    days = _trading_days(date(2025, 1, 2), 100)
    rep = _checker(as_of=days[-1]).check_bars("GOOD", _bars(days))
    assert rep.ok, [i.as_line() for i in rep.issues]
    assert rep.n_bars == 100
    assert rep.first_day == days[0] and rep.last_day == days[-1]


def test_empty_series_is_error():
    rep = _checker().check_bars("EMPTY", [])
    assert _codes(rep) == {"empty"}
    assert rep.errors


# ---- ERROR: 데이터 자체가 모순인 경우 ----------------------------------

def test_high_below_low_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars[5] = Bar(ts=bars[5].ts, open=100, high=90, low=95, close=98, volume=10)
    rep = _checker().check_bars("X", bars)
    issue = next(i for i in rep.issues if i.code == "ohlc_violation")
    assert issue.severity == ERROR
    # 고저 역전은 "범위 밖 종가" 와 원인이 다르므로 그 분기가 잡았음을 못박는다.
    assert "high(90)" in issue.detail and "< low(95)" in issue.detail


def test_close_outside_high_low_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars[7] = Bar(ts=bars[7].ts, open=100, high=105, low=99, close=120, volume=10)
    rep = _checker().check_bars("X", bars)
    assert "ohlc_violation" in _codes(rep)


def test_nonpositive_price_is_error_and_skips_ohlc_check():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars[3] = Bar(ts=bars[3].ts, open=100, high=101, low=99, close=0.0, volume=10)
    rep = _checker().check_bars("X", bars)
    assert "nonpositive_price" in _codes(rep)
    # 가격이 0 이면 OHLC 논리 위반까지 겹쳐 두 번 외치지 않는다.
    assert "ohlc_violation" not in _codes(rep)


def test_duplicate_date_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars.append(bars[4])
    bars.sort(key=lambda b: b.ts)
    rep = _checker().check_bars("X", bars)
    assert "duplicate_date" in _codes(rep)


def test_unsorted_series_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars[10], bars[11] = bars[11], bars[10]
    rep = _checker().check_bars("X", bars)
    assert "unsorted" in _codes(rep)


def test_future_bar_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[20]).check_bars("X", _bars(days))
    assert "future_bar" in _codes(rep)


def test_negative_volume_is_error():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    bars[2] = Bar(ts=bars[2].ts, open=100, high=101, low=99, close=100, volume=-5)
    rep = _checker().check_bars("X", bars)
    assert "negative_volume" in _codes(rep)


# ---- WARN: 값은 성립하지만 백테스트를 왜곡하는 경우 --------------------

def test_missing_trading_days_warns():
    days = _trading_days(date(2025, 1, 2), 60)
    kept = days[:20] + days[23:]          # 거래일 3일 삭제
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(kept))
    assert "missing_trading_days" in _codes(rep)
    issue = next(i for i in rep.issues if i.code == "missing_trading_days")
    assert issue.severity == WARN and issue.count == 3
    # 개수만으로는 고칠 수 없다 — 실제 날짜가 메시지에 있어야 한다.
    for d in days[20:23]:
        assert d.isoformat() in issue.detail


def test_long_gap_is_called_out_separately():
    days = _trading_days(date(2025, 1, 2), 60)
    kept = days[:20] + days[32:]          # 12거래일 연속 결측
    rep = _checker(as_of=days[-1], long_gap_days=5).check_bars("X", _bars(kept))
    assert "long_gap" in _codes(rep)


def test_holiday_only_gap_does_not_warn():
    """설 연휴처럼 휴장일만 비어 있으면 결측이 아니다 (거짓 경보 방지)."""
    days = _trading_days(date(2025, 1, 2), 60)   # 1/28~1/30 설 연휴가 포함된 구간
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days))
    assert "missing_trading_days" not in _codes(rep)


def test_calendar_uncovered_skips_gap_check():
    """휴장일 표(2024~2027) 밖 구간은 결측 판정을 하지 않고 그 사실을 알린다."""
    days = [d for d in (date(2021, 1, 1) + timedelta(days=k) for k in range(120))
            if d.weekday() < 5]
    rep = _checker(as_of=date(2021, 5, 3)).check_bars("X", _bars(days))
    assert "calendar_uncovered" in _codes(rep)
    assert "missing_trading_days" not in _codes(rep)


def test_bar_on_closed_day_warns():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = _bars(days)
    saturday = date(2025, 1, 4)
    assert not is_trading_day(saturday)
    bars.append(Bar(ts=datetime(2025, 1, 4), open=100, high=101, low=99,
                    close=100, volume=10))
    bars.sort(key=lambda b: b.ts)
    rep = _checker(as_of=days[-1]).check_bars("X", bars)
    assert "bar_on_closed_day" in _codes(rep)


def test_split_is_recognised_as_split_not_generic_jump():
    days = _trading_days(date(2025, 1, 2), 60)
    closes = [10_000.0] * 30 + [2_000.0] * 30      # 1/5 액면분할 미조정
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days, closes))
    assert "split_suspect" in _codes(rep)
    assert "price_jump" not in _codes(rep)
    assert "1/5 분할" in next(i for i in rep.issues if i.code == "split_suspect").detail


def test_non_split_jump_is_generic_price_jump():
    days = _trading_days(date(2025, 1, 2), 60)
    closes = [10_000.0] * 30 + [16_500.0] * 30     # +65% — 분할 비율과 무관
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days, closes))
    assert "price_jump" in _codes(rep)
    assert "split_suspect" not in _codes(rep)


def test_within_price_limit_move_does_not_warn():
    days = _trading_days(date(2025, 1, 2), 40)
    closes = [10_000.0] * 20 + [12_500.0] * 20     # +25% — 상한가 범위 안
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days, closes))
    assert "price_jump" not in _codes(rep)


def test_short_history_warns():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[-1], min_bars=200).check_bars("X", _bars(days))
    assert "short_history" in _codes(rep)


def test_zero_volume_ratio_warns():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days, volume=0.0))
    assert "zero_volume" in _codes(rep)


def test_flat_bars_warn():
    days = _trading_days(date(2025, 1, 2), 30)
    bars = [Bar(ts=datetime(d.year, d.month, d.day), open=100, high=100,
                low=100, close=100, volume=10) for d in days]
    rep = _checker(as_of=days[-1]).check_bars("X", bars)
    assert "flat_bars" in _codes(rep)


def test_stale_data_warns():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[-1] + timedelta(days=40)).check_bars("X", _bars(days))
    assert "stale_data" in _codes(rep)


def test_fresh_data_does_not_warn_stale():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[-1]).check_bars("X", _bars(days))
    assert "stale_data" not in _codes(rep)


# ---- 리포트 형태 -------------------------------------------------------

def test_repeated_issues_are_collapsed_into_one_row():
    days = _trading_days(date(2025, 1, 2), 30)
    rep = _checker(as_of=days[0]).check_bars("X", _bars(days))
    future = [i for i in rep.issues if i.code == "future_bar"]
    assert len(future) == 1                 # 29줄이 아니라 1줄로 묶인다
    assert future[0].count == 29
    assert len(future[0].samples) == 3      # 표본 날짜는 앞 3건만
    assert "…" in future[0].as_line()


def test_gate_semantics_error_vs_warn():
    days = _trading_days(date(2025, 1, 2), 30)
    checker = _checker(as_of=days[-1], min_bars=200)
    warn_only = checker.check_bars("W", _bars(days))          # short_history 만
    assert warn_only.warnings and not warn_only.errors

    from autotrader.dataquality import QualityReport
    rep = QualityReport(as_of=days[-1], symbols=[warn_only])
    assert rep.passed() is True                # 기본 게이트는 WARN 을 통과시키고
    assert rep.passed(strict=True) is False    # --strict 에서는 막는다

    bars = _bars(days)
    bars[1] = Bar(ts=bars[1].ts, open=100, high=90, low=95, close=98, volume=10)
    rep2 = QualityReport(as_of=days[-1], symbols=[checker.check_bars("E", bars)])
    assert rep2.passed() is False


def test_report_dict_is_json_serialisable():
    days = _trading_days(date(2025, 1, 2), 30)
    from autotrader.dataquality import QualityReport
    rep = QualityReport(as_of=days[-1],
                        symbols=[_checker(as_of=days[0]).check_bars("X", _bars(days))])
    blob = json.loads(json.dumps(rep.as_dict(), ensure_ascii=False))
    assert blob["as_of"] == days[-1].isoformat()
    assert blob["counts_by_code"]["future_bar"] == 29
    assert blob["passed"] is False


# ---- CSV 경로: 조용히 버려지는 행을 드러낸다 ---------------------------

def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


def _rows(days):
    return [[d.isoformat(), 100, 101, 99, 100, 1000] for d in days]


def test_dropped_rows_are_detected(tmp_path):
    days = _trading_days(date(2025, 1, 2), 30)
    rows = _rows(days)
    rows[10] = [days[10].isoformat(), "", "", "", "", ""]        # 숫자 칸 결손
    rows[20] = [days[20].isoformat(), "n/a", "n/a", "n/a", "n/a", "0"]
    _write_csv(tmp_path / "T.csv", rows)
    rep = _checker(as_of=days[-1]).check_csv_dir(str(tmp_path))
    issue = next(i for i in rep.symbols[0].issues if i.code == "dropped_rows")
    assert issue.severity == ERROR and issue.count == 2
    assert rep.passed() is False


def test_unparseable_date_makes_symbol_unreadable(tmp_path):
    days = _trading_days(date(2025, 1, 2), 30)
    rows = _rows(days)
    rows.insert(5, ["합계", 1, 1, 1, 1, 1])
    _write_csv(tmp_path / "T.csv", rows)
    rep = _checker(as_of=days[-1]).check_csv_dir(str(tmp_path))
    assert [s for s, _ in rep.unreadable] == ["T"]
    assert rep.passed() is False


def test_clean_csv_dir_passes(tmp_path):
    days = _trading_days(date(2025, 1, 2), 30)
    _write_csv(tmp_path / "T.csv", _rows(days))
    rep = _checker(as_of=days[-1]).check_csv_dir(str(tmp_path))
    # 종가가 고정이라 flat 봉이지만 O/H/L 이 달라 flat_bars 는 걸리지 않는다.
    assert rep.passed() is True
    assert rep.clean_symbols == ["T"]


# ---- CLI 게이트 --------------------------------------------------------

def test_cli_exit_code_marks_gate_result(tmp_path, capsys):
    days = _trading_days(date(2025, 1, 2), 30)
    _write_csv(tmp_path / "OK.csv", _rows(days))
    argv = ["--csv", str(tmp_path), "validate-data",
            "--as-of", days[-1].isoformat(), "--min-bars", "10"]
    assert main(argv) == 0
    assert "PASS" in capsys.readouterr().out

    bad = _rows(days)
    bad[3] = [days[3].isoformat(), 100, 90, 95, 98, 10]   # high < low
    _write_csv(tmp_path / "BAD.csv", bad)
    assert main(argv) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "ohlc_violation" in out


def test_cli_strict_flag_rejects_warnings(tmp_path):
    days = _trading_days(date(2025, 1, 2), 30)
    _write_csv(tmp_path / "OK.csv", _rows(days))
    base = ["--csv", str(tmp_path), "validate-data",
            "--as-of", days[-1].isoformat(), "--min-bars", "200"]
    assert main(base) == 0            # short_history 는 WARN 이라 통과
    assert main(base + ["--strict"]) == 1


def test_cli_writes_json_report(tmp_path):
    days = _trading_days(date(2025, 1, 2), 30)
    _write_csv(tmp_path / "OK.csv", _rows(days))
    out = tmp_path / "report.json"
    main(["--csv", str(tmp_path), "validate-data", "--as-of", days[-1].isoformat(),
          "--min-bars", "10", "--output", str(out)])
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["passed"] is True and blob["symbols"][0]["symbol"] == "OK"


def test_cli_show_limit_counts_remaining_rows_not_occurrences(tmp_path, capsys):
    """묶인 항목의 발생 횟수가 아니라 '아직 못 보여준 줄 수' 를 보고해야 한다."""
    days = _trading_days(date(2025, 1, 2), 30)
    _write_csv(tmp_path / "A.csv", _rows(days))
    _write_csv(tmp_path / "B.csv", _rows(days))
    # 기준일을 앞당겨 종목마다 future_bar 가 수십 건씩 묶이게 만든다.
    main(["--csv", str(tmp_path), "validate-data", "--as-of", days[0].isoformat(),
          "--min-bars", "10", "--show", "1"])
    out = capsys.readouterr().out
    # 전체 줄은 종목당 1줄씩 2줄, 그중 1줄을 보여줬으므로 남은 것은 1줄이다.
    assert "그 외 1건" in out
