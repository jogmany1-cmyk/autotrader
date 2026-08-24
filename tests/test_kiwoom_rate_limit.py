"""유량 제한(429) 대응 회귀 테스트.

배경: 사용자의 실제 수집이 이렇게 죽었다.

    HTTP 429 {"return_msg":"허용된 API 요청 개수를 초과하였습니다
              [1700: ... 유량=1, API ID=ka10081]","return_code":5}

원인은 두 가지였다. (a) 요청 사이에 대기가 전혀 없었고 — 연속조회는 한 종목에
최대 30페이지를 연달아 던진다 — (b) 429 를 재시도하지 않고 그대로 실패시켰다.
"""
import pytest

from autotrader.data.base import DataError
from tests._optional import requires_requests


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClock:
    """time.monotonic / time.sleep 을 대신해 실제로 기다리지 않게 한다."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, sec):
        self.slept.append(sec)
        self.t += sec


@pytest.fixture
def prov(tmp_path, monkeypatch):
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider

    p = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                       cache_dir=str(tmp_path))
    clock = _FakeClock()
    monkeypatch.setattr("autotrader.data.kiwoom.time.monotonic", clock.monotonic)
    monkeypatch.setattr("autotrader.data.kiwoom.time.sleep", clock.sleep)
    p._clock = clock
    return p


def _session(responses):
    """정해진 응답을 순서대로 돌려주는 가짜 세션."""
    seq = list(responses)

    class _S:
        calls = 0

        def post(self, url, headers=None, data=None, timeout=None):
            _S.calls += 1
            return seq.pop(0) if seq else _Resp(200, {"return_code": 0})

    return _S()


@requires_requests
def test_429_is_retried_then_succeeds(prov, monkeypatch):
    ok = _Resp(200, {"return_code": 0, "list": []})
    sess = _session([_Resp(429, {"return_code": 5}), ok])
    monkeypatch.setattr(prov, "_http", lambda: sess)

    r = prov._post("/api/dostk/chart", {}, headers={})
    assert r is ok
    assert prov._clock.slept, "429 뒤에는 물러섰다가 다시 시도해야 한다"


@requires_requests
def test_persistent_429_raises_actionable_error(prov, monkeypatch):
    sess = _session([_Resp(429, {"return_code": 5})] * 10)
    monkeypatch.setattr(prov, "_http", lambda: sess)

    with pytest.raises(DataError) as e:
        prov._post("/api/dostk/chart", {}, headers={})
    msg = str(e.value)
    assert "한도 초과" in msg
    assert "--min-interval" in msg, "무엇을 조정하면 되는지 알려줘야 한다"


@requires_requests
def test_requests_are_spaced_by_min_interval(prov, monkeypatch):
    """대기 없이 몰아치면 첫 페이지부터 429 를 맞는다."""
    monkeypatch.setattr(prov, "_http",
                        lambda: _session([_Resp(200, {"return_code": 0})] * 5))
    prov.min_interval = 1.1
    for _ in range(3):
        prov._post("/api/dostk/chart", {}, headers={})

    # 첫 요청은 즉시, 이후 두 번은 간격만큼 기다려야 한다.
    assert len(prov._clock.slept) == 2
    assert all(abs(s - 1.1) < 1e-6 for s in prov._clock.slept)


@requires_requests
def test_retry_after_header_is_respected(prov, monkeypatch):
    sess = _session([_Resp(429, {}, {"Retry-After": "7"}),
                     _Resp(200, {"return_code": 0})])
    monkeypatch.setattr(prov, "_http", lambda: sess)
    prov._post("/api/dostk/chart", {}, headers={})
    assert 7.0 in prov._clock.slept, "서버가 알려준 대기 시간을 따라야 한다"


@requires_requests
def test_min_interval_zero_disables_throttle(prov, monkeypatch):
    monkeypatch.setattr(prov, "_http",
                        lambda: _session([_Resp(200, {"return_code": 0})] * 3))
    prov.min_interval = 0
    for _ in range(3):
        prov._post("/api/dostk/chart", {}, headers={})
    assert prov._clock.slept == []
