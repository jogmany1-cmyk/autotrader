"""전략은 `[0..at]` 만 본다 — 규칙 1번의 실행 가능한 게이트.

어제 속도 최적화 중에 `closes` 의 의미를 접두 구간에서 전체 시리즈로 바꿨고,
`swing_trend` 가 `len(closes)` 와 `closes[-1]` 로 미래 봉을 읽게 됐다.
그때 224개 테스트가 전부 통과했다 — 아무도 확인하지 않았기 때문이다
(PITFALLS #24).

여기서 쓰는 방법은 전략 내부를 들여다보지 않는다. **같은 `at` 에 대해,
뒤에 미래 봉이 붙어 있든 없든 판단이 같아야 한다.** 미래를 읽으면 달라진다.
"""
import random
from datetime import datetime, timedelta

import pytest

from autotrader.backtest import Backtester
from autotrader.config import Config
from autotrader.data.synthetic import SyntheticProvider
from autotrader.models import Bar
from autotrader.strategy.base import StrategyContext


def _series(n=420, seed=7):
    rng = random.Random(seed)
    px, out = 50_000.0, []
    for i in range(n):
        px *= 1 + rng.gauss(0.0006, 0.022)
        hi = px * (1 + abs(rng.gauss(0, 0.012)))
        lo = px * (1 - abs(rng.gauss(0, 0.012)))
        out.append(Bar(datetime(2020, 1, 1) + timedelta(days=i),
                       px, max(hi, px), min(lo, px), px,
                       rng.randint(200_000, 900_000)))
    return out


def _strategies():
    return Backtester(SyntheticProvider(), Config())._default_strategies()


def _decide(strategy, bars, at):
    ctx = StrategyContext(symbol="TEST", bars=bars, at=at, cache={})
    res = strategy.evaluate(ctx)
    sig = res.signal
    return (None if sig is None else (sig.side, round(sig.strength, 10)),
            None if res.stop_hint is None else round(res.stop_hint, 6),
            None if res.target_hint is None else round(res.target_hint, 6))


@pytest.mark.parametrize("strategy", _strategies(),
                         ids=lambda s: type(s).__name__)
def test_future_bars_do_not_change_the_decision(strategy):
    full = _series()
    checked = 0
    for at in range(260, len(full) - 40, 17):
        truncated = full[: at + 1]          # 미래가 아예 없는 세계
        assert _decide(strategy, truncated, at) == _decide(strategy, full, at), (
            f"{type(strategy).__name__}: at={at} 에서 뒤에 붙은 미래 봉이 "
            f"판단을 바꿨다 — [0..at] 밖을 읽고 있다")
        checked += 1
    assert checked >= 5


@pytest.mark.parametrize("strategy", _strategies(),
                         ids=lambda s: type(s).__name__)
def test_rewriting_the_future_does_not_change_the_decision(strategy):
    """잘라내는 대신 미래를 **다른 값으로 덮어써** 본다.

    잘라내기만 하면 `bars[-1]` 같은 참조가 우연히 맞아떨어지는 구간이 생긴다.
    미래를 폭등으로 바꿔도 판단이 같아야 진짜로 안 읽는 것이다.
    """
    base = _series()
    for at in range(260, len(base) - 40, 23):
        tampered = list(base)
        for j in range(at + 1, len(tampered)):
            b = tampered[j]
            tampered[j] = Bar(b.ts, b.open * 5, b.high * 5, b.low * 5,
                              b.close * 5, b.volume * 3)
        assert _decide(strategy, base, at) == _decide(strategy, tampered, at), (
            f"{type(strategy).__name__}: at={at} 이후를 5배로 바꿨더니 판단이 "
            f"달라졌다 — 미래 정보가 새고 있다")
