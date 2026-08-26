# 시장정보·그림자 판단 설계

## 목적과 안전 경계

이 기능은 한국·미국 뉴스와 공식 공시를 모아 아침 카카오톡 요약을 만들고,
기존 매수 후보에 대해 `allow / review / would_block` 가상 판단을 기록한다.

**실제 주문을 만들거나 차단하지 않는다.** `would_block` 는 "차단했으면 결과가
어땠을까"를 재기 위한 꼬리표다. 최소 60거래일과 충분한 매수 후보가 쌓이기 전에는
이 값을 `LiveTrader` 또는 `RiskEngine` 입력으로 연결하지 않는다.

Claude가 작업 중인 `swing_trend_v2_experimental`과 병행할 수 있도록 이 기능은
`autotrader/intelligence/`와 별도 실행 파일에만 둔다. 전략·손절·점수·주문 코드는
수정하지 않는다.

## 정보 원천

- 네이버 뉴스 검색: 기사 발견용. 일반 뉴스만으로 자동 차단하지 않는다.
- OpenDART: 한국 공식 공시. 고위험 단어가 공식 공시에 있을 때만
  `would_block` 그림자 판단을 만든다.
- SEC EDGAR: 미국 공식 공시. 8-K/6-K는 `review`, 10-Q/10-K는 정보로 기록한다.

특정 MCP 서버에 코어를 묶지 않는다. 실제 사용 가능한 MCP가 확인되면
`Collector` 함수 하나로 감싸 추가한다. 이름이 비슷한 미확인 MCP를 대신 연결하지
않는다.

## Windows 환경변수

비밀값은 저장소나 설정 JSON에 넣지 않는다. PowerShell에서 사용자 환경변수로
저장한다. 아래 값 자체를 채팅이나 로그에 붙이지 않는다.

```powershell
[Environment]::SetEnvironmentVariable("NAVER_CLIENT_ID", "발급값", "User")
[Environment]::SetEnvironmentVariable("NAVER_CLIENT_SECRET", "발급값", "User")
[Environment]::SetEnvironmentVariable("OPENDART_API_KEY", "발급값", "User")
[Environment]::SetEnvironmentVariable("SEC_USER_AGENT", "이름 email@example.com", "User")
[Environment]::SetEnvironmentVariable("KAKAO_REST_API_KEY", "발급값", "User")
[Environment]::SetEnvironmentVariable("KAKAO_REFRESH_TOKEN", "발급값", "User")
[Environment]::SetEnvironmentVariable("KAKAO_BRIEFING_URL", "등록한 웹주소", "User")
```

카카오 URL의 도메인은 카카오 개발자 앱의 제품 링크에 먼저 등록해야 한다.

## 관심목록 예시

실제 파일은 `config/intelligence.local.json`처럼 만들고 Git에 올리지 않는다.

```json
{
  "naver_queries": [
    {"query": "한국 증시 주요 이슈", "display": 20},
    {"query": "삼성전자", "symbol": "005930", "company": "삼성전자"}
  ],
  "dart_days": 1,
  "us_watchlist": [
    {"symbol": "AAPL", "cik": "0000320193"}
  ],
  "sec_days": 2,
  "holdings": ["005930", "AAPL"],
  "baseline_buy_symbols": []
}
```

## 먼저 카카오 없이 시험

```powershell
python scripts/morning_briefing.py --config config/intelligence.local.json --no-kakao
```

예상 결과:

- 콘솔에 한국·미국 요약이 표시된다.
- `data/intelligence/events.jsonl`에 수집 사건이 쌓인다.
- `data/intelligence/shadow_decisions.jsonl`에 가상 판단이 쌓인다.
- 한 공급자가 실패해도 나머지 보고서는 만들어지고 매매에는 영향이 없다.

카카오 인증이 끝난 뒤 `--no-kakao`만 제거한다. Windows 작업 스케줄러 등록은
수동 한 번 실행이 성공한 다음 단계에서 한다.
