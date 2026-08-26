"""카카오톡 '나에게 보내기' 어댑터.

토큰은 생성자나 환경변수로만 받고 로그·저장 파일에는 남기지 않는다.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from ..notify import Notification, NotificationChannel
from .http import JsonHttpClient


class KakaoTokenManager:
    endpoint = "https://kauth.kakao.com/oauth/token"

    def __init__(self, rest_api_key: str, refresh_token: str, *,
                 client_secret: str = "",
                 http: Optional[JsonHttpClient] = None):
        if not rest_api_key or not refresh_token:
            raise ValueError("KAKAO_REST_API_KEY/REFRESH_TOKEN 이 필요합니다")
        self.rest_api_key = rest_api_key
        self.refresh_token = refresh_token
        self.client_secret = client_secret
        self.http = http or JsonHttpClient()

    def access_token(self) -> str:
        form = {"grant_type": "refresh_token",
                "client_id": self.rest_api_key,
                "refresh_token": self.refresh_token}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        data = self.http.request("POST", self.endpoint, form=form)
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("카카오 액세스 토큰 갱신 실패")
        return token


class KakaoMemoChannel(NotificationChannel):
    endpoint = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

    def __init__(self, token_manager: KakaoTokenManager, link_url: str, *,
                 http: Optional[JsonHttpClient] = None,
                 max_chars: int = 180):
        if not link_url.startswith(("https://", "http://")):
            raise ValueError("카카오 앱에 등록한 KAKAO_BRIEFING_URL 이 필요합니다")
        self.token_manager = token_manager
        self.link_url = link_url
        self.http = http or JsonHttpClient()
        self.max_chars = max_chars

    @classmethod
    def from_env(cls, http: Optional[JsonHttpClient] = None
                 ) -> "KakaoMemoChannel":
        manager = KakaoTokenManager(
            os.getenv("KAKAO_REST_API_KEY", ""),
            os.getenv("KAKAO_REFRESH_TOKEN", ""),
            client_secret=os.getenv("KAKAO_CLIENT_SECRET", ""), http=http,
        )
        return cls(manager, os.getenv("KAKAO_BRIEFING_URL", ""), http=http)

    def _chunks(self, text: str) -> List[str]:
        chunks, current = [], ""
        for line in text.splitlines() or [text]:
            candidate = line if not current else current + "\n" + line
            if len(candidate) <= self.max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            while len(line) > self.max_chars:
                chunks.append(line[:self.max_chars])
                line = line[self.max_chars:]
            current = line
        if current:
            chunks.append(current)
        return chunks or [""]

    def send(self, notification: Notification) -> None:
        token = self.token_manager.access_token()
        text = notification.title
        if notification.body:
            text += "\n" + notification.body
        for chunk in self._chunks(text):
            template = {
                "object_type": "text", "text": chunk,
                "link": {"web_url": self.link_url,
                         "mobile_web_url": self.link_url},
                "button_title": "상세 보기",
            }
            result = self.http.request(
                "POST", self.endpoint,
                headers={"Authorization": f"Bearer {token}"},
                form={"template_object": json.dumps(
                    template, ensure_ascii=False, separators=(",", ":"))},
            )
            if int(result.get("result_code", -1)) != 0:
                raise RuntimeError("카카오 메시지 전송 실패")
