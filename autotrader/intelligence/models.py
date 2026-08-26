"""외부 공급자가 달라도 공유하는 시장정보 자료구조."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List


def event_id(source: str, url: str, title: str, published_at: datetime) -> str:
    raw = "\x1f".join((source, url, title, published_at.isoformat()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class MarketEvent:
    source: str
    region: str                 # KR | US
    title: str
    url: str
    published_at: datetime
    symbol: str = ""
    company: str = ""
    summary: str = ""
    event_type: str = "news"
    severity: str = "info"     # info | watch | high
    official: bool = False
    tags: List[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(
                self, "id",
                event_id(self.source, self.url, self.title, self.published_at),
            )

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["published_at"] = self.published_at.isoformat()
        return out


@dataclass(frozen=True)
class ShadowDecision:
    """실제 주문에는 적용하지 않는 뉴스 위험 가상 판단."""
    symbol: str
    baseline_action: str
    shadow_action: str          # allow | review | would_block
    reasons: List[str] = field(default_factory=list)
    event_ids: List[str] = field(default_factory=list)
    evaluated_at: datetime | None = None

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["evaluated_at"] = (self.evaluated_at.isoformat()
                               if self.evaluated_at else None)
        return out
