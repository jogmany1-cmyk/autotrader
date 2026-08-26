from datetime import datetime

from autotrader.intelligence.kakao import KakaoMemoChannel, KakaoTokenManager
from autotrader.notify import Notification


class QueueHttp:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results.pop(0)


def test_kakao_refreshes_token_and_sends_chunked_self_messages():
    http = QueueHttp([{"access_token": "access-secret"},
                      {"result_code": 0}, {"result_code": 0},
                      {"result_code": 0}])
    manager = KakaoTokenManager("rest-key", "refresh-secret", http=http)
    channel = KakaoMemoChannel(manager, "https://brief.example/report",
                               http=http, max_chars=12)
    channel.send(Notification(datetime(2026, 8, 26), "info",
                              "아침요약", "123456789012345"))

    assert len(http.calls) == 4
    token_form = http.calls[0][1]["form"]
    assert token_form["refresh_token"] == "refresh-secret"
    for _, kwargs in http.calls[1:]:
        assert kwargs["headers"]["Authorization"] == "Bearer access-secret"
        assert "refresh-secret" not in str(kwargs)
        assert "template_object" in kwargs["form"]
