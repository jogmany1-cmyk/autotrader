"""시장 뉴스·공시 정보층.

이 패키지는 매매 엔진과 의도적으로 분리한다. 수집 결과는 아침 요약과
그림자 판단에 쓰이며, 매매에 닿는 경로는 **단 하나** — `veto` 의 수비용
거부 목록뿐이다. 그 경로조차 한 방향이다: 살 수 있던 것을 못 사게 만들 뿐,
사지 않던 것을 사게 만들지 않는다.

뉴스를 수익 신호(알파)로 쓰지 않는 이유와 수비로는 쓰는 이유는
`veto.py` 의 모듈 주석에 근거와 함께 적어 두었다.
"""

from .models import MarketEvent, ShadowDecision
from .risk import classify_event
from .shadow import ShadowPolicy
from .veto import (HARD_BLOCK_TERMS, apply_to_risk_engine, build_block_list,
                   matched_terms)

__all__ = ["MarketEvent", "ShadowDecision", "ShadowPolicy", "classify_event",
           "HARD_BLOCK_TERMS", "apply_to_risk_engine", "build_block_list",
           "matched_terms"]
