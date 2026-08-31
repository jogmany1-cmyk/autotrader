"""페이퍼 계좌가 프로세스 경계를 넘어 살아남는지.

배경: 크론이 매일 새 파이썬 프로세스로 모의매매를 부른다. PaperBroker 의
포트폴리오는 프로세스 안에 있으므로, 저장하지 않으면 매일 1일차가 된다 —
포지션은 `reconcile_positions` 가 "브로커에 없음 → 정리된 것" 으로 버리고
현금은 initial_cash 로 되돌아간다. 60거래일을 돌려도 측정되는 것이 없다.

이 파일은 그 누적을 고정한다. 특히 마지막 테스트는 **진짜 별도 프로세스**를
띄운다 — 같은 인터프리터 안에서 객체를 재사용하면 이 버그는 재현되지 않는다.
"""
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime

import pytest

from autotrader.broker.paper import PaperBroker
from autotrader.broker.paper_state import PaperAccountError, load, save
from autotrader.config import Config
from autotrader.models import Order, Side

INIT = 10_000_000.0


def _broker():
    return PaperBroker(INIT, Config.default().costs)


def _buy(b, sym="005930", qty=10, price=70_000.0, day=24):
    b.submit(Order(symbol=sym, side=Side.BUY, qty=qty), price_hint=price,
             ts=datetime(2026, 8, day, 10, 0), stop=price * 0.9)


def test_cash_and_positions_survive_a_new_broker(tmp_path):
    path = str(tmp_path / "account.json")
    b1 = _broker()
    _buy(b1)
    cash_after, qty_after = b1.cash(), b1.positions()["005930"].qty
    save(b1, path)

    b2 = _broker()                       # 크론이 다음 날 만드는 새 브로커
    assert load(b2, path) is True
    assert b2.cash() == pytest.approx(cash_after)
    assert b2.positions()["005930"].qty == qty_after


def test_stop_price_survives(tmp_path):
    """손절선이 사라지면 그 포지션에는 스탑이 영원히 안 걸린다."""
    path = str(tmp_path / "account.json")
    b1 = _broker()
    _buy(b1, price=70_000.0)
    stop = b1.positions()["005930"].stop_price
    assert stop is not None
    save(b1, path)

    b2 = _broker()
    load(b2, path)
    assert b2.positions()["005930"].stop_price == pytest.approx(stop)


def test_opened_at_datetime_round_trips(tmp_path):
    path = str(tmp_path / "account.json")
    b1 = _broker()
    _buy(b1, day=24)
    opened = b1.positions()["005930"].opened_at
    save(b1, path)
    b2 = _broker()
    load(b2, path)
    got = b2.positions()["005930"].opened_at
    assert isinstance(got, datetime) and got == opened


def test_closed_trades_accumulate_across_sessions(tmp_path):
    """60일 결과를 낼 때 필요한 것은 청산 기록이다. 이게 안 쌓이면
    기간을 늘려도 판정할 근거가 없다."""
    path = str(tmp_path / "account.json")
    for day in (24, 25, 26):
        b = _broker()
        load(b, path)
        _buy(b, day=day)
        b.flat_all({"005930": 71_000.0}, datetime(2026, 8, day, 15, 0))
        save(b, path)

    final = _broker()
    load(final, path)
    assert len(final.portfolio.closed_trades) == 3, "세션마다 덮어써지고 있다"
    assert len(final.fills) == 6                      # 매수3 + 매도3


def test_realised_pnl_compounds_not_resets(tmp_path):
    """손실이 다음 날로 이어지는지. 리셋되면 백테스트가 손실을 지운다."""
    path = str(tmp_path / "account.json")
    b1 = _broker()
    _buy(b1, price=70_000.0, day=24)
    b1.flat_all({"005930": 63_000.0}, datetime(2026, 8, 24, 15, 0))   # 손실 확정
    after_loss = b1.cash()
    assert after_loss < INIT
    save(b1, path)

    b2 = _broker()
    load(b2, path)
    assert b2.cash() == pytest.approx(after_loss), "손실이 지워졌다"


def test_missing_file_is_a_fresh_session(tmp_path):
    b = _broker()
    assert load(b, str(tmp_path / "nope.json")) is False
    assert b.cash() == INIT


def test_corrupt_file_raises_instead_of_silently_resetting(tmp_path):
    """SessionState 는 깨진 파일을 조용히 넘긴다 — 브로커에 진짜 잔고가
    남아 있어서 잃는 게 손절선뿐이기 때문이다. 여기서는 이 파일이 잔고
    자체라서, 조용히 넘어가면 60일 결과가 통째로 사라진다."""
    path = tmp_path / "account.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(PaperAccountError):
        load(_broker(), str(path))


def test_unknown_schema_refuses_to_load(tmp_path):
    path = tmp_path / "account.json"
    path.write_text(json.dumps({"schema": 999, "cash": 1}), encoding="utf-8")
    with pytest.raises(PaperAccountError):
        load(_broker(), str(path))


def test_unknown_extra_field_is_tolerated(tmp_path):
    """옛 파일에 지금은 없는 필드가 남아 있어도 읽혀야 한다."""
    path = tmp_path / "account.json"
    b = _broker()
    _buy(b)
    save(b, str(path))
    row = json.loads(path.read_text(encoding="utf-8"))
    row["positions"]["005930"]["gone_field"] = 1
    path.write_text(json.dumps(row), encoding="utf-8")
    b2 = _broker()
    load(b2, str(path))
    assert b2.positions()["005930"].qty == 10


def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "account.json")
    b = _broker()
    _buy(b)
    save(b, path)
    assert not os.path.exists(path + ".tmp")


def test_accumulates_across_real_separate_processes(tmp_path):
    """같은 인터프리터 안에서는 이 버그가 재현되지 않는다. 크론이 하는 것과
    똑같이 프로세스를 매번 새로 띄운다."""
    path = str(tmp_path / "account.json")
    script = textwrap.dedent("""
        import sys
        from datetime import datetime
        from autotrader.broker.paper import PaperBroker
        from autotrader.broker.paper_state import load, save
        from autotrader.config import Config
        from autotrader.models import Order, Side

        path, day = sys.argv[1], int(sys.argv[2])
        b = PaperBroker(10_000_000.0, Config.default().costs)
        load(b, path)
        b.submit(Order(symbol="005930", side=Side.BUY, qty=10),
                 price_hint=70_000.0, ts=datetime(2026, 8, day, 10, 0))
        save(b, path)
        print(f"{b.cash():.0f} {b.positions()['005930'].qty}")
    """)
    script_path = tmp_path / "one_day.py"
    script_path.write_text(script, encoding="utf-8")

    # 스크립트를 파일로 실행하면 sys.path[0] 이 **스크립트 폴더**(tmp)라
    # 저장소가 안 잡힌다. PYTHONPATH 로 명시한다.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    seen = []
    for day in (24, 25, 26):
        proc = subprocess.run([sys.executable, str(script_path), path, str(day)],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, cwd=repo_root, env=env)
        assert proc.returncode == 0, proc.stderr
        cash, qty = proc.stdout.split()
        seen.append((float(cash), int(qty)))

    cashes = [c for c, _ in seen]
    qtys = [q for _, q in seen]
    assert qtys == [10, 20, 30], f"수량이 안 쌓인다: {qtys}"
    assert cashes[0] > cashes[1] > cashes[2], f"현금이 리셋되고 있다: {cashes}"


# --------------------------------------------------------------- LiveTrader 배선
# 위 테스트들은 paper_state 모듈 자체를 고정한다. 아래는 그것이 LiveTrader 의
# recover() / save_state() 에 실제로 연결되어 있는지 — 특히 **순서**가 맞는지를
# 본다. 계좌 복원이 reconcile_positions 보다 뒤에 오면, reconcile 이 빈 브로커를
# 보고 모든 포지션을 "정리된 것" 으로 버린다. 그 버그가 이 작업의 출발점이었다.

def _trader(broker, account_path, state_path=None):
    from autotrader.config import Config
    from autotrader.data.synthetic import SyntheticProvider
    from autotrader.live import LiveTrader
    cfg = Config.default()
    return LiveTrader(SyntheticProvider(), broker, cfg,
                      account_path=account_path, state_path=state_path)


def test_recover_restores_positions_instead_of_discarding_them(tmp_path):
    path = str(tmp_path / "account.json")
    b1 = _broker()
    _buy(b1)
    t1 = _trader(b1, path)
    t1.save_state()

    b2 = _broker()
    t2 = _trader(b2, path)
    notes = t2.recover()

    assert "005930" in b2.positions(), (
        "계좌 복원이 reconcile 보다 늦게 일어나 포지션이 버려졌다. notes=%r" % notes)
    assert b2.positions()["005930"].qty == 10
    assert b2.cash() == pytest.approx(b1.cash())
    assert not any("정리된 것으로 본다" in n for n in notes), (
        "복원했는데도 '정리됨' 경고가 났다 — 순서가 뒤집혔다")


def test_save_state_persists_account_even_without_state_path(tmp_path):
    """--account 만 주고 --state 를 안 준 경우. save_state() 의 조기 반환이
    계좌 저장까지 건너뛰면 안 된다."""
    path = str(tmp_path / "account.json")
    b = _broker()
    _buy(b)
    _trader(b, path, state_path=None).save_state()
    assert os.path.exists(path)
    b2 = _broker()
    load(b2, path)
    assert b2.positions()["005930"].qty == 10


def test_no_account_path_means_no_file_written(tmp_path):
    """계좌 경로를 안 주면 아무 파일도 만들지 않는다 — 기존 동작 보존."""
    b = _broker()
    _buy(b)
    _trader(b, account_path=None).save_state()
    assert list(tmp_path.iterdir()) == []
