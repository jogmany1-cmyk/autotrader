"""사건과 그림자 판단을 JSONL 감사기록으로 남긴다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import MarketEvent, ShadowDecision


class IntelligenceStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _append(self, name: str, rows: Iterable[dict]) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        count = 0
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        return count

    def append_events(self, events: Iterable[MarketEvent]) -> int:
        return self._append("events.jsonl", (event.as_dict() for event in events))

    def append_decisions(self, decisions: Iterable[ShadowDecision]) -> int:
        return self._append("shadow_decisions.jsonl",
                            (decision.as_dict() for decision in decisions))
