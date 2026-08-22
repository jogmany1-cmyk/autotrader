"""페이퍼 트레이딩 브로커.

수수료/세금/슬리피지를 실제와 비슷하게 흉내내며 백테스트·모의매매 양쪽에서
쓴다. `mark(prices)` 를 매 봉 호출해서 손절/익절/트레일링을 트리거한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from ..config import Costs
from ..models import Fill, Order, Position, Side, Trade
from ..portfolio import Portfolio
from .base import Broker, BrokerError


class PaperBroker(Broker):
    def __init__(self, initial_cash: float, costs: Costs):
        self.portfolio = Portfolio(cash=initial_cash)
        self.costs = costs
        self.fills: List[Fill] = []

    # --- 계좌 상태 ----------------------------------------------------------
    def cash(self) -> float:
        return self.portfolio.cash

    def positions(self) -> Dict[str, Position]:
        return dict(self.portfolio.positions)

    def equity(self, prices: Dict[str, float]) -> float:
        return self.portfolio.equity(prices)

    # --- 주문 ---------------------------------------------------------------
    def submit(self, order: Order, price_hint: float,
               ts: Optional[datetime] = None,
               stop: Optional[float] = None,
               target: Optional[float] = None) -> Fill:
        ts = ts or datetime.utcnow()
        slip = self.costs.slippage_bp / 10_000
        if order.side is Side.BUY:
            fill_price = price_hint * (1 + slip)
        else:
            fill_price = price_hint * (1 - slip)
        gross = fill_price * order.qty
        fee = gross * (self.costs.commission_bp / 10_000)
        tax = gross * (self.costs.tax_sell_bp / 10_000) if order.side is Side.SELL else 0.0
        fill = Fill(ts=ts, symbol=order.symbol, side=order.side, qty=order.qty,
                    price=round(fill_price, 4), fee=round(fee, 4),
                    tax=round(tax, 4), tag=order.tag)
        # 매수 시 현금 부족 검증. 매도는 보유 검증이 portfolio 안에서 일어난다.
        cost = fill.cost
        if order.side is Side.BUY and cost > self.portfolio.cash + 1e-6:
            raise BrokerError(f"cash insufficient: need {cost:.0f}, have {self.portfolio.cash:.0f}")
        trade = self.portfolio.apply_fill(fill, stop=stop, target=target)
        self.fills.append(fill)
        return fill

    # --- 매 봉 후처리 -------------------------------------------------------
    def mark(self, bars: Dict[str, "BarLike"], ts: datetime,
             trail_pct: float = 0.0, max_hold: Optional[int] = None) -> List[Trade]:
        """각 종목의 당일 봉을 받아 스탑/타깃/시간 청산을 트리거.

        return : 이번 mark 로 종결된 트레이드들.
        """
        closed: List[Trade] = []
        prices = {s: b.close for s, b in bars.items()}
        if trail_pct > 0:
            self.portfolio.update_trailing(prices, trail_pct)
        for sym in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[sym]
            bar = bars.get(sym)
            if bar is None:
                continue
            exit_price: Optional[float] = None
            reason = ""
            if pos.stop_price is not None and bar.low <= pos.stop_price:
                # 갭다운으로 스탑 아래에서 시가가 열리면 시가로 체결
                exit_price = min(pos.stop_price, bar.open)
                reason = "stop"
            elif pos.take_price is not None and bar.high >= pos.take_price:
                exit_price = max(pos.take_price, bar.open)
                reason = "target"
            elif max_hold is not None and pos.bars_held >= max_hold:
                exit_price = bar.close
                reason = "time"
            if exit_price is None:
                continue
            order = Order(symbol=sym, side=Side.SELL, qty=pos.qty, tag=reason)
            trade = self._sell_at(order, exit_price, ts)
            if trade:
                closed.append(trade)
        self.portfolio.bump_hold_counters()
        return closed

    def _sell_at(self, order: Order, price: float, ts: datetime) -> Optional[Trade]:
        slip = self.costs.slippage_bp / 10_000
        fill_price = price * (1 - slip)
        gross = fill_price * order.qty
        fee = gross * (self.costs.commission_bp / 10_000)
        tax = gross * (self.costs.tax_sell_bp / 10_000)
        fill = Fill(ts=ts, symbol=order.symbol, side=Side.SELL, qty=order.qty,
                    price=round(fill_price, 4), fee=round(fee, 4), tax=round(tax, 4),
                    tag=order.tag)
        trade = self.portfolio.apply_fill(fill)
        self.fills.append(fill)
        return trade


class BarLike:  # 문서용 프로토콜
    close: float
    open: float
    high: float
    low: float
