"""수집 실패가 조용히 개수로만 사라지지 않는지 고정한다.

배경: `refresh_all` 이 `except DataError: fail += 1` 로 예외를 통째로 버려서,
실제 사용자의 첫 키움 접속이 화면에 `ok=0 fail=1` 한 줄만 남기고 끝났다.
무엇이 잘못됐는지 알 수 없으면 고칠 수도 없다.
"""
from autotrader.data.base import DataError
from tests._optional import requires_requests


@requires_requests
def test_failure_reason_is_recorded_not_just_counted(tmp_path):
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider

    prov = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                          cache_dir=str(tmp_path))

    def boom(symbol, limit=500):
        raise DataError("토큰 발급 실패: 401")

    prov.history = boom
    ok, fail = prov.refresh_all(["005930"], limit=10)

    assert (ok, fail) == (0, 1)
    assert prov.last_failures == [("005930", "DataError: 토큰 발급 실패: 401")]


@requires_requests
def test_non_dataerror_does_not_abort_the_whole_universe(tmp_path):
    """한 종목의 네트워크 오류가 나머지 수집을 중단시키면 안 된다."""
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider

    prov = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                          cache_dir=str(tmp_path))

    def flaky(symbol, limit=500):
        if symbol == "BAD":
            raise ConnectionError("연결 끊김")   # DataError 가 아닌 예외
        return []

    prov.history = flaky
    ok, fail = prov.refresh_all(["AAA", "BAD", "CCC"], limit=10)

    assert (ok, fail) == (2, 1)
    assert prov.last_failures == [("BAD", "ConnectionError: 연결 끊김")]


@requires_requests
def test_last_failures_resets_between_runs(tmp_path):
    from autotrader.config import KiwoomConfig
    from autotrader.data import KiwoomProvider

    prov = KiwoomProvider(KiwoomConfig(app_key="x", app_secret="y"),
                          cache_dir=str(tmp_path))
    prov.history = lambda symbol, limit=500: (_ for _ in ()).throw(DataError("나쁨"))
    prov.refresh_all(["AAA"], limit=10)
    assert len(prov.last_failures) == 1

    prov.history = lambda symbol, limit=500: []
    ok, fail = prov.refresh_all(["AAA"], limit=10)
    assert (ok, fail) == (1, 0)
    assert prov.last_failures == []       # 이전 실행의 실패가 남아 있으면 안 된다
