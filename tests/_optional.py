"""선택적 의존성이 없는 환경에서 벤더 어댑터 테스트를 건너뛰기 위한 표식.

키움·KIS 어댑터는 `requests` 를 필요로 한다. 이 저장소에서 `requests` 는
**선택적** 의존성이라 (`pyproject.toml` 의 `live` extra) 맨 환경에서는 깔려
있지 않다. 그런데 지금까지 테스트가 그 사실을 모른 채 어댑터를 생성해서,
requests 가 없는 환경에서는 `pytest -q` 가 10건 실패했다.

README 와 CLAUDE.md 는 `pip install pytest` 만으로 스위트가 돈다고 말한다.
그 약속을 지키려면 어댑터 테스트는 requests 가 있을 때만 돌아야 한다.

주의: "코어가 requests 없이 동작한다" 는 제약은 이 표식으로 검사되지 않는다.
그건 scripts/check_stdlib_only.py 가 임포트를 차단한 채로 따로 강제한다.
여기서 건너뛰는 것은 어댑터 전용 테스트뿐이다.
"""
import importlib.util

import pytest


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


HAS_REQUESTS = _installed("requests")

requires_requests = pytest.mark.skipif(
    not HAS_REQUESTS,
    reason="requests 미설치 — 벤더 어댑터 테스트 건너뜀 (pip install requests)")
