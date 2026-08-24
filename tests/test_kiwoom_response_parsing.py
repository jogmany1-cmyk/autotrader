"""키움 응답 파싱 회귀 테스트.

배경: 사용자의 첫 실제 키움 호출이 이렇게 끝났다.

    [DONE ] ok=0 fail=1
    [FAIL] 005930: DataError: 005930: 시세 없음

토큰 발급도 HTTP 200 응답도 성공했는데 봉이 0건이었다. 원인은 어댑터가
봉 배열의 키 이름(`stk_dt_pole_chart_qry`)을 코드에 못박아 두고 그 키가
없으면 조용히 빈 리스트를 쓴 것. 벤더 문서와 실제 응답이 다르면 "데이터가
없다" 로 둔갑한다.

여기서는 (a) 키 이름이 달라도 배열을 찾아내는지, (b) HTTP 200 안에 담긴
벤더 오류 코드를 잡아내는지, (c) 부호 붙은 가격·별칭 필드를 읽는지를 고정한다.
"""
from datetime import datetime

import pytest

from autotrader.data.base import DataError
from tests._optional import requires_requests


class _Resp:
    """requests.Response 를 흉내내는 최소 객체."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture
def prov(tmp_path):
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider
    return KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                          cache_dir=str(tmp_path))


ROW = {"dt": "20260821", "open_pric": "+73000", "high_pric": "+74000",
       "low_pric": "-72500", "cur_prc": "+73400", "trde_qty": "12345678"}


# ---- 봉 배열 찾기 -------------------------------------------------------

@requires_requests
def test_rows_found_under_documented_key(prov):
    assert prov._rows({"stk_dt_pole_chart_qry": [ROW]}) == [ROW]


@requires_requests
def test_rows_found_even_when_key_name_is_unknown(prov):
    """이게 실제 실패의 핵심 — 문서에 없는 키로 와도 찾아내야 한다."""
    assert prov._rows({"return_code": 0, "some_new_name_v2": [ROW]}) == [ROW]


@requires_requests
def test_rows_ignores_non_dict_lists(prov):
    """문자열 리스트 같은 건 봉 배열이 아니다."""
    assert prov._rows({"codes": ["005930", "000660"]}) == []


@requires_requests
def test_rows_empty_when_no_array(prov):
    assert prov._rows({"return_code": 0, "msg": "no data"}) == []


# ---- 한 행 → Bar -------------------------------------------------------

@requires_requests
def test_signed_prices_are_read_as_positive(prov):
    """키움은 '+73400' / '-72500' 처럼 부호를 붙여 보낸다."""
    bar = prov._bar_from_row(ROW)
    assert bar.ts == datetime(2026, 8, 21)
    assert (bar.open, bar.high, bar.low, bar.close) == (73000.0, 74000.0, 72500.0, 73400.0)
    assert bar.volume == 12345678.0


@requires_requests
def test_alias_field_names_are_accepted(prov):
    """벤더가 필드명을 바꿔도 별칭으로 읽어야 한다."""
    bar = prov._bar_from_row({"stck_bsop_date": "20260821", "stck_oprc": "73000",
                              "stck_hgpr": "74000", "stck_lwpr": "72500",
                              "stck_clpr": "73400", "acml_vol": "100"})
    assert bar.close == 73400.0 and bar.ts == datetime(2026, 8, 21)


@requires_requests
def test_minute_timestamp_format(prov):
    bar = prov._bar_from_row({"cntr_tm": "20260821093000", "cur_prc": "73400"})
    assert bar.ts == datetime(2026, 8, 21, 9, 30)


@requires_requests
def test_missing_ohlc_falls_back_to_close(prov):
    bar = prov._bar_from_row({"dt": "20260821", "cur_prc": "73400"})
    assert bar.open == bar.high == bar.low == bar.close == 73400.0


@requires_requests
def test_unparseable_date_is_rejected(prov):
    with pytest.raises(ValueError):
        prov._bar_from_row({"dt": "not-a-date", "cur_prc": "1"})


# ---- HTTP 200 안의 벤더 오류 -------------------------------------------

@requires_requests
def test_business_error_inside_http_200_raises(prov):
    """200 이어도 본문에 오류 코드가 있으면 '데이터 없음' 이 아니라 오류다."""
    with pytest.raises(DataError) as e:
        prov._inspect(_Resp({"return_code": 3, "return_msg": "유효하지 않은 종목코드"}),
                      "005930", "ka10081")
    assert "return_code=3" in str(e.value)
    assert "유효하지 않은 종목코드" in str(e.value)


@requires_requests
def test_return_code_zero_passes(prov):
    js = prov._inspect(_Resp({"return_code": 0, "list": [ROW]}), "005930", "ka10081")
    assert js["list"] == [ROW]


@requires_requests
def test_diagnostics_recorded_for_error_message(prov):
    prov._inspect(_Resp({"return_code": 0, "weird_key": [ROW]}), "005930", "ka10081")
    diag = prov._diag()
    assert "HTTP 200" in diag and "weird_key" in diag


# ---- 필수 파라미터 -------------------------------------------------------

@requires_requests
def test_daily_request_sends_non_empty_base_dt(prov, monkeypatch):
    """base_dt 를 빈 문자열로 보내면 키움이 거절한다.

        return_code=2: 입력 값 오류입니다
        [1511:필수 입력 값에 값이 존재하지 않습니다. 필수입력 파라미터=base_dt]

    빈 값이 "오늘" 로 해석될 거라 추측했던 것이 틀렸다. 실제 날짜를 넣는다.
    """
    import json as _json

    captured = {}

    class _Session:
        def post(self, url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["body"] = _json.loads(data)
            return _Resp({"return_code": 0, "stk_dt_pole_chart_qry": [ROW]})

    monkeypatch.setattr(prov, "_http", lambda: _Session())
    monkeypatch.setattr(prov, "_headers", lambda *a, **k: {})
    prov._fetch_daily("005930")

    base_dt = captured["body"]["base_dt"]
    assert base_dt, "base_dt 가 비어 있으면 키움이 요청을 거절한다"
    assert len(base_dt) == 8 and base_dt.isdigit(), f"YYYYMMDD 형식이어야 함: {base_dt!r}"


@requires_requests
def test_base_dt_uses_korean_date_not_utc(prov, monkeypatch):
    """UTC 를 쓰면 한국시간 오전 9시 이전에 하루 전 날짜가 들어간다."""
    import json as _json
    from datetime import datetime, timezone

    from autotrader.market import now_kst

    captured = {}

    class _Session:
        def post(self, url, headers=None, data=None, timeout=None):
            captured["body"] = _json.loads(data)
            return _Resp({"return_code": 0, "stk_dt_pole_chart_qry": [ROW]})

    monkeypatch.setattr(prov, "_http", lambda: _Session())
    monkeypatch.setattr(prov, "_headers", lambda *a, **k: {})
    prov._fetch_daily("005930")

    assert captured["body"]["base_dt"] == now_kst().strftime("%Y%m%d")
