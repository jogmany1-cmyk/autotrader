"""시스템 전체가 하나의 시계(한국시간)를 쓰는지 고정한다.

배경: `market.py` 의 장 시간 판정은 한국시간 09:00~15:30 기준인데, 호출자들은
`datetime.utcnow()` 로 UTC 를 넘기고 있었다. 저장소 전체에 시간대 변환이 한 줄도
없었다. 그 결과 장중(한국 09:30)에는 "휴장" 으로 판정해 거래를 안 하고,
한국시간 저녁 18:30(UTC 09:30)에 "장이 열렸다" 고 판단한다.

여기서 고정하는 것은 두 가지다.
(a) 코드에 `datetime.utcnow()` 호출이 하나도 남지 않았는지 (실행 가능한 게이트)
(b) 시각을 생략했을 때 실제로 한국시간을 쓰는지 (동작)
"""
import ast
import pathlib
from datetime import datetime

import pytest

from autotrader.config import Config, Costs
from autotrader.broker import PaperBroker
from autotrader.data import SyntheticProvider
from autotrader.live import LiveTrader

PKG = pathlib.Path(__file__).resolve().parents[1] / "autotrader"


def _utcnow_calls(path: pathlib.Path):
    """AST 로 실제 호출만 찾는다 — 주석·독스트링의 언급은 세지 않는다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "utcnow"):
            found.append(node.lineno)
    return found


def test_no_utcnow_call_remains_in_runtime_code():
    """한 곳이라도 UTC 로 되돌아가면 시계가 둘로 갈라진다."""
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        lines = _utcnow_calls(path)
        if lines:
            offenders[str(path.relative_to(PKG.parent))] = lines
    assert not offenders, (
        "datetime.utcnow() 가 남아 있습니다. market.now_kst() 를 쓰세요.\n"
        f"{offenders}")


def _trader():
    provider = SyntheticProvider(symbols=("AAA", "BBB"), n=260)
    cfg = Config.default()
    cfg.universe.symbols = provider.universe()
    cfg.universe.min_price = 0
    cfg.universe.min_avg_dollar_vol = 0
    return LiveTrader(provider, PaperBroker(1e7, Costs()), cfg,
                      ensemble_threshold=0.99, dry_run=True)


def test_cycle_without_now_uses_korean_clock(monkeypatch):
    """시각을 생략하면 한국시간을 써야 한다 — 장중이면 열린 것으로 판정."""
    kst_open = datetime(2026, 8, 24, 9, 30)      # 월요일 장중 (한국시간)
    monkeypatch.setattr("autotrader.live.now_kst", lambda: kst_open)
    assert _trader().cycle().market_open is True


def test_evening_is_not_treated_as_regular_session(monkeypatch):
    """UTC 를 쓰던 시절 정규장으로 오판하던 시각.

    한국시간 18:30 은 UTC 로 09:30 이다. 예전에는 그 UTC 값이 그대로 장 시간
    판정에 들어가 "정규장" 으로 잡혔다. 실제로는 NXT 애프터 세션이고,
    기본 설정(allow_after_market=False)에서는 참여하지 않는다.
    """
    monkeypatch.setattr("autotrader.live.now_kst",
                        lambda: datetime(2026, 8, 24, 18, 30))
    rep = _trader().cycle()
    assert rep.market_open is False
    assert "after" in (rep.skipped_reason or ""), "정규장으로 잡히면 안 된다"


def test_late_night_is_fully_closed(monkeypatch):
    monkeypatch.setattr("autotrader.live.now_kst",
                        lambda: datetime(2026, 8, 24, 22, 0))
    rep = _trader().cycle()
    assert rep.market_open is False
    assert "closed" in (rep.skipped_reason or "")


def test_jobs_use_the_same_clock(monkeypatch):
    """jobs 도 같은 시계를 써야 한다 — 크론이 09:30 KST 에 부르기 때문이다."""
    import autotrader.jobs as jobs
    assert hasattr(jobs, "now_kst"), "jobs.py 가 now_kst 를 임포트해야 한다"


def test_scheduler_uses_the_same_clock():
    """크론 표현식('30 9 * * 0-4')은 한국시간 09:30 을 뜻한다."""
    import autotrader.scheduler as sched
    assert hasattr(sched, "now_kst"), "scheduler.py 가 now_kst 를 임포트해야 한다"
