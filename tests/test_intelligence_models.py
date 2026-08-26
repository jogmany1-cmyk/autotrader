from datetime import datetime, timezone

from autotrader.intelligence.dedupe import canonical_url, deduplicate
from autotrader.intelligence.models import MarketEvent


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _event(**overrides):
    values = dict(source="test", region="KR", title="제목",
                  url="https://example.com/a", published_at=NOW)
    values.update(overrides)
    return MarketEvent(**values)


def test_event_id_is_stable_and_serialisable():
    a, b = _event(), _event()
    assert a.id == b.id
    assert a.as_dict()["published_at"] == NOW.isoformat()


def test_dedupe_removes_tracking_parameters_and_keeps_newest():
    older = _event(url="https://Example.com/a/?utm_source=x",
                   published_at=NOW.replace(hour=1))
    newer = _event(url="https://example.com/a",
                   published_at=NOW.replace(hour=2))
    assert canonical_url(older.url) == "https://example.com/a"
    assert deduplicate([older, newer]) == [newer]
