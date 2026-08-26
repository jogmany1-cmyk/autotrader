"""여러 검색어·공급자에서 반복된 사건을 한 번만 남긴다."""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import MarketEvent


TRACKING_KEYS = frozenset(("utm_source", "utm_medium", "utm_campaign",
                           "utm_term", "utm_content", "fbclid", "gclid"))


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.casefold() not in TRACKING_KEYS]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path.rstrip("/"), urlencode(query), ""))


def _title_key(title: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", title.casefold())


def deduplicate(events: Iterable[MarketEvent]) -> List[MarketEvent]:
    seen: set[Tuple[str, str]] = set()
    out = []
    for event in sorted(events, key=lambda e: e.published_at, reverse=True):
        url = canonical_url(event.url)
        key = (url, "") if url else ("", _title_key(event.title))
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out
