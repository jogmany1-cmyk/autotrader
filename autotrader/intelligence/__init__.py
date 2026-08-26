"""시장 뉴스·공시 정보층.

이 패키지는 매매 엔진과 의도적으로 분리한다. 수집 결과는 아침 요약과
그림자 판단에만 쓰이며 실제 주문을 만들거나 차단하지 않는다.
"""

from .models import MarketEvent, ShadowDecision
from .risk import classify_event
from .shadow import ShadowPolicy

__all__ = ["MarketEvent", "ShadowDecision", "ShadowPolicy", "classify_event"]
