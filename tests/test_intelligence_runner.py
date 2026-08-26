import json
from datetime import datetime, timezone

from autotrader.intelligence.runner import run_morning_briefing


def test_runner_works_without_credentials_when_kakao_is_disabled(
        tmp_path, monkeypatch, capsys):
    for name in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "OPENDART_API_KEY",
                 "SEC_USER_AGENT", "KAKAO_REST_API_KEY", "KAKAO_REFRESH_TOKEN",
                 "KAKAO_BRIEFING_URL"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "watch.json"
    config.write_text(json.dumps({"holdings": ["005930"]}), encoding="utf-8")

    report = run_morning_briefing(
        config, output_dir=tmp_path / "out", send_kakao=False,
        now=datetime(2026, 8, 26, 7, 30, tzinfo=timezone.utc))

    assert report.events == []
    assert "비활성 공급자(환경변수 없음): naver, opendart, sec-edgar" in report.body
    assert "아침 시장 요약" in capsys.readouterr().err
