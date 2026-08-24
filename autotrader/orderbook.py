"""미결 주문 장부 — 상태머신을 실제로 움직이는 곳.

`orders.BrokerOrder` 는 주문 하나의 생애를 표현할 뿐이다. 그 주문들을 모아
두고, 체결통보를 올바른 주문으로 보내고, 프로세스가 죽어도 살아남게 하는 것이
여기다. 셋 중 하나라도 빠지면 상태머신은 장식이 된다.

디스크 형식은 JSONL 이다. 표준 라이브러리만으로 되고, 덧붙이기가 원자적에
가깝고, 사람이 열어볼 수 있다. 규모가 커지면 SQLite 로 바꾼다 — 그때
바꾸기 쉽도록 접근은 전부 이 클래스를 통한다.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

from .models import Side
from .orders import (OPEN, BrokerOrder, ExecutionReport, OrderStatus,
                     OrderStateError)


class OrderBook:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.orders: Dict[str, BrokerOrder] = {}      # client_order_id → 주문
        self._by_broker_id: Dict[str, str] = {}       # 브로커 주문번호 → client id
        if path and os.path.exists(path):
            self.load()

    # ---- 조회 ----------------------------------------------------------

    def get(self, client_order_id: str) -> Optional[BrokerOrder]:
        return self.orders.get(client_order_id)

    def by_broker_id(self, broker_order_id: str) -> Optional[BrokerOrder]:
        cid = self._by_broker_id.get(broker_order_id)
        return self.orders.get(cid) if cid else None

    def open_orders(self) -> List[BrokerOrder]:
        """아직 체결될 수 있는 주문. 재시작 복구 때 브로커에 되물을 대상."""
        return [o for o in self.orders.values() if o.status in OPEN]

    def open_symbols(self) -> Dict[str, int]:
        """종목별 미결 수량. 노출 한도 계산에 넣어야 실제 위험과 맞는다."""
        out: Dict[str, int] = {}
        for o in self.open_orders():
            if o.side is Side.BUY and o.remaining > 0:
                out[o.symbol] = out.get(o.symbol, 0) + o.remaining
        return out

    def entries_on(self, day: date) -> int:
        """그날 낸 신규 진입 주문 수. 거부된 것은 세지 않는다."""
        return sum(1 for o in self.orders.values()
                   if o.side is Side.BUY
                   and o.created_at.date() == day
                   and o.status is not OrderStatus.REJECTED)

    # ---- 기록 ----------------------------------------------------------

    def add(self, order: BrokerOrder) -> BrokerOrder:
        """주문을 장부에 넣는다. 같은 client_order_id 가 있으면 기존 것을 준다.

        이것이 중복 주문 방지의 마지막 방어선이다. 앞단에서 걸러야 정상이지만,
        재시도 경로나 스트림 중복 때문에 여기까지 오는 경우가 실제로 있다.
        """
        exist = self.orders.get(order.client_order_id)
        if exist is not None:
            return exist
        self.orders[order.client_order_id] = order
        if order.broker_order_id:
            self._by_broker_id[order.broker_order_id] = order.client_order_id
        self._append(order)
        return order

    def apply(self, report: ExecutionReport) -> Optional[BrokerOrder]:
        """체결통보를 해당 주문에 반영한다. 모르는 주문이면 None.

        모르는 주문번호가 오는 것은 정상이다 — 다른 단말이나 손으로 낸 주문의
        통보가 같은 계좌로 들어온다. 그것을 우리 주문인 척 반영하면 포지션
        기록이 틀어지므로, 조용히 무시하지 말고 None 을 돌려 호출부가
        기록하게 한다.
        """
        bo = self.by_broker_id(report.broker_order_id)
        if bo is None:
            return None
        changed = bo.apply(report)
        if changed:
            if bo.broker_order_id:
                self._by_broker_id[bo.broker_order_id] = bo.client_order_id
            self._append(bo)
        return bo

    def link(self, client_order_id: str, broker_order_id: str) -> None:
        self._by_broker_id[broker_order_id] = client_order_id
        bo = self.orders.get(client_order_id)
        if bo is not None:
            bo.broker_order_id = broker_order_id
            self._append(bo)

    # ---- 영속화 --------------------------------------------------------

    def _append(self, order: BrokerOrder) -> None:
        if not self.path:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(order.as_dict(), ensure_ascii=False) + "\n")

    def load(self) -> None:
        """JSONL 을 되읽는다. 같은 주문의 마지막 줄이 최신 상태다.

        체결 내역(fills)까지는 복원하지 않는다 — 재시작 후의 진실은 브로커
        잔고이지 우리 로그가 아니다. 여기서 복원하는 것은 "어떤 주문을 냈고
        어디까지 진행됐다고 알고 있었는가" 뿐이며, 브로커 조회로 덮어쓴다.
        """
        if not self.path or not os.path.exists(self.path):
            return
        latest: Dict[str, dict] = {}
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue            # 쓰다 만 줄 하나가 복구를 막으면 안 된다
                cid = row.get("client_order_id")
                if cid:
                    latest[cid] = row
        for cid, row in latest.items():
            try:
                bo = BrokerOrder(
                    client_order_id=cid,
                    symbol=row["symbol"],
                    side=Side(row["side"]),
                    qty=int(row["qty"]),
                    status=OrderStatus(row["status"]),
                    broker_order_id=row.get("broker_order_id"),
                    filled_qty=int(row.get("filled_qty", 0)),
                    reject_reason=row.get("reject_reason", ""),
                    tag=row.get("tag", ""),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
            except (KeyError, ValueError):
                continue
            bo._filled_notional = bo.filled_qty * float(row.get("avg_fill_price", 0.0))
            self.orders[cid] = bo
            if bo.broker_order_id:
                self._by_broker_id[bo.broker_order_id] = cid
