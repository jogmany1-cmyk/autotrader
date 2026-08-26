"""공시 → RiskEngine 거부 목록.

정보층에서 **유일하게 매매에 영향을 주는 경로**다. 그리고 그 영향은 한
방향뿐이다: 살 수 있던 것을 못 사게 만든다. 사지 않던 것을 사게 만들지
않는다.

왜 이 방향만 허용하는가
------------------------
뉴스를 수익 신호(알파)로 쓰는 것은 증거가 반대편에 있다:

- 호재는 **1분 내** 완전 반영, 악재도 15분 (Busse & Green, JFE 2002).
- 묵은 뉴스에 반응하는 매매는 **개인 과잉반응 패턴**이고 다음 주에 되돌아온다
  (Tetlock, RFS 2011). 반전은 개인 거래 비중이 높은 종목에서 더 크다.
  전날 공시를 아침에 요약해 매매 신호로 쓰는 것이 정확히 그 패턴이다.
- 한국 시장에서 개인은 아노말리를 **만들어내는** 쪽이고 외국인·기관이 그것을
  가져간다 (Pacific-Basin Finance Journal, 2024).

반면 수비로는 근거가 통계가 아니라 **제도**다:

- **거래정지 종목은 어떤 가격에도 팔 수 없다.** `exits.py` 의 hard_stop ·
  stop · trail · time · eod_flat 이 전부 무력하다 — 호가가 없으면 어떤 청산
  규칙도 체결되지 않는다. 사전 회피가 유일한 통제수단이다.
- 필터는 거래를 만들지 않으므로 **회전율 비용이 0** 이다. 왕복 31~103bp 의
  비용 장벽에 걸리지 않는 유일한 종류의 개선이다.

좁게 유지해야 하는 이유
-----------------------
`HARD_BLOCK_TERMS` 는 **열거된 하드 이벤트**만 담는다. 감성 점수나 토픽
분류로 넓히면 안 된다. 반례가 실제로 있다: 한국에서 **제3자배정 유상증자는
CAR +7.14%** 로 오히려 호재로 읽힌다. "유상증자 = 악재" 같은 단순 규칙은
틀리고, 틀린 필터는 살 수 있었던 것을 못 사게 만들어 조용히 손해를 낸다.

그래서 여기 있는 항목의 기준은 "나쁜 뉴스인가"가 아니라 **"이 사건이 발생하면
포지션을 정상적으로 청산할 수 없거나, 주주 지분이 기계적으로 훼손되는가"** 다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Sequence

from .models import MarketEvent

#: 청산 불능 또는 기계적 지분 훼손. 공시 제목에 이 표현이 있으면 신규 진입 금지.
#:
#: 각 항목이 여기 있는 이유:
#:   거래정지·매매거래정지  → 어떤 가격에도 못 판다 (유일하고 결정적인 근거)
#:   상장폐지·정리매매      → 청산 자체가 시한부가 된다
#:   실질심사               → 거래정지로 이어지는 직전 단계
#:   관리종목               → 지정 요건이 이미 발생했다는 뜻
#:   감사의견 거절/한정      → 상장폐지 사유. 회계 신뢰가 무너진 상태
#:   자본잠식               → 상장폐지 요건
#:   횡령·배임              → 거래정지·실질심사로 직행하는 사유
#:   회생절차·파산·부도      → 청산 불능
#:   불성실공시법인          → 유의하게 음의 초과수익 (Han et al., APJFS 2014)
#:   무상감자               → 주식 수가 기계적으로 줄어든다
HARD_BLOCK_TERMS = (
    "거래정지", "매매거래정지", "상장폐지", "정리매매", "실질심사",
    "관리종목", "감사의견거절", "감사의견 거절", "의견거절",
    "감사의견한정", "감사의견 한정", "자본잠식",
    "횡령", "배임", "회생절차", "파산", "부도",
    "불성실공시", "무상감자",
)

#: 해제 표현. `거래정지**해제**` 를 정지로 오독하면 정상 종목을 영구히
#: 배제하게 된다. 실제 DART 피드에 "주권매매거래정지해제" 가 흔하다.
RELEASE_TERMS = ("해제", "해소", "취소", "철회", "종료")

#: 차단 유지 기간(일). 사건은 지나가지만 흔적은 남으므로 무한정 막지 않는다.
DEFAULT_BLOCK_DAYS = 90


def _text(event: MarketEvent) -> str:
    return f"{event.title} {event.summary}"


def matched_terms(event: MarketEvent) -> list:
    """이 사건이 걸리는 하드 이벤트 표현들. 해제 공시면 빈 목록."""
    text = _text(event)
    hits = [t for t in HARD_BLOCK_TERMS if t in text]
    if not hits:
        return []
    # 해제 공시는 차단하지 않는다. 표현이 붙어 있는지로만 판단한다 —
    # "거래정지해제" 는 막으면 안 되고 "거래정지" 는 막아야 한다.
    for term in hits:
        idx = text.find(term)
        tail = text[idx + len(term): idx + len(term) + 6]
        if any(r in tail for r in RELEASE_TERMS):
            hits = [h for h in hits if h != term]
    return hits


def build_block_list(events: Sequence[MarketEvent], *,
                     now: Optional[datetime] = None,
                     block_days: int = DEFAULT_BLOCK_DAYS,
                     official_only: bool = True) -> Dict[str, str]:
    """사건 목록에서 `RiskEngine.blocked_symbols` 로 쓸 dict 를 만든다.

    `official_only=True` (기본) 이면 **공식 공시만** 차단 근거로 인정한다.
    일반 뉴스는 오보·재전송 가능성이 있어 자동 차단으로 승격하지 않는다 —
    잘못된 차단은 조용히 기회를 없앤다.

    종목코드가 없는 사건은 무시한다. 어느 종목을 막을지 특정할 수 없으면
    막지 않는 쪽이 안전하다.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=block_days)
    out: Dict[str, str] = {}
    for event in events:
        symbol = (event.symbol or "").strip()
        if not symbol:
            continue
        if official_only and not event.official:
            continue
        if event.published_at < cutoff:
            continue
        hits = matched_terms(event)
        if not hits:
            continue
        reason = f"{hits[0]}({event.published_at.date().isoformat()})"
        # 같은 종목에 여러 건이면 가장 최근 사건을 남긴다.
        out[symbol] = reason
    return out


def apply_to_risk_engine(engine, events: Sequence[MarketEvent], **kwargs) -> Dict[str, str]:
    """거부 목록을 만들어 엔진에 얹고, 얹은 목록을 돌려준다.

    기존 목록을 **덮어쓰지 않고 합친다** — 다른 경로(수동 차단 등)로 넣은
    항목을 지우면 안 되기 때문이다.
    """
    blocks = build_block_list(events, **kwargs)
    for symbol, reason in blocks.items():
        engine.block(symbol, reason)
    return blocks
