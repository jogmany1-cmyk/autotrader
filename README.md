# autotrader — AI 주식 자동매매 시스템

공유하신 설계 방향(전략 병렬 경쟁 · Trading Score · Risk Engine 상위 배치 · 엄격한 백테스트 · LLM/알고리즘 분리)을 그대로 코드로 옮긴 파이썬 구현입니다.
외부 의존성 없이(표준 라이브러리만으로) 백테스트·모의매매·CLI 가 전부 돌아가며, 한국투자증권 Open API 는 옵션(어댑터 스텁 포함)입니다.

## 1. 아키텍처

```
데이터공급자(DataProvider)
    │
    ▼
스크리너(팩터 점수 랭킹)  ─── 유동성 · 최소가격 하드 필터
    │
    ▼
전략 앙상블(가중 합산)
  ├─ DAY-01 Breakout    (Donchian 돌파 + 거래량 스파이크 + ATR)
  ├─ DAY-02 Pullback    (추세 안 눌림 → 반전 확인 → RSI 중립)
  ├─ DAY-03 Momentum    (ROC + 신고가 근접 근사)
  ├─ SWING-01 Trend     (50/200 정배열 + 200봉 위치 + 모멘텀)
  └─ Mean-Reversion     (장기 추세 안의 과매도 반등 · 분산 기여)
    │
    ▼
Risk Engine  ─── 종목당 1R · 종목당 최대비중 · 동시보유 상한
    │            일일 손실 스톱 · 연속손절 쿨다운 · 현금 여유
    ▼
브로커(PaperBroker · KISBroker)
    │
    ▼
포트폴리오(트레일링 스탑 · 스탑/타깃/시간 청산)
```

* **전략은 오늘 종가로 판단하고 내일 시가에 체결됩니다** — 미래 정보가 새어 들어가지 않도록.
* **수수료·거래세·슬리피지**를 페이퍼 브로커가 실제와 비슷하게 반영합니다 — 백테스트가 현실을 과장하지 않도록.
* **AI(LLM)는 이 코어에서 주문 버튼을 누르지 않습니다.** 뉴스/공시 해석 같은 보조 신호로 붙일 자리(`ensemble` 앞단)는 남겨두되, 실행은 결정론적 규칙과 Risk Engine 이 담당합니다.

## 2. 빠른 시작

```bash
# 합성 데이터로 스크리너 · 백테스트 · 페이퍼 매매 사이클 확인
python -m autotrader screen --top 5
python -m autotrader --threshold 0.45 backtest
python -m autotrader --threshold 0.45 --votes 1 paper --cycles 3 --dry-run

# 실제 CSV 로 (date,open,high,low,close,volume 헤더, 한국 헤더도 자동 인식)
python -m autotrader --csv data/kospi backtest --output out.json
```

전역 옵션: `--csv`, `--config`, `--threshold`, `--votes`, `--trail` 는 하위 명령보다 **앞에** 씁니다.

## 3. 백테스트 성과 리포트 항목

승률 하나로 판단하지 않기 위해 다음을 함께 계산합니다.

* Net Return / CAGR / Max Drawdown
* Sharpe / Sortino
* Profit Factor · Expectancy · Payoff Ratio
* Win Rate · 평균이익 · 평균손실 · 최대연속손실
* 트레이드 수 · 평균 노출도 · 일수

리포트는 자동으로 **TRAIN → VALIDATION → OUT-OF-SAMPLE** 세 구간으로 나뉩니다. 파라미터 튜닝은 VAL 까지만 하고, 실전 판단은 **OOS 성적**만 봐야 과최적화를 피할 수 있습니다.

## 4. 한국투자증권(KIS) 연결

`autotrader/broker/kis.py` 가 REST 얇은 래퍼로 들어 있습니다. 사용 전:

```bash
export KIS_APP_KEY=...
export KIS_APP_SECRET=...
export KIS_ACCOUNT_NUMBER=...
export KIS_MODE=paper   # 모의투자. 실계좌는 real
pip install requests    # 선택 의존성
```

토큰은 24시간 캐시하며(정책상 1일 1회 발급 원칙), 잔고·주문 엔드포인트가 정의돼 있습니다. **실시간 시세와 체결통보**는 WebSocket 이 필요하며, 붙일 수 있게 인터페이스는 마련해 뒀지만 기본 구현은 REST 만입니다 — 실계좌 운영 전에 반드시 자기 계정으로 손으로 확인하시고, tr_id/필드가 KIS 문서의 최신값과 맞는지 확인해 주세요.

## 5. 정직하게 말씀드리는 것

* **"미래 주가를 맞히는 AI"는 이 프로젝트가 하려는 일이 아닙니다.** 여기서 하는 일은 규칙 기반 신호를 재현 가능하게 만들고, 손실을 통제하는 것입니다. LLM 은 나중에 뉴스/공시 스코어링에서 붙일 자리만 남겨뒀습니다.
* **합성 데이터의 백테스트 성적은 코드가 돈다는 증거일 뿐, 전략이 돈을 번다는 증거가 아닙니다.** 실제 KOSPI/KOSDAQ 데이터로 다시 돌리고, 여러 파라미터를 VAL 로 튜닝, OOS 로만 채택 여부를 결정하세요.
* **개발 순서 권장**: 백테스트 → 모의투자 → 소액 실전(수십만 원) → 정상 실전. 이 순서를 건너뛰지 마세요.
* **본인 계좌 자동매매는 자유**지만, 다른 사람의 자금을 굴리려면 투자자문/일임 인허가 문제가 별도로 있습니다. 사업화는 별도 법률 검토가 필요합니다.

## 6. 테스트

```bash
pip install pytest
pytest -q
```

27개 테스트가 지표·데이터·전략·리스크·포트폴리오·브로커·백테스트·스크리너·성과지표를 검증합니다.

## 7. 파일 지도

```
autotrader/
  models.py           기본 자료구조 (Bar, Signal, Position, Trade …)
  indicators.py       순수 파이썬 기술적 지표
  config.py           Costs · RiskLimits · Universe · Weights · Backtest · KIS
  data/               DataProvider · CsvProvider · SyntheticProvider
  strategy/           DayBreakout · DayPullback · DayMomentum · SwingTrend · MeanReversion · Ensemble
  screener.py         팩터 랭킹 (모멘텀 · 추세 · 유동성 · 저변동성)
  risk.py             Risk Engine — 사이징 · 계좌한도 · 쿨다운
  portfolio.py        포지션 · 트레일링 스탑 · 라운드트립 트레이드 기록
  broker/             PaperBroker · KISBroker (KIS Open API 스텁)
  metrics.py          성과지표
  backtest.py         이벤트 기반 백테스트, 자동 train/val/OOS 분할
  live.py             LiveTrader — 페이퍼/실계좌 공통 사이클
  cli.py              `python -m autotrader …`
tests/                pytest 스위트 (27개)
```


## 8. 실운영 경험에서 반영한 개선 사양 (v0.2)

블로그 후기 세 편의 **최종 상태만** 뽑아 코드에 반영했습니다. (초기 시행착오는 참고만.)

| # | 개선 항목 | 구현 위치 |
|---|-----------|-----------|
| ① | 휴장일이면 사이클 자체 스킵 (주말·KRX 공휴일) | `market.py`, `LiveTrader.cycle`, `Backtester.run` |
| ② | 브로커의 실제 잔고를 진실의 기준(SoT)으로 사용 (수동매매 대응) | `LiveTrader.cycle` 앞부분에서 `broker.positions()` 재조회 |
| ③ | 익절은 쿨다운 없음, 손절/AI 매도/시간청산만 N일 쿨다운 | `cooldown.py` (`COOLDOWN_EXEMPT_REASONS`) |
| ④ | 진입시 예측(신뢰도·목표가·손절가) 저장 → 청산시 실측과 비교, 승률/목표가 도달률/신뢰도 구간별 성과 | `tracker.py` (`PredictionTracker`) |
| ⑤ | 개별 전략 스탑과 별개로 계좌 보호용 하드 손절(-10%) | `PaperBroker.mark(hard_stop_pct=)`, `RiskLimits.hard_stop_loss_pct` |
| ⑥ | ETF vs 개별주 프로파일 분기 (임계값·트레일링·손절폭 상이) | `SymbolProfiles`, `Config.symbol_kinds` |
| ⑦ | 앙상블 SELL 임계값 별도 설정 | `RiskLimits.ensemble_sell_threshold` |
| ⑧ | 일 3사이클 스케줄 (09:30/13:00/15:00) — 외부 스케줄러가 호출, 사이클 자체는 시장시간 판정 | `market.is_market_open` |

**정직하게 짚고 넘어가는 것**

- 원문 블로그의 성적(투자금 10만원, 2주간 -2,427원 후 반등)은 표본이 극도로 작고 시장 상승기에 뽑힌 스냅샷입니다. 우리 코드가 그 성적을 재현한다는 뜻이 전혀 아닙니다.
- "GPT-5.5" 같은 특정 LLM 모델을 프롬프트로 신호 생성하는 부분은 **이번 v0.2 에서 코어에 넣지 않았습니다**. 이유: (a) 결정론적으로 재현 가능한 백테스트가 어렵고, (b) API 비용이 하루 3사이클 기준으로 월 2~3만원 이상이 되며, (c) Risk Engine 이 있는 한 굳이 실행 라인에 LLM 을 두지 않아도 뉴스·공시 스코어를 앙상블 앞단에 붙일 자리는 이미 있음. 붙이려면 `Ensemble` 앞에 `LLMFactor` 를 하나 추가하고 `weights.llm_news` 를 두는 게 최소 침습적입니다.


## 9. NXT 통합 · 검증 게이트 (v0.3 / v0.4)

블로그 참고글의 아키텍처 지침을 반영해 실전 배포에 필요한 두 관문을 추가.

### v0.3 — NXT 확장 세션 & SourceReconciler
- **`market.session_of(ts)`** 는 `pre` (08:00~08:59), `regular` (09:00~15:30),
  `after` (15:30~20:00), `closed` 넷 중 하나를 돌려준다.
- **`market.is_extended_market_open(ts, include_pre, include_after)`** 로
  세션 참여 여부 결정. `LiveTrader.allow_pre_market / allow_after_market` 로 조절.
- **`reconciler.SourceReconciler(primary, secondary)`** 는 두 DataProvider
  (KRX 단독 / KRX+NXT 통합)에 같은 조건식을 돌려 `only_in_secondary` (=서버
  조건에서 새어나간 종목)를 리포트한다. `python -m autotrader reconcile
  --primary krx/ --secondary integrated/` 로 사용.

### v0.4 — Screener 3-티어 로그 & 검증 게이트
- **Screener 3-티어**: `tier1(price) → tier2(indicator) → tier3(ranking)`.
  각 단계 통과 종목 수는 `screener.last_stats.as_line()` 으로 감사 로그.
  값이 싼 판정을 먼저 돌려 API 조회 한도를 넘겨 연결이 끊기는 사고를 예방.
- **StrategyRegistry**: 전략별 최근 백테스트 성적을 JSON 파일로 관리.
  기본 승인 기준은 **OOS Profit Factor ≥ 1.20**, **트레이드 ≥ 20**,
  **MDD ≥ -25%**, **90일 이내 재검증**. `python -m autotrader validate
  --registry r.json` 로 통과 여부 확인.
- **`--validated-only`**: `paper` 실행 시 레지스트리에서 통과한 전략만 앙상블에
  들어감. "검증되지 않은 규칙은 실행되지 않는다" 원칙의 코드 게이트.


## 10. 실시간 조건검색 스트림 (v0.5)

키움 조건검색 실시간 튜토리얼(WebSocket) 참고. **폴링과 별개로** 실시간
이벤트를 소비해 즉시 앙상블·리스크 게이트로 전달하는 계층 추가.

### 신설 모듈
- **`streaming.base.StreamClient`** — 벤더 무관 추상. 백그라운드 스레드에서
  이벤트를 큐로 emit, `events()` / `drain()` 두 소비 API 제공.
- **`streaming.local.LocalStream`** — 사전 등록 + 런타임 `push()` 를 지원하는
  로컬 목(mock). 테스트·데모 전용.
- **`streaming.kiwoom_ws.KiwoomConditionStream`** — 키움 WebSocket 스켈레톤:
  `wss://mockapi.kiwoom.com:10000/...` (모의) / `wss://api.kiwoom.com:10000/...`
  (실), `access_token` 2차 로그인, `TRNM=PING` 자동 반사, `TRNM=REAL` 이벤트에서
  9001 필드를 종목 코드로 파싱. `websockets` 는 옵션 의존성 (없어도 임포트 성공,
  실행 시점에만 실패).

### LiveTrader 통합
`trader.stream` 에 `StreamClient` 를 붙이면 매 사이클 끝에서 큐에 쌓인 이벤트를
`drain()` 하여 **신호가 뜬 종목을 즉시 앙상블에 넣고, Risk Engine 통과 시 주문
발주**. 폴링 로직은 그대로 유지되므로 스트림 없이도 동작한다.

```python
from autotrader.streaming import KiwoomConditionStream
trader.stream = KiwoomConditionStream(
    access_token=token, condition_seq="0", is_paper=True,
)
trader.stream.start()
try:
    while True:
        rep = trader.cycle()
        print(rep.stream_events, "실시간 이벤트 처리")
finally:
    trader.stream.stop()
```

- **주의**: 실 접속은 이 저장소에서 테스트하지 않는다. 벤더 최신 문서(TR ID·
  필드 이름·URL 변경 여부)를 실전 배포 전에 반드시 확인.
- **REST vs 스트림**: REST 는 우편, 스트림은 전화. 조건검색 실시간처럼 서버가
  push 하는 이벤트는 스트림으로만 놓치지 않는다.


## 11. 키움 REST 어댑터 (v0.6)

Chapter 0 (Kiwoom REST API 입문·설정 편) 참고. v0.5 의
`KiwoomConditionStream(WebSocket)` 과 짝을 이루는 REST 쪽 어댑터.

### 신설
- **`config.KiwoomConfig`** — `app_key`, `app_secret`, `account_number`,
  `is_paper`. `KiwoomConfig.from_env()` 는 `KIWOOM_APP_KEY` / `KIWOOM_APP_SECRET`
  / `KIWOOM_ACCOUNT_NUMBER` / `KIWOOM_MODE` (기본 paper) 환경변수 사용.
- **`broker.KiwoomBroker`** — REST 얇은 래퍼. 자격증명 비면 즉시 명확한 예외로
  실패. 실전(`api.kiwoom.com`) / 모의(`mockapi.kiwoom.com`) URL 자동 분기.
  OAuth 토큰 12h 캐시(만료 60초 전에만 재발급). `Authorization: Bearer` +
  `appkey` + `appsecret` + `api-id` 헤더 조합.
- **`Broker.list_stocks(market_code)`** — 종목 마스터 조회 훅. 기본은 빈 리스트,
  Kiwoom 은 `ka10099` TR 로 오버라이드.

### 사용 흐름
```python
from autotrader.broker import KiwoomBroker
from autotrader.config import KiwoomConfig
from autotrader.streaming import KiwoomConditionStream

cfg = KiwoomConfig.from_env()          # env 에서 로드
rest = KiwoomBroker(cfg)               # 잔고·주문
token = rest._ensure_token()           # 조건검색 스트림 인증에도 재사용
ws = KiwoomConditionStream(access_token=token, condition_seq="0",
                           is_paper=cfg.is_paper)
```

### 주의
- 실제 TR ID·엔드포인트·필드명은 벤더 문서(개발자 센터)의 최신값과 맞춰야 함.
  이 저장소에서는 실 네트워크로 검증하지 않으므로, 실계좌 배포 전 자기 계정으로
  수동 확인 필수.
- `requests` 는 옵션 의존성. 없어도 임포트는 성공, 실행 시점에만 실패.
- IP 등록·앱키 발급 등 계좌 준비 절차는 코드가 아니라 개발자 센터 웹에서 진행.


## 12. 실패 사례에서 배운 리스크 강화 (v0.7)

"흑우스토리 — 자동매매 프로그램 실패 후기" 영상 참고. 실전 자동매매의 진짜
킬러 세 가지를 코드 게이트로 반영.

### 신설
| 항목 | 기본값 | 설명 |
|------|--------|------|
| `RiskLimits.max_trades_per_day` | 8 | 일일 신규 진입 상한. "5초 만에 -2% 손절 → 재진입 → 반복" 폭주 원천 차단 |
| `RiskLimits.chase_filter_pct` | 0.05 | 직전 봉이 이 값 이상 급등한 종목은 진입 금지. 0 = 비활성. "최고점에서 매수 → 곧바로 하락 → 로스컷" 사고 방지 |
| `metrics.CostAudit` | — | 회전율(turnover) · 총 매매대금 · 총 비용(수수료+세금) · 비용/자본 비율 |

`Backtester` 는 자동으로 진입 시 `last_bar_return` 을 계산해 chase filter 로
전달하고, 주문 성공 시 `risk.register_entry()` 를 호출해 일일 카운터를 올린다.
`BacktestReport.cost_audit` 필드로 회전율·비용 리포트가 자동 첨부되며, CLI 는
`[COST] fills=... turnover×... fees+tax=... (cost/capital=...%)` 형태로 출력.

### 왜 이 세 개인가
영상의 저자가 1년을 들여 만든 자동매매가 왜 수익을 못 냈는지 정리하면:
- **수수료·세금이 원금 대비 4배 회전율에서 계속 갉아먹음** → CostAudit 으로
  "이 전략이 진짜 수익성이 있는지, 아니면 수수료로 다 나가는지" 를 매 백테스트
  결과에 강제로 노출.
- **최고점 매수 후 5초 만에 로스컷** → chase filter 로 진입 자체를 차단.
- **하루 수백건 폭주** → 일일 거래 상한으로 회전율의 상한선을 두고, 상한 도달
  시 그 날은 자동으로 신규 진입 금지.

pytest 67 → 75 통과.


## 13. Cron 스케줄러 · EOD 청산 · 알림 채널 (v0.8)

"AI 알고리즘 기반 주식 자동매매 봇 구축" 영상의 아키텍처를 검증. 특정 플랫폼·
서비스(OpenClaw, Supabase, 라이너, 텔레그램) 종속 부분은 걷어내고 **파이썬
라이브러리로서 진짜 빠져 있던 세 가지**만 반영.

### 신설
- **`scheduler.py`** — 표준 5-필드 cron 파서 + `JobRegistry`. `*/5 9-15 * * 0-4`,
  `0 15 * * 0-4` 같은 표현식으로 잡을 코드에서 선언하고, `next_after(ts)`,
  `next_schedule()`, `crontab_lines()`, `run_forever()` 로 관리.
- **`ExecutionCfg.flat_at_time`** (예: `"15:00"`) + **`PaperBroker.flat_all()`** —
  데이트레이딩 규율(밤 사이 갭·이벤트 리스크 회피). `LiveTrader` 는 매 사이클
  시작에서 이 시각을 지났으면 자동으로 보유 전량 청산 (하루 1회 보장).
  청산은 `exit_reason="eod_flat"` 로 기록되어 tracker·metrics 에 반영.
- **`notify.py`** — `NotificationChannel` 추상 + `ConsoleChannel` /
  `NoopChannel` / `RecordingChannel` 기본 구현 + `Notifier` 다중 팬아웃.
  텔레그램/슬랙/이메일 등 벤더 채널은 사용자가 직접 붙일 수 있도록 표면만 제공.
  실패하는 채널이 있어도 다른 채널로는 계속 전달됨(매매에 알림이 영향 X).

### CLI 신규
```bash
# 실전 자동매매 표준 크론잡 5개를 crontab 라인으로 출력
python -m autotrader schedule --prefix "python -m autotrader run-job "
# 출력 예:
#   */5 9-15 * * 0-4 python -m autotrader run-job collect-5m   # 평일 장중 5분봉 수집
#   0 15   * * 0-4 python -m autotrader run-job eod-flat        # 15:00 EOD 일괄 청산
```

이 라인들을 `crontab -e` 로 등록하면 실전 데이트레이딩 워크플로가 완성된다.

### 반영하지 않은 것 (판단 결과)
- **팩터 가중치 자동 조정 피드백 루프** — 과최적화의 정확히 그 문제.
  v0.4 `StrategyRegistry` 의 수동 승인·90일 만료 방식이 더 안전.
- **특정 벤더 종속** (OpenClaw · Supabase · 라이너 · 텔레그램 · OpenDART) —
  아키텍처만 취하고 구현은 사용자가 자기 스택에 붙이도록 훅만 제공.
- **영상의 백테스트 성적 (승률 74.8%, 손익비 5.86)** — 우리 CostAudit + OOS
  원칙과 정합성 없어 반영 X.

pytest 75 → 89 통과.


## 14. 키움 REST 시세 자동 수집 (v0.9)

**"실제 시세를 사람이 CSV 로 매번 내려받는 워크플로"를 없앰.** 키움 REST API 를
DataProvider 로 감싸서, 코드 하나가 종목 목록·일봉·연속조회·CSV 캐시 누적까지
자동으로 처리한다. v0.8 의 Cron 잡 `collect-daily` 가 이 명령을 호출하도록
설계돼 있어, 매일 장 마감 후 자동 최신화까지 완성된다.

### 신설 · `autotrader/data/kiwoom.py` — `KiwoomProvider`
- `history(symbol, limit)` → **캐시 우선, 부족분만 API 호출, CSV 로 즉시 누적**
- `universe()` → 코스피(0) + 코스닥(10) 종목 마스터 자동 병합
- `refresh_all(symbols, limit)` → 유니버스 전체 최신화 (성공/실패 카운트 리턴)
- 연속조회 지원 (`cont-yn` / `next-key` 헤더 페이지네이션, 최대 30 페이지 안전 상한)
- 자격증명 비면 즉시 명확한 예외 (`DataError`) — 다른 어댑터와 동일 패턴
- CSV 캐시 포맷은 `CsvProvider` 와 완전 호환 → 오프라인 백테스트로 스위칭 가능

### CLI 신규 — `autotrader fetch`
```bash
# 특정 종목만 수집 (모의 서버 기본)
KIWOOM_APP_KEY=xxx KIWOOM_APP_SECRET=yyy \
  python -m autotrader fetch --cache ./data/kiwoom --symbol 005930 --symbol 000660

# 전체 유니버스 최신화 (실전 서버)
KIWOOM_APP_KEY=xxx KIWOOM_APP_SECRET=yyy \
  python -m autotrader fetch --cache ./data/kiwoom --real --limit 1000

# v0.8 의 collect-daily 크론잡과 결합
0 16 * * 0-4 python -m autotrader fetch --cache /var/data/kiwoom
```

### 데이터 품질 함정 (README 로도 명시)
1. **생존자 편향** — 현재 상장 종목만 조회하면 폐지된 과거 종목 누락.
   장기 백테스트는 KRX 과거 종목 유니버스로 보완 필요.
2. **분봉 제공 기간 제한** — 벤더 정책상 최근 N일치만. 매일 저장해 자체 시계열
   DB 를 축적하는 것이 정석.
3. **수정주가·액면분할·거래정지·신규상장** 처리 정책은 실 계정에서 검증 필수.
4. **연속조회 안 쓰면 최근 N건만 받고 끝남** → `KiwoomProvider` 는 자동 처리.

### 이제 완성되는 데이터 파이프라인
```
매일 16:00 (크론)
  → autotrader fetch (KiwoomProvider.refresh_all)
    → data/kiwoom/*.csv 자동 누적
  → autotrader backtest --csv data/kiwoom
    → OOS 성적 확인 → StrategyRegistry 승인 여부 결정
평일 09:30
  → autotrader paper --csv data/kiwoom --validated-only
    → 승인된 전략만 실전(모의) 진입
15:00
  → EOD 자동 청산
```

pytest 89 → 96 통과.


## 15. v1.0 · 실전 배포 준비 완료

세 가지를 마무리해 파이프라인이 끝에서 끝까지 실제로 돌아가게 만듦.

### ① 분봉 수집 · `KiwoomProvider.history_minutes(symbol, interval, limit)`
- 지원 간격: 1 / 3 / 5 / 10 / 15 / 30 / 45 / 60 분 (`ka10080`)
- 캐시 파일: `{symbol}_{interval}m.csv` — 일봉 캐시와 분리
- `refresh_minutes()` 로 유니버스 전체 최신화
- CLI: `autotrader fetch --minutes 5`

### ② 생존자 편향 방어 · `KrxUniverse` (autotrader/data/krx_universe.py)
- 과거 시점의 KRX 상장 종목 스냅샷을 JSONL 로 저장
- `symbols_on(date, market)` — 그 시점의 상장 종목
- `union_between(start, end, market)` — **폐지된 종목 포함 합집합** ← 핵심
- `refresh_from_pykrx(dates)` — pykrx 옵션 의존성으로 자동 수집

### ③ 실전 잡 디스패처 · `autotrader/jobs.py` + `run-job` CLI
v0.8 스케줄러의 crontab 라인이 실제로 뭘 하는지 마침내 구현.

| 잡 이름 | 언제 | 하는 일 |
|---------|------|---------|
| `morning-entry` | 09:30 | LiveTrader.cycle() 한 번 실행 (스크리너→앙상블→Risk→주문) |
| `eod-flat` | 15:00 | 보유 전량 일괄 청산 (밤 리스크 회피) |
| `collect-daily` | 15:45 | KiwoomProvider.refresh_all() — 일봉 최신화 |
| `collect-5m` | 장중 5분마다 | KiwoomProvider.refresh_minutes() — 분봉 최신화 |
| `post-analysis` | 15:30 | 오늘 승인 전략 · 정확도 리포트 |

**자격증명 없어도 안전**: 키움 자격증명이 없으면 자동으로 CsvProvider 로 폴백,
분봉 수집처럼 키움만 할 수 있는 잡은 "스킵" 알림만 남기고 종료.

### 이제 완성된 crontab 워크플로
```bash
# 1. 표준 잡 5개를 crontab 라인으로 출력
python -m autotrader schedule --prefix "python -m autotrader run-job "

# 2. 편집기 열어서 그대로 붙여넣기 (Linux/Mac)
crontab -e

# 3. 등록된 잡 확인
crontab -l

# 완료. 이제 컴퓨터가 켜져 있는 한 자동으로:
#   평일 09:30 → morning-entry (진입)
#   장중 5분  → collect-5m (분봉 축적)
#   평일 15:00 → eod-flat (일괄 청산)
#   평일 15:30 → post-analysis (리포트)
#   평일 15:45 → collect-daily (일봉 최신화)
```

Windows 사용자는 crontab 대신 **작업 스케줄러(Task Scheduler)** 로 같은 시각에
`python -m autotrader run-job <name>` 을 등록하면 된다.

pytest 96 → 107 통과.

---

## 🎉 v1.0 마일스톤 — 로드맵 완성

| 버전 | 반영 |
|------|------|
| v0.1 | 골격 (모델·지표·데이터·전략·앙상블·리스크·백테스트) |
| v0.2 | 실운영 후기 개선판 (휴장일·쿨다운·트래커·하드스톱) |
| v0.3 | NXT 세션 + SourceReconciler |
| v0.4 | Screener 3-티어 + StrategyRegistry (validated-only) |
| v0.5 | 실시간 스트림 계층 (WebSocket 스켈레톤) |
| v0.6 | 키움 REST 브로커 (주문) |
| v0.7 | 실패 사례에서 배운 리스크 강화 (CostAudit · chase filter · 일 상한) |
| v0.8 | Cron 스케줄러 + EOD 청산 + 알림 채널 |
| v0.9 | 키움 REST 시세 자동 수집 (일봉) |
| **v1.0** | **분봉 + KRX 유니버스(생존자편향) + run-job (실전 크론 액션)** |

이제 크론 등록만 하면 위 파이프라인이 매일 자동으로 돕니다. 다음은 사용자
계정으로 KIS/키움 자격증명 넣고 모의계좌에서 2~4주 검증 → 소액 실전 순서.
