"""보유 포지션 관리. 현금·자산·트레일링스톱·강제청산 규칙을 담는다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Bar, Fill, Position, Side, Trade


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    closed_trades: List[Trade] = field(default_factory=list)

    # --- 자산가치 -----------------------------------------------------------

    def equity(self, prices: Dict[str, float]) -> float:
        mv = sum(p.qty * prices.get(sym, p.avg_price) for sym, p in self.positions.items())
        return self.cash + mv

    def exposure(self, prices: Dict[str, float]) -> float:
        return sum(p.qty * prices.get(sym, p.avg_price) for sym, p in self.positions.items())

    # --- 체결 반영 ----------------------------------------------------------

    def apply_fill(self, fill: Fill,
                   stop: Optional[float] = None,
                   target: Optional[float] = None,
                   trail: Optional[float] = None,
                   entry_score: float = 0.0,
                   entry_votes: int = 0,
                   entry_factors: Optional[Dict[str, float]] = None) -> Optional[Trade]:
        """체결 하나를 반영하고, 이번 체결이 라운드 트립을 완료했다면 Trade 를 리턴."""
        self.cash -= fill.cost
        if fill.side is Side.BUY:
            self._apply_buy(fill, stop, target, trail, entry_score, entry_votes,
                            entry_factors)
            return None
        return self._apply_sell(fill)

    def _apply_buy(self, fill: Fill, stop: Optional[float],
                   target: Optional[float],
                   trail: Optional[float] = None,
                   entry_score: float = 0.0,
                   entry_votes: int = 0,
                   entry_factors: Optional[Dict[str, float]] = None) -> None:
        pos = self.positions.get(fill.symbol)
        if pos is None:
            self.positions[fill.symbol] = Position(
                symbol=fill.symbol, qty=fill.qty,
                avg_price=(fill.gross + fill.fee) / fill.qty,
                opened_at=fill.ts, stop_price=stop, take_price=target,
                highest_close=fill.price, trail_pct=trail,
                entry_score=entry_score, entry_votes=entry_votes,
                entry_factors=dict(entry_factors or {}),
            )
            return
        # 추가 매수: 가중평균 단가 갱신, 스탑은 유지(더 높인 것만 반영)
        new_qty = pos.qty + fill.qty
        pos.avg_price = (pos.avg_price * pos.qty + fill.gross + fill.fee) / new_qty
        pos.qty = new_qty
        if stop is not None and (pos.stop_price is None or stop > pos.stop_price):
            pos.stop_price = stop
        if target is not None:
            pos.take_price = target

    def _apply_sell(self, fill: Fill) -> Optional[Trade]:
        pos = self.positions.get(fill.symbol)
        if pos is None or pos.qty < fill.qty:
            raise RuntimeError(f"{fill.symbol}: 팔 수 없는 수량 {fill.qty}")
        entry_price = pos.avg_price
        proceeds = fill.gross - fill.fee - fill.tax
        pnl = proceeds - entry_price * fill.qty
        pct = pnl / (entry_price * fill.qty) if entry_price > 0 else 0.0
        trade = Trade(
            symbol=fill.symbol, entry_ts=pos.opened_at, exit_ts=fill.ts,
            qty=fill.qty, entry_price=entry_price, exit_price=fill.price,
            pnl=pnl, return_pct=pct, exit_reason=fill.tag or "manual",
            bars_held=pos.bars_held, entry_score=pos.entry_score,
            entry_votes=pos.entry_votes,
            entry_factors=dict(pos.entry_factors),
        )
        self.closed_trades.append(trade)
        pos.qty -= fill.qty
        if pos.qty <= 0:
            del self.positions[fill.symbol]
        return trade

    # --- 매일 아침의 후처리 --------------------------------------------------

    def bump_hold_counters(self) -> None:
        for pos in self.positions.values():
            pos.bars_held += 1

    def update_trailing(self, prices: Dict[str, float], trail_pct: float) -> None:
        """트레일링 스탑: 최고 종가 대비 trail_pct 만큼 스탑을 끌어올린다.
        내려가지는 않는다.
        """
        for sym, pos in self.positions.items():
            price = prices.get(sym)
            if price is not None:
                update_trailing_stop(pos, price, trail_pct)


def update_trailing_stop(pos, price: float, trail_pct: float) -> bool:
    """포지션 하나의 트레일링 스탑을 갱신한다. 올렸으면 True.

    포트폴리오 밖으로 꺼내 둔 이유: 실계좌에는 페이퍼의 `mark()` 같은 경로가
    없어서 스탑을 끌어올리는 주체가 아무도 없었다. 트레일링이 이름만 남는다.
    두 경로가 같은 함수를 부르게 해야 갈라지지 않는다.
    """
    if price <= pos.highest_close:
        return False
    pos.highest_close = price
    # 포지션별 폭이 있으면 그것을 쓴다 (진입 시점 ATR 기반).
    width = pos.trail_pct if pos.trail_pct is not None else trail_pct
    if width <= 0:
        return False
    trailing = pos.highest_close * (1 - width)
    if pos.stop_price is None or trailing > pos.stop_price:
        pos.stop_price = trailing
        pos.stop_from_trail = True
        return True
    return False
