# CLAUDE.md

이 파일은 매 세션 자동으로 읽힌다. **짧게 유지한다** — 상세 내용은 `docs/`에 있다.

## 이 프로젝트

한국 주식 자동매매 시스템 (스크리닝 → 전략 앙상블 → Risk Engine → 브로커).
순수 Python, **표준 라이브러리만으로 동작**한다 (numpy/pandas/requests 없이).
CLI 하나로 백테스트·모의매매·시세수집이 전부 돌아간다. 화면(UI)은 없다.

`jogmany1-cmyk/diary` 저장소에서 `git subtree split` 으로 분리해 나왔다.
그래서 옛 커밋 메시지에 "autotrader v0.x" 같은 표기가 남아 있다.

## 사용자와 대화하는 방식 (항상 지킬 것)

1. **코드는 최종 명령이 있을 때만 작성/수정한다.** "해줘 / 작성해줘 / 수정해줘"처럼
   실행을 확정하는 말이 나오기 전에는 코드를 먼저 쓰지 않는다. 그 전에는
   무엇을 어떻게 바꿀지 **내용만 먼저 보여주고** 확인을 기다린다.
2. **용어는 처음 한 번 풀어서 설명하고, 이후엔 (7자 내외) 짧은 괄호 설명만 붙인다.**
   예: 처음 → "커밋(작업 내용을 저장 기록으로 남기는 것)" / 이후 → "커밋(저장기록)"
3. **선택지를 주거나 실행 여부를 물을 때는 결과까지 설명한다.** 그 선택/명령을
   실행하면 실제로 무슨 일이 일어나는지 함께 말해준다. 옵션만 나열하지 않는다.

## 실행 환경 — Windows

사용자는 **Windows + PowerShell**을 쓴다. 안내는 항상 Windows 기준으로 한다.

- `python` (`python3` 아님) · 환경변수는 `$env:VAR='값'` (`export` 아님)
- 명령 예시의 경로는 `data/kiwoom` 처럼 `/` 로 써도 PowerShell·Python 양쪽에서
  동작한다. 사람에게 폴더 위치를 설명할 때만 `바탕화면\autotrader` 처럼 쓴다.
- **`scripts/verify.sh` 는 PowerShell 에서 안 돈다** — Git Bash 에서 돌려야 한다.
- **cron 이 없다.** `autotrader schedule` 이 뱉는 crontab 라인을 Windows 작업
  스케줄러가 소비할 수 없다. 이 갭은 아직 메워져 있지 않다.

런타임 코드 자체는 Windows-clean 이다 (추정이 아니라 확인함): POSIX 전용 임포트
없음, 하드코딩된 POSIX 경로 없음, `os.path.join` 사용, 모든 `open()` 이 encoding
명시(cp949 로 한글 깨지지 않음), CSV 쓰기가 `newline=""` 지정.

## 검증 상태 — PRE-LIVE / UNDER VALIDATION

**단위 테스트 통과는 매매 유효성이나 수익성의 증거가 아니다.**

승격 경로 (건너뛰기 금지):

```
키움 실데이터 → 데이터 무결성 → 과거 유니버스 → 백테스트 → OOS
  → 레지스트리 승인 → 페이퍼 트레이딩 → 사람의 명시적 승인 → 실전
```

AI 는 승격을 **권고**할 수 있으나 **실전 승인은 절대 내릴 수 없다.**

데이터 무결성 단계에는 실행 가능한 게이트가 있다. `autotrader validate-data` 는
가격 데이터가 내부적으로 모순이면 0 이 아닌 코드로 종료한다. 새로 수집한 캐시는
백테스트 전에 반드시 통과시킨다 — `CsvProvider` 가 파싱 실패한 행을 조용히
버리고 정렬까지 해버리기 때문에, 이 게이트가 없으면 깨진 데이터가 아무 불평 없이
백테스트에 도달한다.

비자명한 변경은: 조사 → 계획 → 구현 → 테스트 → 검증 → 보고.
**검증하지 않은 동작을 검증했다고 보고하지 않는다.**

의미 있는 실패를 발견하면 산문 규칙을 늘리는 대신
**실패 → 회귀 테스트 → 수정 → 실행 가능한 게이트** 로 만든다.

## 절대 깨면 안 되는 규칙 6가지

상세와 근거는 `docs/ARCHITECTURE.md` 에 있다.

1. **미래 정보 금지.** 전략은 `[0..at]` 구간만 본다. 오늘 종가로 판단하고 다음 봉
   시가에 체결한다. `StrategyContext.at` 이 경계선이다.
2. **RiskEngine 이 모든 진입의 최종 거부권을 갖는다.** 신호는 권고일 뿐이다.
   새 안전장치는 전략이 아니라 `RiskEngine.evaluate_entry(...)` 에 붙인다.
3. **비용은 항상 포함한다.** `Fill.cost` 를 우회하면 백테스트가 수익을 과장한다.
4. **전략은 순수·무상태.** `__init__` 에서 설정하고 `evaluate(ctx)` 에서 판단한다.
5. **`DataProvider` 가 백테스트와 실매매의 경계면이다.** 전략·리스크 코드에서
   벤더 API 를 직접 부르지 않는다.
6. **LLM 은 주문을 내지 않는다.** AI 성격의 판단은 앙상블 *앞단* 입력으로만 둔다.

## 자주 쓰는 명령

```bash
# 합성 데이터로 전 구간 점검
python -m autotrader screen --top 5
python -m autotrader --threshold 0.45 backtest

# 시세 수집 (KIWOOM_APP_KEY 등 환경변수 필요)
python -m autotrader fetch --cache data/kiwoom --symbol 005930 --limit 500

# 데이터 무결성 게이트 — 실데이터 백테스트 전에 반드시
python -m autotrader --csv data/kiwoom validate-data              # 전체 이력
python -m autotrader --csv data/kiwoom validate-data --bars 2500  # 쓸 구간만

# 진입 신호에 우위가 있는지 (청산 규칙 제외) — 전략을 고치기 전에 먼저
python -m autotrader --csv data/kiwoom --threshold 0.45 edge --bars 2500

# 전략 승인 게이트
python -m autotrader paper --registry runs/registry.json --validated-only

# 재시작을 건너야 하는 상태(손절선·일일카운터·쿨다운)를 남기며 돌린다
python -m autotrader paper --state runs/state.json --order-log runs/orders.jsonl
```

**전역 옵션(`--csv` `--config` `--threshold` `--votes` `--trail`)은 하위 명령
앞에 쓴다.** 뒤에 쓰면 argparse 가 거부한다.

## 브랜치 · PR

`main` 에서 개발한다. 옛 통합 저장소와 달리 분리해 둘 다이어리 콘텐츠가 없다.

**PR 생성을 먼저 권하지 않는다.** 화면의 "PR 생성" 버튼은 항상 떠 있지만, 그것을
누르라고 말하는 것은 아래 셋 중 하나일 때뿐이다. 그 외에는 언급조차 하지 않는다
— 사용자는 버튼이 보일 때마다 눌러야 하나 고민하게 되고, 그 고민 자체가 비용이다.

1. 사용자가 **직접 요청**했을 때
2. 외부에 **코드 감사를 맡기려** 할 때 (검토 범위를 링크 하나로 특정할 수 있다)
3. 한 덩어리 작업이 **검증까지 끝나** 본선에 합칠 준비가 됐을 때

작업 결과는 브랜치에 push 하는 것만으로 이미 안전하다. PR 은 보관이 아니라
**검토를 요청하는 행위**다.

## 문서 목차

| 언제 | 파일 |
|---|---|
| 구조·데이터 흐름·저장 위치를 알아야 할 때 | `docs/ARCHITECTURE.md` |
| 테스트·검증 실행법, 새 전략/설정 추가 절차 | `docs/CONVENTIONS.md` |
| **작업 시작 전 한 번 훑을 것** — 반복해서 걸린 함정들 | `docs/PITFALLS.md` |
