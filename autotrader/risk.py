"""Risk Engine — 시스템의 헌법.

전략이 아무리 강한 신호를 내도 여기서 거부하면 주문은 나가지 않는다.
포지션 사이징(ATR × 1R = 자본의 X%) 과 계좌 단위 한도(동시 보유·일일 손실·
현금 여유)를 함께 처리한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from .config import RiskLimits
from .models import Position


@dataclass
class RiskDecision:
    allowed: bool
    qty: int = 0
    reason: str = ""
    risk_per_share: float = 0.0


@dataclass
class RiskState:
    """리스크 엔진이 계좌 전체를 통제하기 위해 보유하는 상태."""
    day: Optional[date] = None
    day_start_equity: float = 0.0
    day_realized_pnl: float = 0.0
    day_new_entries: int = 0
    consecutive_losses: int = 0
    cooldown_until: Optional[date] = None

    def roll_day(self, today: date, equity: float) -> None:
        if self.day != today:
            self.day = today
            self.day_start_equity = equity
            self.day_realized_pnl = 0.0
            self.day_new_entries = 0

    def register_trade_pnl(self, pnl: float) -> None:
        self.day_realized_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0


class RiskEngine:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.state = RiskState()

    def new_day(self, today: date, equity: float) -> None:
        self.state.roll_day(today, equity)
        # 쿨다운이 오늘이면 오늘까지 진입 금지, 내일부터 다시 허용
        if self.state.cooldown_until and today > self.state.cooldown_until:
            self.state.cooldown_until = None
            self.state.consecutive_losses = 0

    def evaluate_entry(self, *, symbol: str, price: float, stop_price: Optional[float],
                       equity: float, cash: float,
                       positions: Dict[str, Position], score: float = 1.0,
                       last_bar_return: Optional[float] = None) -> RiskDecision:
        L = self.limits
        if price <= 0:
            return RiskDecision(False, 0, "price<=0")
        if symbol in positions:
            return RiskDecision(False, 0, "already-held")

        # 계좌 레벨 게이트
        if self.state.cooldown_until:
            return RiskDecision(False, 0, "cooldown")
        if len(positions) >= L.max_positions:
            return RiskDecision(False, 0, "max-positions")
        # 일일 거래 상한 (v0.7): 회전율 폭주 방지 그물.
        if self.state.day_new_entries >= L.max_trades_per_day:
            return RiskDecision(False, 0, "max-trades-per-day")
        loss_frac = -self.state.day_realized_pnl / self.state.day_start_equity if self.state.day_start_equity else 0.0
        if loss_frac >= L.daily_loss_stop_pct:
            return RiskDecision(False, 0, "daily-loss-stop")
        if self.state.consecutive_losses >= L.max_consecutive_losses:
            return RiskDecision(False, 0, "consec-losses")
        # 최고점 매수 방지 (v0.7): 직전 봉 급등 종목은 차단.
        if (L.chase_filter_pct > 0 and last_bar_return is not None
                and last_bar_return >= L.chase_filter_pct):
            return RiskDecision(False, 0, f"chase-filter {last_bar_return*100:.1f}%")

        gross_now = sum(p.qty * price for p in positions.values())  # 러프 estimate
        if gross_now / equity > L.max_gross_exposure:
            return RiskDecision(False, 0, "gross-exposure")

        # 사이징 1 : 1R 기준. stop 이 없으면 가격의 3% 를 임시 stop 으로 잡는다.
        if stop_price is None or stop_price >= price:
            stop_price = price * 0.97
        risk_per_share = price - stop_price
        if risk_per_share <= 0:
            return RiskDecision(False, 0, "bad-stop")
        risk_budget = equity * L.per_trade_risk_pct * max(0.5, min(1.5, score * 1.2))
        qty_by_risk = int(risk_budget // risk_per_share)

        # 사이징 2 : 종목당 최대 비중.
        qty_by_position = int((equity * L.max_position_pct) // price)
        # 사이징 3 : 현금 한도.
        max_spendable = max(0.0, cash - equity * L.min_cash_pct)
        qty_by_cash = int(max_spendable // price)

        qty = max(0, min(qty_by_risk, qty_by_position, qty_by_cash))
        if qty <= 0:
            return RiskDecision(False, 0, f"qty=0 (r{qty_by_risk} p{qty_by_position} c{qty_by_cash})",
                                risk_per_share=risk_per_share)
        return RiskDecision(True, qty, "ok", risk_per_share)

    def register_entry(self) -> None:
        """실제 주문 접수 성공 시 호출. 일일 거래 카운터 증가."""
        self.state.day_new_entries += 1

    def register_exit(self, pnl: float, today: date) -> None:
        self.state.register_trade_pnl(pnl)
        if self.state.consecutive_losses >= self.limits.max_consecutive_losses:
            self.state.cooldown_until = today
