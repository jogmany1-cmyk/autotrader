"""재시작 복구 — 브로커가 최종 진실의 기준이다.

프로세스가 죽었다 살아나면 우리는 아무것도 모른다. 무엇을 들고 있는지, 낸
주문이 체결됐는지, 오늘 몇 번 진입했는지. 그 상태로 사이클을 돌리면:

  - 이미 들고 있는 종목에 또 들어간다 (already-held 를 모르니까)
  - 일일 진입 상한이 0 부터 다시 세어진다
  - 손절선을 모르니 스탑이 안 걸린다 — 브로커는 우리 스탑 가격을 모른다
  - 미결 주문을 잊고 같은 신호로 또 주문을 낸다

두 종류의 진실이 있고 섞으면 안 된다.

  **브로커만 아는 것** — 보유수량, 평균매입가, 현금, 주문 체결 여부.
  이것들은 우리 로그가 뭐라 하든 브로커가 이긴다. 다른 단말에서 손으로 팔았을
  수도 있고, 우리가 죽어 있는 동안 체결됐을 수도 있다.

  **우리만 아는 것** — 손절가, 목표가, 트레일링 폭, 진입 시각, 최고가 기록.
  브로커는 이것을 모른다. 우리가 기록해 두지 않으면 영원히 사라진다.

그래서 복구는 "덮어쓰기" 가 아니라 **합치기**다. 브로커 잔고를 뼈대로 삼고,
우리 기록에서 브로커가 모르는 값만 채운다. 브로커에 없는 종목은 버린다 —
우리가 모르는 사이에 정리된 포지션이다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from .market import now_kst
from .models import Position


@dataclass
class SessionState:
    """재시작을 건너뛰어야 하는 값들.

    브로커에 물어볼 수 없는 것만 담는다. 브로커가 아는 것을 여기 저장하면
    두 진실이 생기고, 어긋났을 때 어느 쪽이 맞는지 알 수 없게 된다.
    """
    day: Optional[date] = None
    day_start_equity: float = 0.0
    day_realized_pnl: float = 0.0
    day_new_entries: int = 0
    consecutive_losses: int = 0
    cooldown_until: Optional[date] = None
    #: 그날 EOD 일괄청산을 이미 했는지. 재시작 후 또 하면 안 된다.
    flat_done_for: Optional[str] = None
    #: 종목별 쿨다운 만료일
    cooldowns: Dict[str, date] = field(default_factory=dict)
    #: 종목 → 브로커가 모르는 포지션 값들
    position_meta: Dict[str, dict] = field(default_factory=dict)

    # ---- 직렬화 --------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "day_realized_pnl": self.day_realized_pnl,
            "day_new_entries": self.day_new_entries,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": (self.cooldown_until.isoformat()
                               if self.cooldown_until else None),
            "flat_done_for": self.flat_done_for,
            "cooldowns": {k: v.isoformat() for k, v in self.cooldowns.items()},
            "position_meta": self.position_meta,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "SessionState":
        def _d(key):
            raw = row.get(key)
            return date.fromisoformat(raw) if raw else None

        return cls(
            day=_d("day"),
            day_start_equity=float(row.get("day_start_equity", 0.0)),
            day_realized_pnl=float(row.get("day_realized_pnl", 0.0)),
            day_new_entries=int(row.get("day_new_entries", 0)),
            consecutive_losses=int(row.get("consecutive_losses", 0)),
            cooldown_until=_d("cooldown_until"),
            flat_done_for=row.get("flat_done_for"),
            cooldowns={k: date.fromisoformat(v)
                       for k, v in (row.get("cooldowns") or {}).items()},
            position_meta=dict(row.get("position_meta") or {}),
        )

    def save(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 임시 파일에 쓰고 갈아끼운다. 쓰다가 죽으면 예전 상태가 남는 편이
        # 반쯤 쓰인 상태보다 낫다.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(self.as_dict(), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "SessionState":
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (ValueError, KeyError, OSError):
            # 상태 파일이 깨졌다고 매매를 못 하면 안 된다. 빈 상태로 시작하면
            # 브로커 잔고는 그대로 살아 있고, 잃는 것은 손절선 같은 우리
            # 기록뿐이다. 그건 아래 reconcile 이 경고로 드러낸다.
            return cls()


def snapshot_positions(positions: Dict[str, Position]) -> Dict[str, dict]:
    """브로커가 모르는 포지션 값만 뽑아낸다."""
    out: Dict[str, dict] = {}
    for sym, p in positions.items():
        out[sym] = {
            # qty 는 브로커가 이기는 값이지만, "우리가 알던 수량" 을 남겨 둬야
            # 재시작 때 달라진 것을 알아챌 수 있다. 이게 없으면 부분체결이나
            # 외부 매매로 수량이 바뀌어도 아무도 모른 채 지나간다.
            "qty": p.qty,
            "opened_at": p.opened_at.isoformat(),
            "stop_price": p.stop_price,
            "take_price": p.take_price,
            "highest_close": p.highest_close,
            "bars_held": p.bars_held,
            "stop_from_trail": p.stop_from_trail,
            "trail_pct": p.trail_pct,
            "entry_score": p.entry_score,
            "entry_votes": p.entry_votes,
        }
    return out


@dataclass
class ReconcileResult:
    positions: Dict[str, Position]
    #: 브로커에는 있는데 우리 기록에 없는 종목. 손절선이 없으니 위험하다.
    unknown: List[str] = field(default_factory=list)
    #: 우리 기록에는 있는데 브로커에 없는 종목. 이미 정리된 것.
    vanished: List[str] = field(default_factory=list)
    #: 수량이 우리 기록과 다른 종목 (부분체결·외부 매매)
    requantified: List[Tuple[str, int, int]] = field(default_factory=list)

    @property
    def notes(self) -> List[str]:
        out = []
        for sym in self.unknown:
            out.append(f"[복구] {sym}: 브로커에 있으나 기록에 없음 — "
                       f"손절선을 모른다. 수동 확인 필요")
        for sym in self.vanished:
            out.append(f"[복구] {sym}: 기록에 있으나 브로커에 없음 — 정리된 것으로 본다")
        for sym, was, now in self.requantified:
            out.append(f"[복구] {sym}: 수량 {was} → {now} (브로커 기준)")
        return out


def reconcile_positions(broker_positions: Dict[str, Position],
                        state: SessionState) -> ReconcileResult:
    """브로커 잔고를 뼈대로, 우리 기록에서 브로커가 모르는 값을 채운다.

    브로커가 이기는 것: 종목 존재 여부, 수량, 평균매입가.
    우리가 채우는 것: 손절가, 목표가, 트레일 폭, 진입 시각, 최고가, 보유 봉수.
    """
    meta = state.position_meta
    out: Dict[str, Position] = {}
    unknown: List[str] = []
    requantified: List[Tuple[str, int, int]] = []

    for sym, bp in broker_positions.items():
        m = meta.get(sym)
        if m is None:
            # 브로커에는 있는데 우리는 모른다. 다른 단말에서 샀거나, 우리가
            # 죽어 있는 동안 체결됐거나, 상태 파일이 날아갔다.
            # 버리지 않는다 — 버리면 이 포지션은 영영 청산되지 않는다.
            unknown.append(sym)
            out[sym] = bp
            continue
        was = int(m.get("bars_held", 0))
        pos = Position(
            symbol=sym,
            qty=bp.qty,                       # 브로커가 이긴다
            avg_price=bp.avg_price,           # 브로커가 이긴다
            opened_at=_parse_dt(m.get("opened_at")) or bp.opened_at,
            stop_price=m.get("stop_price"),
            take_price=m.get("take_price"),
            highest_close=float(m.get("highest_close") or 0.0),
            bars_held=was,
            stop_from_trail=bool(m.get("stop_from_trail")),
            trail_pct=m.get("trail_pct"),
            entry_score=float(m.get("entry_score") or 0.0),
            entry_votes=int(m.get("entry_votes") or 0),
        )
        out[sym] = pos
        prev_qty = int(m.get("qty", bp.qty)) if "qty" in m else bp.qty
        if prev_qty != bp.qty:
            requantified.append((sym, prev_qty, bp.qty))

    vanished = [s for s in meta if s not in broker_positions]
    return ReconcileResult(positions=out, unknown=unknown,
                           vanished=vanished, requantified=requantified)


def _parse_dt(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
