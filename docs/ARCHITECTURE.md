# ARCHITECTURE — 구조와 데이터 흐름

## 한눈에 보는 파이프라인

단계 순서가 곧 설계다. 위 단계는 아래 단계를 모르고, 아래 단계는 위 단계를
되돌리지 않는다. 이 순서를 흐트러뜨리면 계층이 서로 얽힌다.

```
DataProvider (CSV | Synthetic | Kiwoom REST)
   → Screener (tier1 가격 → tier2 지표 → tier3 랭킹)
      → Ensemble — 전략 5종 가중 투표
         (DayBreakout, DayPullback, DayMomentum, SwingTrend, MeanReversion)
         → RiskEngine (사이징 + 계좌한도 + 쿨다운 + 추격금지 + 일일상한 + 하드스톱)
            → Broker (PaperBroker | KISBroker | KiwoomBroker)   ← 최종 권한
               → Portfolio (트레일링 스탑, EOD 청산, 목표/손절 청산)
                  → PredictionTracker + Metrics (CostAudit, 승률, PF, Sortino, MDD)
```

## 진입점

| 진입점 | 하는 일 |
|---|---|
| `python -m autotrader <명령>` | `autotrader/__main__.py` → `cli.py:main()` |
| `cli.py` 의 `cmd_*` 함수 | 명령 하나당 하나. 여기서 provider·config 를 조립한다 |
| `jobs.py` 의 `job_*` 함수 | 스케줄러(cron/작업 스케줄러)가 부르는 실제 동작 |
| `live.py: LiveTrader.cycle()` | 실매매 1회 사이클 — 스크리닝→앙상블→리스크→주문 |
| `backtest.py: Backtester.run()` | 과거 데이터 재생. train/val/OOS 자동 분할 |

## 데이터가 어디에 저장되나

**데이터베이스가 없다.** 전부 파일이다.

| 무엇 | 어디에 | 형식 |
|---|---|---|
| 일봉 시세 | `{cache}/{종목코드}.csv` | CSV (date,open,high,low,close,volume) |
| 분봉 시세 | `{cache}/{종목코드}_{분}m.csv` | CSV, 일봉과 분리 |
| 전략 승인 기록 | `runs/registry.json` (경로 지정) | JSON |
| 과거 상장 종목 스냅샷 | `KrxUniverse` 지정 경로 | JSONL |
| 무결성 검사 리포트 | `--output` 로 지정 | JSON |

`KiwoomProvider` 의 캐시 형식은 `CsvProvider` 와 **의도적으로 완전히 동일**하다.
그래서 수집을 멈추고 오프라인 백테스트로 전환해도 나머지 코드가 그대로 돈다.
병합은 날짜 기준이며 새 데이터가 우선한다.

`.gitignore` 가 `data/` `runs/` `state/` `config.yaml` `.env` 를 막는다 —
수집한 시세와 자격증명은 저장소에 들어가지 않는다.

## 폴더 구조

```
autotrader/
  models.py        기본 자료구조 (Bar, Signal, Position, Trade)
  indicators.py    순수 파이썬 기술적 지표
  config.py        숫자 설정의 단일 진실 (Costs·RiskLimits·Universe·Weights)
  market.py        KRX 휴장일 + NXT 확장세션 + KST 시각 (now_kst)
  data/            DataProvider 추상 + CSV·Synthetic·Kiwoom·KrxUniverse
  strategy/        전략 5종 + Ensemble
  screener.py      3티어 팩터 랭킹
  risk.py          RiskEngine — 진입의 최종 관문
  portfolio.py     포지션·트레일링 스탑·라운드트립 기록
  broker/          PaperBroker · KISBroker · KiwoomBroker
  backtest.py      이벤트 기반 백테스트
  live.py          LiveTrader — 페이퍼/실계좌 공통 사이클
  dataquality.py   데이터 무결성 검사 (백테스트 이전 관문)
  metrics.py       성과지표 + CostAudit
  registry.py      전략 승인 저장소
  scheduler.py     cron 파서 + JobRegistry
  jobs.py          스케줄러가 부르는 실제 동작 5종
  notify.py        알림 팬아웃 (실패해도 매매에 영향 없음)
  reconciler.py    두 데이터 원천 대조 (KRX vs KRX+NXT 누락 감지)
  streaming/       실시간 스트림 추상 + 키움 조건검색 WebSocket
```

## 외부 연동

| 대상 | 모듈 | 자격증명 | 상태 |
|---|---|---|---|
| 키움 REST (시세) | `data/kiwoom.py` | `KIWOOM_APP_KEY/SECRET/ACCOUNT_NUMBER/MODE` | 실접속 진행 중 |
| 키움 REST (주문) | `broker/kiwoom.py` | 위와 동일 | **실접속 미검증** |
| 키움 WebSocket | `streaming/kiwoom_ws.py` | 토큰 재사용 | **스켈레톤, 미검증** |
| 한국투자증권 | `broker/kis.py` | `KIS_APP_KEY/SECRET/ACCOUNT_NUMBER/MODE` | **실접속 미검증** |

`requests` · `websockets` · `yaml` 은 **선택적 의존성**이며 반드시 함수 안에서
늦게 임포트한다. 그래야 어댑터 모듈도 임포트는 성공하고, 실제 호출 시점에만
실패한다. 이 규칙은 `scripts/check_stdlib_only.py` 가 강제한다.

## 주요 하위 시스템

- **`market.py`** — KRX 휴장일 표(2024~2027) + NXT 세션(pre 08:00–08:59,
  regular 09:00–15:30, after 15:30–20:00). `session=closed` 면 사이클 자체를
  건너뛴다. **모든 판정은 한국시간 기준** — `now_kst()` 를 쓴다.
- **`registry.StrategyRegistry`** — JSON 승인 저장소. `paper --validated-only` 는
  최신 OOS 백테스트가 기준(PF≥1.2, 거래≥20, MDD≥-0.25, 90일 이내)을 통과한
  전략만 앙상블에 넣는다. 실전은 이 게이트를 절대 우회하지 않는다.
- **`reconciler.SourceReconciler`** — 같은 조건식을 두 provider 에 돌려
  `only_in_secondary` 를 누락 집합으로 보고한다 (KRX 단독 vs KRX+NXT 통합 문제).
- **`dataquality.py`** — ERROR(데이터 모순) / WARN(백테스트 왜곡) 2단계로 분류.
  거짓 경보를 막는 두 장치: 휴장일 표 범위 밖은 결측 판정 생략, 봉마다 걸리는
  검사는 종목·코드 단위로 묶어 보고.
- **`KrxUniverse`** — 생존자 편향 방어. `union_between(start, end)` 는 그 기간의
  **합집합**(폐지 종목 포함)을 돌려준다.
