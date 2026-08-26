from datetime import date

import pytest

from autotrader.intelligence.providers import (NaverNewsProvider,
                                                OpenDartProvider,
                                                SecEdgarProvider)


class FakeHttp:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def test_naver_maps_json_and_strips_html():
    http = FakeHttp({"items": [{
        "title": "<b>삼성전자</b> 실적", "description": "&quot;상승&quot;",
        "originallink": "https://news.example/a",
        "pubDate": "Wed, 26 Aug 2026 07:00:00 +0900",
    }]})
    event = NaverNewsProvider("id", "secret", http).search(
        "삼성전자", symbol="005930", company="삼성전자")[0]
    assert event.title == "삼성전자 실적"
    assert event.summary == '"상승"'
    assert event.symbol == "005930"
    assert http.calls[0][1]["headers"]["X-Naver-Client-Secret"] == "secret"


def test_dart_maps_official_disclosure_and_no_data_status():
    http = FakeHttp({"status": "000", "list": [{
        "corp_code": "001", "corp_name": "테스트", "stock_code": "123456",
        "report_nm": "유상증자 결정", "rcept_no": "202608260001",
        "rcept_dt": "20260826", "corp_cls": "Y",
    }]})
    event = OpenDartProvider("key", http).disclosures(
        date(2026, 8, 26), date(2026, 8, 26))[0]
    assert event.official is True
    assert event.symbol == "123456"
    assert event.url.endswith("202608260001")
    assert OpenDartProvider("key", FakeHttp({"status": "013"})).disclosures(
        date(2026, 8, 26), date(2026, 8, 26)) == []


def test_sec_maps_recent_watched_forms_and_requires_identified_user_agent():
    http = FakeHttp({"name": "Example Inc", "filings": {"recent": {
        "form": ["8-K", "4"], "filingDate": ["2026-08-25", "2026-08-25"],
        "accessionNumber": ["0001-26-000002", "0001-26-000003"],
        "primaryDocument": ["a.htm", "b.htm"],
        "primaryDocDescription": ["Current report", "Insider trade"],
    }}})
    events = SecEdgarProvider("owner@example.com", http).filings(
        "1234", "EXM", since=date(2026, 8, 25))
    assert len(events) == 1
    assert events[0].tags == ["8-K"]
    assert events[0].symbol == "EXM"
    assert "/1234/000126000002/a.htm" in events[0].url
    with pytest.raises(ValueError):
        SecEdgarProvider("anonymous", http)
