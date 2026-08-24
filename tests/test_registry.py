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
        oos_trades=60,          # 기준이 50 으로 올랐다 (거래 20건대 PF 는 잡음)
        oos_max_drawdown=-0.10,
        oos_net_return=0.08,    # 비용 차감 후 실제로 벌었는지
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


# ---- 강화된 기준 (실측에서 배운 것) -----------------------------------

def test_twenty_trades_is_no_longer_enough():
    """옛 기준(거래≥20)은 잡음을 신호로 오인한다.

    같은 전략·같은 데이터로 종목 수만 늘려가며 잰 OOS Profit Factor:
        거래 4건 → 20.93 / 10건 → 3.25 / 29건 → 0.84 / 40건 → 0.59
    표본 20건대의 PF 는 신호가 아니다.
    """
    reg = StrategyRegistry()
    reg.upsert(_rec("small_sample", oos_trades=22))
    assert not reg.is_validated("small_sample")


def test_losing_strategy_is_never_approved():
    """PF 가 기준을 넘어도 비용 차감 후 손실이면 승인하지 않는다."""
    reg = StrategyRegistry()
    reg.upsert(_rec("lossy", oos_profit_factor=1.84, oos_net_return=-0.015))
    assert not reg.is_validated("lossy")


def test_zero_net_return_is_not_approved():
    """본전은 통과가 아니다 — 비용과 위험을 감수할 이유가 없다."""
    reg = StrategyRegistry()
    reg.upsert(_rec("flat", oos_net_return=0.0))
    assert not reg.is_validated("flat")


def test_unmeasured_net_return_is_not_approved():
    """모르는 것을 통과시키는 것이 가장 위험하다.

    oos_net_return 이 없는 옛 레코드는 자동으로 재검증 대상이 된다.
    """
    reg = StrategyRegistry()
    reg.upsert(_rec("legacy", oos_net_return=None))
    assert not reg.is_validated("legacy")


def test_old_json_without_net_return_loads_and_fails_closed():
    """예전 형식 파일을 읽어도 깨지지 않고, 대신 승인되지 않는다."""
    old = {
        "name": "legacy", "validated_at": dt.datetime.utcnow().isoformat(),
        "oos_profit_factor": 1.5, "oos_trades": 60, "oos_max_drawdown": -0.1,
    }
    rec = StrategyRecord.from_dict(old)
    assert rec.oos_net_return is None
    assert not rec.is_valid(ValidationThresholds())


def test_the_measured_failing_run_would_be_rejected():
    """실제 20종목 백테스트 결과(PF 0.59, 거래 40건, 수익 -1.47%)는 탈락한다."""
    reg = StrategyRegistry()
    reg.upsert(_rec("measured", oos_profit_factor=0.59, oos_trades=40,
                    oos_net_return=-0.0147))
    assert not reg.is_validated("measured")
