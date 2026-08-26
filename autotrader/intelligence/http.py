"""비밀값을 로그에 남기지 않는 작은 JSON HTTP 클라이언트."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Dict, Mapping, Optional


class HttpError(RuntimeError):
    pass


class JsonHttpClient:
    def request(self, method: str, url: str, *,
                headers: Optional[Mapping[str, str]] = None,
                query: Optional[Mapping[str, object]] = None,
                form: Optional[Mapping[str, object]] = None) -> Dict:
        if query:
            separator = "&" if "?" in url else "?"
            url += separator + urllib.parse.urlencode(query)
        body = None
        req_headers = dict(headers or {})
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            req_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
        request = urllib.request.Request(
            url, data=body, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except Exception as exc:
            # URL이나 헤더를 붙이면 API 키가 오류 로그로 새어 나갈 수 있다.
            raise HttpError(f"외부 정보 요청 실패: {type(exc).__name__}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError("외부 정보 응답이 JSON 형식이 아닙니다") from exc
        if not isinstance(value, dict):
            raise HttpError("외부 정보 응답의 최상위 값이 객체가 아닙니다")
        return value
