"""PaperBroker 계좌 상태의 영속화 — 프로세스가 바뀌어도 살아남게 한다.

`recovery.SessionState` 는 "브로커에 물어볼 수 없는 것만 담는다" 는 계약을
갖고 있다. 실브로커에서는 옳다 — 현금·수량·평균단가는 증권사가 진실이고,
그걸 우리도 저장하면 두 진실이 생겨 어긋났을 때 어느 쪽이 맞는지 알 수 없다.

그런데 `PaperBroker` 는 그 브로커가 **우리 프로세스 안에** 있다. 프로세스가
끝나면 진실도 같이 사라진다. 크론이 매일 새 프로세스로 부르면 이렇게 된다:

    1일차  매수 10주, 현금 9,298,895
    2일차  reconcile_positions → "기록에 있으나 브로커에 없음 — 정리된 것으로
           본다" 로 포지션이 조용히 버려지고, 현금은 initial_cash 로 복귀

즉 60거래일을 돌려도 **매일 1일차**다. 누적이 일어나지 않으므로 모의투자
기간을 아무리 늘려도 측정되는 것이 없다. 이 모듈이 그 진실을 파일에 둔다.

실브로커에는 쓰지 않는다. 쓰면 위의 "두 진실" 문제가 그대로 생긴다.

직렬화 규약: datetime 은 `{"$dt": "ISO8601"}` 로 감싼다. 예약 키가 `$` 로
시작하므로 종목코드·팩터명 같은 실제 키와 충돌하지 않는다.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ..models import Fill, Position, Side, Trade

#: 파일 포맷 버전. 필드가 바뀌어 옛 파일을 못 읽게 되면 올린다.
SCHEMA = 1


class PaperAccountError(Exception):
    """계좌 파일이 있는데 쓸 수 없는 상태 — 조용히 넘기면 안 되는 것."""


# ------------------------------------------------------------ 직렬화
def _enc(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, Side):
        return value.value
    if isinstance(value, dict):
        return {k: _enc(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enc(v) for v in value]
    if is_dataclass(value):
        return {f.name: _enc(getattr(value, f.name)) for f in fields(value)}
    return value


def _dec(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$dt"}:
            return datetime.fromisoformat(value["$dt"])
        return {k: _dec(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dec(v) for v in value]
    return value


def _build(cls, row: Dict[str, Any]):
    """알려진 필드만 골라 dataclass 를 만든다.

    모르는 키는 버린다 — 옛 파일에 지금은 없는 필드가 남아 있어도 읽혀야
    한다. 반대로 새 필드는 dataclass 기본값이 채운다.
    """
    known = {f.name for f in fields(cls)}
    kwargs = {k: _dec(v) for k, v in row.items() if k in known}
    if cls is Fill and "side" in kwargs:
        kwargs["side"] = Side(kwargs["side"])
    return cls(**kwargs)


# ------------------------------------------------------------ 스냅샷
def snapshot(broker) -> Dict[str, Any]:
    """PaperBroker 의 계좌 전체를 JSON 가능한 dict 로."""
    p = broker.portfolio
    return {
        "schema": SCHEMA,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cash": p.cash,
        "positions": {sym: _enc(pos) for sym, pos in p.positions.items()},
        "closed_trades": [_enc(t) for t in p.closed_trades],
        "fills": [_enc(f) for f in broker.fills],
    }


def restore(broker, row: Dict[str, Any]) -> None:
    """스냅샷을 브로커에 되살린다. 기존 내용은 덮어쓴다."""
    got = int(row.get("schema", 0))
    if got != SCHEMA:
        raise PaperAccountError(
            f"계좌 파일 스키마 {got} 를 읽을 수 없습니다 (이 버전은 {SCHEMA}). "
            f"파일을 옮기고 새 세션으로 시작하거나, 옛 버전으로 마저 돌리세요.")
    p = broker.portfolio
    p.cash = float(row["cash"])
    p.positions = {sym: _build(Position, r)
                   for sym, r in (row.get("positions") or {}).items()}
    p.closed_trades = [_build(Trade, r) for r in (row.get("closed_trades") or [])]
    broker.fills = [_build(Fill, r) for r in (row.get("fills") or [])]


# ------------------------------------------------------------ 파일 I/O
def save(broker, path: str) -> None:
    """원자적으로 쓴다 — 쓰다가 죽으면 반쯤 쓰인 계좌보다 옛 계좌가 낫다."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(snapshot(broker), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(broker, path: str) -> bool:
    """계좌를 복원했으면 True, 파일이 없어서 새 세션이면 False.

    파일이 **있는데** 깨졌으면 예외를 던진다. SessionState 는 같은 상황에서
    빈 상태로 넘어가는데, 그건 브로커에 진짜 잔고가 남아 있어서 잃는 것이
    손절선뿐이기 때문이다. 여기서는 그 파일이 잔고 자체라서, 조용히 넘어가면
    60일 쌓은 결과가 통째로 사라지고 아무도 눈치채지 못한다.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
    except (ValueError, OSError) as exc:
        raise PaperAccountError(f"계좌 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    restore(broker, row)
    return True


def summary(path: str) -> Optional[str]:
    """사람이 읽을 한 줄. 파일이 없으면 None."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        row = json.load(fh)
    return (f"계좌 복원: 현금 {row.get('cash', 0):,.0f}원 · "
            f"보유 {len(row.get('positions') or {})}종목 · "
            f"청산 {len(row.get('closed_trades') or [])}건 "
            f"(저장 {row.get('saved_at', '?')})")
