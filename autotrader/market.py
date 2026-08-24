"""한국 주식시장 개장일 판정.

블로그 후기 개선판 ①: "공휴일인데 주문이 들어갈 뻔했다" → 사이클 자체를 스킵.
정식 캘린더 API 없이 표준 라이브러리만으로 처리하려고 (a) 주말과 (b) KRX
공식 휴장일 하드코딩(2024~2027) 을 함께 사용한다. 실전 배포 전에는 KIS
공식 휴장일 API 로 매년 초에 최신화하는 것이 안전하다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Set

# KRX 정규 휴장일 (연도별). 대체공휴일·임시공휴일 포함. 매년 초 갱신 필요.
#
# 이 표는 손으로 관리하므로 반드시 틀린다. 실제로 삼성전자 40년치 실데이터와
# 대조했더니 2026-03-04(공휴일 아닌데 등재), 2025-01-27(설 임시공휴일 누락),
# 2026-07-17(제헌절 부활 누락) 세 건이 어긋나 있었다.
#
# 검증 방법: 시세를 수집한 뒤 `autotrader --csv <캐시> validate-data` 를 돌린다.
#   bar_on_closed_day   → 이 표에 잘못 들어간 날
#   missing_trading_days → 이 표에서 빠진 휴장일
# 틀린 날짜 하나가 실제 거래일을 통째로 건너뛰게 만든다.
KRX_HOLIDAYS: Set[date] = {
    # 2024
    date(2024, 1, 1),  date(2024, 2, 9),  date(2024, 2, 12), date(2024, 3, 1),
    date(2024, 4, 10), date(2024, 5, 1),  date(2024, 5, 6),  date(2024, 5, 15),
    date(2024, 6, 6),  date(2024, 8, 15), date(2024, 9, 16), date(2024, 9, 17),
    date(2024, 9, 18), date(2024, 10, 1), date(2024, 10, 3), date(2024, 10, 9),
    date(2024, 12, 25), date(2024, 12, 31),
    # 2025
    date(2025, 1, 1),  date(2025, 1, 27), date(2025, 1, 28), date(2025, 1, 29),
    date(2025, 1, 30),
    date(2025, 3, 3),  date(2025, 5, 1),  date(2025, 5, 5),  date(2025, 5, 6),
    date(2025, 6, 3),  date(2025, 6, 6),  date(2025, 8, 15), date(2025, 10, 3),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8), date(2025, 10, 9),
    date(2025, 12, 25), date(2025, 12, 31),
    # 2026
    date(2026, 1, 1),  date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 2),  date(2026, 5, 1),  date(2026, 5, 5),  date(2026, 7, 17),
    date(2026, 5, 25), date(2026, 6, 3),  date(2026, 8, 17), date(2026, 9, 24),
    date(2026, 9, 25), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 9),
    date(2026, 12, 25), date(2026, 12, 31),
    # 2027 (임시공휴일 미확정분은 실전 배포 전 KIS API 로 재확인)
    date(2027, 1, 1),  date(2027, 2, 8),  date(2027, 2, 9),  date(2027, 3, 1),
    date(2027, 5, 5),  date(2027, 5, 13), date(2027, 6, 7),  date(2027, 8, 16),
    date(2027, 9, 14), date(2027, 9, 15), date(2027, 9, 16), date(2027, 10, 4),
    date(2027, 10, 8), date(2027, 12, 24), date(2027, 12, 31),
}

# 한국 표준시. 서머타임이 없으므로 고정 오프셋으로 정확하다.
# zoneinfo 를 쓰지 않는 이유: 윈도우에는 IANA 시간대 DB 가 없어 tzdata 패키지가
# 필요한데, 그러면 "런타임 코어는 stdlib 만" 제약이 깨진다.
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """한국 시간 기준 현재 시각 (naive).

    이 모듈의 장 시간 판정은 전부 한국 시간 기준이다. 호출자가
    `datetime.utcnow()` 를 넘기면 9시간이 어긋나 장중을 휴장으로,
    장 마감 후를 장중으로 판정한다. 시각이 필요한 곳은 이 함수를 쓴다.
    """
    return datetime.now(KST).replace(tzinfo=None)


# KRX 정규 매매시간 (09:00 ~ 15:30)
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# NXT(대체거래소) 세션 — 2025년 출범 이후 하루가 실질적으로 12시간(08:00~20:00)이 되었다.
# 서버 조건검색이 KRX 시세만 보는 반면 실 체결 가능 시간은 훨씬 길어, 프리·애프터
# 마켓에서 신호가 "보이는데 못 잡는" 간극을 낳는다. 이 판정은 통합 데이터 흐름의
# 첫 관문 역할을 한다.
NXT_PRE_OPEN = time(8, 0)
NXT_PRE_CLOSE = time(8, 59, 59)
NXT_AFTER_OPEN = time(15, 30, 1)
NXT_AFTER_CLOSE = time(20, 0)


def is_trading_day(d: date, extra_holidays: Iterable[date] = ()) -> bool:
    if d.weekday() >= 5:
        return False
    if d in KRX_HOLIDAYS:
        return False
    if d in set(extra_holidays):
        return False
    return True


def is_market_open(ts: datetime, extra_holidays: Iterable[date] = ()) -> bool:
    if not is_trading_day(ts.date(), extra_holidays):
        return False
    return MARKET_OPEN <= ts.time() <= MARKET_CLOSE


def reason_closed(ts: datetime) -> str:
    if ts.weekday() >= 5:
        return "weekend"
    if ts.date() in KRX_HOLIDAYS:
        return "holiday"
    if not (MARKET_OPEN <= ts.time() <= MARKET_CLOSE):
        return "off-hours"
    return "open"


# ---- NXT 확장 세션 ------------------------------------------------------

def session_of(ts: datetime, extra_holidays: Iterable[date] = ()) -> str:
    """현재 시각이 어느 세션에 속하는지 문자열로 돌려준다.

    "pre" : 프리마켓 (NXT 08:00 ~ 08:59)
    "regular" : KRX 정규장 (09:00 ~ 15:30)
    "after" : 애프터마켓 (NXT 15:30 ~ 20:00)
    "closed" : 위 어디에도 속하지 않음 (주말·공휴일 포함)
    """
    if not is_trading_day(ts.date(), extra_holidays):
        return "closed"
    t = ts.time()
    if NXT_PRE_OPEN <= t <= NXT_PRE_CLOSE:
        return "pre"
    if MARKET_OPEN <= t <= MARKET_CLOSE:
        return "regular"
    if NXT_AFTER_OPEN <= t <= NXT_AFTER_CLOSE:
        return "after"
    return "closed"


def is_extended_market_open(ts: datetime,
                            extra_holidays: Iterable[date] = (),
                            include_pre: bool = True,
                            include_after: bool = True) -> bool:
    """NXT 프리·애프터를 포함한 확장 매매 가능 시간 판정.

    include_pre / include_after 로 세션 별 참여 여부를 조절한다. 예를 들어
    사용자의 전략이 프리마켓 유동성을 신뢰하지 않으면 include_pre=False.
    """
    sess = session_of(ts, extra_holidays)
    if sess == "regular":
        return True
    if sess == "pre" and include_pre:
        return True
    if sess == "after" and include_after:
        return True
    return False
