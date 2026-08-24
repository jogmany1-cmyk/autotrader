# CONVENTIONS — 검사 도구와 작업 절차

## 이 프로젝트에 실제로 있는 검사 도구

지어내지 않은, 실제로 존재하는 것만 적는다.

| 도구 | 무엇을 검사하나 |
|---|---|
| `pytest` | 단위 테스트 약 160개 |
| `scripts/check_stdlib_only.py` | 런타임 코어가 stdlib 만으로 도는지 |
| `autotrader validate-data` | 시세 데이터 무결성 |
| `scripts/verify.sh` | 위 셋 + 합성 스모크를 한 번에 |
| `.github/workflows/verify.yml` | 푸시마다 CI 가 `verify.sh` 를 실행 |

린트(코드 스타일 검사)나 타입 체크 도구는 **없다.**

## 변경 후 무엇을 돌리나 (Windows 기준)

**PowerShell 에서:**

```
python -m pytest -q
python scripts/check_stdlib_only.py
```

**전체 검증은 `scripts/verify.sh` 인데 bash 스크립트다.** PowerShell 에서는 안
돈다. 시작 메뉴에서 **Git Bash** 를 열고 (Git for Windows 설치 시 함께 깔림):

```
cd /c/Users/사용자이름/Desktop/autotrader
bash scripts/verify.sh
```

폴더 위치는 `바탕화면\autotrader` 이고, Git Bash 에서는 `/c/Users/...` 형태로
쓴다. 다른 캐시로 무결성 검사를 하려면 `CACHE=data/kospi bash scripts/verify.sh`.

**푸시하면 CI 가 같은 스크립트를 자동으로 돌린다** (Python 3.9/3.11/3.13 + 선택
의존성 포함 환경). 로컬에서 못 돌려도 푸시하면 결과를 볼 수 있다.

## 테스트 개수가 환경에 따라 다르다

- 맨 환경(`pip install pytest` 만): **130 통과 + 10 건너뜀**
- `requests` 설치됨: **160 통과**

건너뛰는 10개는 키움·KIS 어댑터 테스트다. `requests` 가 선택적 의존성이라
`tests/_optional.py` 의 표식으로 자동 제외된다. CI 의 `vendor-adapters` 잡이
의존성을 깔고 이들을 실제로 돌린다 — 그 잡이 없으면 조용히 썩는다.

## 새 전략을 추가할 때

1. `strategy/` 에 `Strategy` 를 상속한 클래스를 만든다
2. `name` 을 **`StrategyWeights` 의 필드명과 정확히 일치**시킨다 —
   `Ensemble` 이 가중치 맵으로 전략을 찾기 때문이다
3. `config.py` 의 `StrategyWeights` 에 같은 이름의 필드를 추가한다
4. `__init__` 에서만 설정하고 `evaluate(ctx)` 에서만 판단한다 (무상태 유지)
5. `ctx.at` 이후의 봉을 절대 읽지 않는다
6. 테스트를 추가하고 `verify.sh` 를 통과시킨다

## 새 안전장치(리스크 규칙)를 추가할 때

**전략이 아니라 `risk.py` 에 넣는다.** 전략에 넣으면 다른 전략이 그 규칙을
우회한다. `RiskLimits` 에 숫자를 추가하고 `RiskEngine.evaluate_entry(...)` 에서
검사한다.

## 새 설정값을 추가할 때 체크리스트

`config.py` 는 숫자 설정의 단일 진실이다. 값을 추가하면 다음을 **함께** 갱신한다:

- [ ] 해당 dataclass 에 필드 + **기본값**
- [ ] 그 값을 실제로 읽는 곳 (기본값만 넣고 안 쓰면 죽은 설정이 된다)
- [ ] YAML 오버레이로 덮어쓸 수 있는지 확인 (`Config.load`)
- [ ] 필요하면 CLI 플래그 (`cli.py` 의 `add_argument`)
- [ ] 기본값이 바뀌면 백테스트 결과가 달라지므로 회귀 테스트

**자격증명은 `config.py` 밖에서 `os.getenv` 를 쓰지 않는다.**
`KISConfig.from_env()` / `KiwoomConfig.from_env()` 만이 유일한 읽기 지점이다.

## 외부 패키지를 쓰고 싶을 때

런타임 코어는 stdlib 만 쓴다. 정말 필요하면:

1. 모듈 상단이 아니라 **함수 안에서** 임포트한다
2. 없을 때의 동작을 정의한다 (`broker/kis.py` 가 본보기)
3. `pyproject.toml` 의 optional-dependencies 에 선언한다
4. 그 패키지를 요구하는 테스트에는 `tests/_optional.py` 의 표식을 붙인다
5. `python scripts/check_stdlib_only.py` 로 확인한다

이 검사는 "설치돼 있지 않은지"가 아니라 **임포트를 차단한 채로** 전 모듈을
임포트하고 백테스트를 한 바퀴 돌린다. 그래서 그 패키지가 깔린 머신에서도 유효하다.

## 코드 스타일

- 주석과 문서 문자열은 **한국어**로 쓴다 (기존 코드 전체가 그렇다)
- 오류 메시지도 한국어. 사용자가 그대로 읽는다
- 모든 `open()` 에 `encoding=` 를 명시한다 (Windows cp949 방지)
- CSV 를 쓸 때는 `newline=""` 을 넘긴다 (Windows 빈 줄 방지)
- 타입 힌트를 쓰되 `from __future__ import annotations` 를 함께 둔다 (3.9 호환)
