import datetime as dt
import json
import os
import tempfile

from autotrader.registry import (StrategyRecord, StrategyRegistry,
                                 ValidationThresholds)


def _rec(name, **kw):
    defaults = dict(
        validated_at=dt.datetime.utcnow(),
        oos_profit_factor=1.5,
        oos_trades=40,
        oos_max_drawdown=-0.10,
    )
    defaults.update(kw)
    return StrategyRecord(name=name, **defaults)


def test_healthy_record_passes_default_thresholds():
    reg = StrategyRegistry()
    reg.upsert(_rec("day_breakout"))
    assert reg.is_validated("day_breakout")


def test_low_profit_factor_fails():
    reg = StrategyRegistry()
    reg.upsert(_rec("weak", oos_profit_factor=1.05))
    assert not reg.is_validated("weak")


def test_stale_record_expires():
    reg = StrategyRegistry()
    reg.upsert(_rec("old", validated_at=dt.datetime.utcnow() - dt.timedelta(days=180)))
    assert not reg.is_validated("old")


def test_too_few_trades_fails():
    reg = StrategyRegistry()
    reg.upsert(_rec("tiny", oos_trades=5))
    assert not reg.is_validated("tiny")


def test_large_drawdown_fails():
    reg = StrategyRegistry()
    reg.upsert(_rec("wild", oos_max_drawdown=-0.40))
    assert not reg.is_validated("wild")


def test_roundtrip_save_load():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "r.json")
        reg = StrategyRegistry(path)
        reg.upsert(_rec("a"))
        reg.upsert(_rec("b", oos_profit_factor=0.5))  # 실패 케이스도 저장
        reg.save()
        reg2 = StrategyRegistry(path)
        assert reg2.is_validated("a")
        assert not reg2.is_validated("b")
