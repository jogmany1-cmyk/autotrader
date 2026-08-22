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
