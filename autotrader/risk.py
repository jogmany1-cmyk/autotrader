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
    """진입의 최종 관문. 전략 신호는 권고이고 여기서 거부하면 끝이다.

    `blocked_symbols` 는 공시·뉴스 기반 **수비용 거부 목록**이다. 알파(수익
    예측)가 아니라 위험 회피라는 점이 중요하다. 근거:

    - 뉴스를 알파로 쓰는 것은 근거가 없다. 호재는 1분 내 완전 반영되고
      (Busse & Green, JFE 2002), 묵은 뉴스에 반응하는 매매는 개인 과잉반응
      패턴으로 그 다음 주에 되돌아온다 (Tetlock, RFS 2011) — 개인 거래 비중이
      높은 종목일수록 반전이 크다.
    - 반면 수비로는 근거가 기계적이다. **거래정지된 종목은 어떤 가격에도 팔 수
      없다.** 손절·트레일링·EOD 청산이 전부 무력하다 (`exits.py` 의 어떤 규칙도
      호가가 없으면 체결되지 않는다). 사전 회피가 유일한 통제수단이다.
    - 그리고 필터는 **거래를 만들지 않으므로 회전율 비용이 0** 이다. 왕복
      31~103bp 장벽에 걸리지 않는 유일한 종류의 개선이다.

    그래서 이 목록은 감성 점수가 아니라 **열거된 하드 이벤트**로만 채워야
    한다. 넓히면 안 되는 이유: 한국에서 제3자배정 유상증자는 CAR +7.14% 로
    오히려 호재로 읽힌다 — "유상증자 = 악재" 같은 단순 규칙은 틀린다.
    """

    def __init__(self, limits: RiskLimits,
                 blocked_symbols: Optional[Dict[str, str]] = None):
        self.limits = limits
        self.state = RiskState()
        #: {종목코드: 사유}. 사유는 리포트에 그대로 실린다.
        self.blocked_symbols: Dict[str, str] = dict(blocked_symbols or {})

    def block(self, symbol: str, reason: str) -> None:
        """이 종목의 신규 진입을 금지한다. 보유분 청산은 막지 않는다."""
        self.blocked_symbols[symbol] = reason

    def unblock(self, symbol: str) -> None:
        self.blocked_symbols.pop(symbol, None)

    def new_day(self, today: date, equity: float) -> None:
        self.state.roll_day(today, equity)
        # 쿨다운이 오늘이면 오늘까지 진입 금지, 내일부터 다시 허용
        if self.state.cooldown_until and today > self.state.cooldown_until:
            self.state.cooldown_until = None
            self.state.consecutive_losses = 0

    @staticmethod
    def _gross_value(positions: Dict[str, Position],
                     position_prices: Optional[Dict[str, float]]) -> float:
        """보유 포지션의 현재 평가액 합계.

        예전에는 **지금 판단 중인 후보 종목의 가격**을 보유 종목 전부에 곱했다.
        7만원짜리 후보를 볼 때 5천원짜리 보유 10주가 70만원으로 계산된다 —
        14배 과대평가다. 반대 방향이면 노출 한도가 열려 있어야 할 때 닫힌다.
        어느 쪽이든 `max_gross_exposure` 가 의도한 값을 지키지 못한다.

        종목별 시세를 주는 것이 원칙이고, 없으면 그 종목의 평균매입가로
        떨어진다 (후보 가격을 쓰는 것보다 항상 낫다).
        """
        prices = position_prices or {}
        return sum(p.qty * prices.get(sym, p.avg_price)
                   for sym, p in positions.items())

    def evaluate_entry(self, *, symbol: str, price: float, stop_price: Optional[float],
                       equity: float, cash: float,
                       positions: Dict[str, Position], score: float = 1.0,
                       last_bar_return: Optional[float] = None,
                       position_prices: Optional[Dict[str, float]] = None) -> RiskDecision:
        L = self.limits
        if price <= 0:
            return RiskDecision(False, 0, "price<=0")
        if symbol in positions:
            return RiskDecision(False, 0, "already-held")
        # 공시·뉴스 수비 필터. 계좌 레벨 게이트보다 **앞**에 둔다 — 거래정지
        # 종목은 자리가 있든 없든, 쿨다운이든 아니든 사면 안 되기 때문이다.
        if symbol in self.blocked_symbols:
            return RiskDecision(False, 0, f"news-block {self.blocked_symbols[symbol]}")

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

        gross_now = self._gross_value(positions, position_prices)
        if equity > 0 and gross_now / equity > L.max_gross_exposure:
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
