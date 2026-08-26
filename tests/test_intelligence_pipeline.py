import json
from datetime import datetime, timezone

from autotrader.intelligence.models import MarketEvent
from autotrader.intelligence.pipeline import MorningIntelligencePipeline
from autotrader.intelligence.store import IntelligenceStore
from autotrader.notify import Notifier, RecordingChannel


NOW = datetime(2026, 8, 26, 7, 30, tzinfo=timezone.utc)


def test_pipeline_isolates_collector_failure_and_records_shadow_decision(tmp_path):
    event = MarketEvent("opendart", "KR", "유상증자 결정",
                        "https://dart.example/a", NOW,
                        symbol="005930", official=True)
    channel = RecordingChannel()

    def broken():
        raise RuntimeError("secret-bearing failure")

    pipeline = MorningIntelligencePipeline(
        [("dart", lambda: [event]), ("news", broken)],
        store=IntelligenceStore(tmp_path), notifier=Notifier([channel]))
    report = pipeline.run(now=NOW, holdings=["005930"],
                          baseline_buy_symbols=["005930"])

    assert report.failed_collectors == ["news"]
    assert report.decisions[0].shadow_action == "would_block"
    assert "실제 주문에는 미반영" in report.body
    assert "수집 실패(매매 영향 없음): news" in report.body
    assert len(channel.received) == 1
    event_rows = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    decision_rows = (tmp_path / "shadow_decisions.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert json.loads(event_rows[0])["severity"] == "high"
    assert json.loads(decision_rows[0])["shadow_action"] == "would_block"
