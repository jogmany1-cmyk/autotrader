# 리눅스 + cron 으로 새 데이터 쌓고 모의투자 돌리기

CLAUDE.md 에 "cron 이 없다 — 이 갭은 아직 메워져 있지 않다" 로 적혀 있던
항목의 해소 문서다.

## 왜 리눅스인가

Windows 작업 스케줄러는 `autotrader schedule` 이 뱉는 crontab 라인을 소비하지
못한다. 형식이 다르고, 변환기를 만들어도 그 변환기가 또 검증 대상이 된다.

키움 REST API 는 크로스플랫폼이다(다른 증권사의 OCX 방식과 달리 Windows 전용이
아니다). 런타임 코드도 Windows-clean 이지만 POSIX 전용 의존이 없다는 뜻이므로
리눅스에서도 그대로 돈다. 그래서 **안 쓰는 노트북에 리눅스를 올리는 것**이
스케줄러 갭을 메우는 가장 짧은 길이다.

Windows 에서 계속 쓰고 싶다면 이 문서는 WSL2 에서도 그대로 적용된다. 다만
WSL2 는 기본적으로 cron 데몬이 안 뜨므로 `sudo service cron start` 를 부팅마다
해야 하고, 노트북이 잠들면 안 돈다. 전용 머신을 권한다.

---

## 1. 설치

```bash
git clone https://github.com/jogmany1-cmyk/autotrader.git
cd autotrader
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
# 런타임은 표준 라이브러리만으로 돈다. pytest 는 검증용이라 선택이다.
.venv/bin/python -m pip install pytest

# 설치가 성립하는지부터 확인한다
.venv/bin/python -m pytest -q
```

## 2. 자격증명

크론은 로그인 셸이 아니라 `~/.bashrc` 를 읽지 않는다. `export` 로 넣은 값은
크론에서 보이지 않는다. 파일로 둬야 한다.

```bash
cp .env.example .env
chmod 600 .env        # API 키가 들어간다. 이 단계를 건너뛰지 않는다.
vi .env               # 값을 채운다
```

`.env` 는 `.gitignore` 에 있다. `scripts/cron-run.sh` 가 실행 직전에 읽고,
권한이 느슨하면 경고한다.

## 3. crontab 등록

```bash
# 뽑아 보고
.venv/bin/python -m autotrader schedule --prefix "$PWD/scripts/cron-run.sh "

# 설치 전에 먼저 점검한다
.venv/bin/python -m autotrader schedule --prefix "$PWD/scripts/cron-run.sh " > /tmp/at.cron
.venv/bin/python -m autotrader schedule --check --crontab-file /tmp/at.cron

# 문제 없으면 설치
crontab /tmp/at.cron

# **등록됐다고 믿지 말고 확인한다**
crontab -l
.venv/bin/python -m autotrader schedule --check
```

마지막 두 줄을 건너뛰지 않는다. 설치 명령이 성공해도 등록이 안 되는 경우가
있고, 크론은 그것을 알려주지 않는다.

### 1단계 — `--profile collect` (기본)

지금 레지스트리에 승인된 전략이 **하나도 없다** (전부 폐기했다). 그래서
모의매매를 거는 것이 아직 불가능하다. 1단계는 전략과 무관하게 굴린다.

| 시각 | 잡 | 하는 일 |
|---|---|---|
| 15:45 | `collect-daily` | 장 마감 후 일봉 수집 |
| 16:00 | `validate-data` | 무결성 게이트 — 실패하면 exit 1 |
| 16:15 | `data-progress` | 이 fold 밖의 새 데이터가 며칠 쌓였나 |

전략이 정해지기 전에 시작하는 이유는 둘이다. 새 구간 데이터는 어차피
필요하고, 크론·자격증명·경로가 실제로 도는지 **먼저** 드러난다.

### 2단계 — `--profile paper`

승인된 전략이 생긴 뒤에 바꿔 단다.

| 시각 | 잡 | 하는 일 |
|---|---|---|
| 09:05 | `paper-session` | 전일 종가로 판단 → 당일 시가 체결 |
| 15:45 | `collect-daily` | 장 마감 후 일봉 수집 |
| 16:00 | `validate-data` | 무결성 게이트 — 실패하면 exit 1 |
| 16:15 | `session-report` | 60거래일까지 며칠 남았는지 + 누적 성과 |

`paper-session` 을 장 마감 뒤로 옮기면 안 된다. `LiveTrader.cycle()` 은 휴장
중이면 즉시 반환하므로 **60일 내내 아무 일도 일어나지 않는다.** 테스트가 이
시각을 장중으로 고정한다.

`--profile daytrade` 도 있지만 기본이 아니다. 매일 전량청산은 연 회전율
343배라 왕복비용만 연 57~125% 다. 근거는 `docs/STRATEGY-RESET-2026-08-26.md`.

## 4. 새 데이터의 경계 — 이것이 1단계의 핵심이다

`collect-daily` 는 500봉을 받아 온다. 첫날부터 캐시에 **2년치 과거가 들어
있고, 그것은 이미 다섯 번 참조한 2016~2026 fold 다.** 그냥 모아 두고 나중에
전 구간으로 백테스트하면 여섯 번째 참조가 된다.

그래서 `data-progress` 가 첫 실행에서 오늘 날짜를 파일에 못 박는다.

```
runs/fresh-data-since.json
{"since": "2026-08-27", "note": "이 날짜보다 뒤의 거래일만 새 데이터로 센다."}
```

이후 그보다 **뒤의 거래일만** 센다. 첫날 보고는 `새 데이터 0/60거래일` 이고,
그것이 맞다. 캐시에 500봉이 있어도 새 데이터는 0일이다.

**이 파일을 손으로 고치지 않는다.** 앞당기는 순간 폐기한 fold 가 슬그머니 다시
들어오고, 그때부터는 무엇을 측정하고 있는지 알 수 없게 된다. 회귀 테스트가
경계가 움직이지 않는 것을 고정한다.

## 5. 2단계로 넘어갈 때

`paper-session` 은 레지스트리가 없거나 승인된 전략이 없으면 **실패로 끝난다.**
조용히 0 으로 끝나면 60일 뒤 빈 계좌를 보고 "전략이 나빴다" 고 오진하게 된다 —
실제로는 시작조차 안 한 것이다.

```bash
.venv/bin/python -m autotrader validate --registry runs/registry.json

# 승인된 전략이 있으면 프로파일을 바꿔 단다
.venv/bin/python -m autotrader schedule --profile paper \
    --prefix "$PWD/scripts/cron-run.sh " | crontab -
.venv/bin/python -m autotrader schedule --check --profile paper
```

폐기한 일봉 5종은 되살리지 않는다. 새로 시험할 전략을 레지스트리에 올리고
승인 기준을 통과시킨 다음에 시작한다.

`--check` 는 프로파일을 바꾼 뒤 **옛 라인이 남아 있으면 경고한다.** 1단계
잡이 계속 돌면서 같은 파일을 건드리는 상태를 그렇게 잡는다.

---

## 6. 매일 무엇이 쌓이는가

전부 `runs/` 아래에 생긴다. **`runs/` 는 `.gitignore` 에 있으므로 커밋되지
않는다.** 60일치가 이 폴더에만 있다 — 머신이 죽으면 같이 사라진다. 주기적으로
다른 곳에 복사한다.

```
runs/
  account.json        현금·보유종목·청산기록·체결기록   ← 이게 없으면 매일 1일차
  state.json          손절선·일일카운터·쿨다운
  orders.jsonl        미결 주문 장부
  sessions.jsonl      세션 일지 (하루 한 줄, 60거래일을 세는 근거)
  fresh-data-since.json  새 데이터 경계 — 손대지 않는다 (§4)
  status/<잡>.json    마지막 실행 시각·종료코드   ← --check 가 읽는다
  logs/<잡>.log       실행 로그
  locks/<잡>.lock     중복 실행 방지
```

`account.json` 이 이 구조의 핵심이다. `PaperBroker` 는 브로커가 우리 프로세스
안에 있어서 물어볼 외부 진실이 없다 — 이 파일이 곧 잔고다. 지우면 그날부터
다시 1일차가 된다.

## 7. 진척 확인

```bash
# 1단계: 새 데이터가 며칠 쌓였나
.venv/bin/python -m autotrader run-job data-progress --runs runs

# 2단계: 모의투자가 며칠째인가
.venv/bin/python -m autotrader run-job session-report --runs runs

# 크론이 실제로 돌고 있는가
.venv/bin/python -m autotrader schedule --check
```

`--check` 의 판정 기준:

| 상태 | 판정 | 이유 |
|---|---|---|
| 등록됨 · 아직 미실행 | 경고 | 설치 직후 빨간불이 뜨면 게이트를 무시하는 법부터 배운다 |
| 마지막 실행 3일 전 | 통과 | 금요일에 돌고 월요일에 점검하면 3일이다 |
| 마지막 실행 실패 | **FAIL** | |
| 4일 이상 정지 | **FAIL** | |
| crontab 에 없음 / 시각 불일치 | **FAIL** | |
| 프로파일 밖의 우리 잡이 남음 | 경고 | 옛 잡이 같은 계좌 파일을 계속 건드린다 |

---

## 8. 안 될 때

크론 실패는 조용하다. 순서대로 좁힌다.

**(가) 크론 데몬이 도는가**

```bash
systemctl status cron      # 데비안/우분투
systemctl status crond     # RHEL 계열
```

**(나) 크론이 잡을 띄우기는 했는가**

```bash
grep CRON /var/log/syslog | tail -20
journalctl -u cron --since today | tail -20
```

여기 아무것도 없으면 등록이 안 된 것이다. `crontab -l` 로 확인한다.

**(다) 띄웠는데 실패했는가**

```bash
cat runs/status/paper-session.json     # 종료코드
tail -50 runs/logs/paper-session.log   # 무엇이 났는지
```

**(라) 손으로 돌려 본다** — 크론 없이 같은 명령을 그대로

```bash
./scripts/cron-run.sh paper-session; echo "종료코드=$?"
```

손으로는 되는데 크론에서만 안 되면 거의 항상 환경 문제다: `.env` 경로,
파이썬 경로, 상대경로. 래퍼가 이 셋을 다 다루므로, 래퍼를 안 거치고 크론에
`python -m autotrader ...` 를 직접 넣은 것은 아닌지 확인한다.

**(마) 시각이 맞는가**

```bash
timedatectl        # Asia/Seoul 이어야 한다
```

크론은 시스템 시간대로 돈다. UTC 로 두면 09:05 잡이 한국시간 18:05 에 돈다 —
장이 닫혀 있으므로 매일 아무 일도 없이 성공한다. `--check` 는 통과하는데
`sessions.jsonl` 의 `market_open` 이 계속 false 인 것이 이 증상이다.

---

## 9. 이 문서가 해결하지 않는 것

- **수익성.** 스케줄러는 측정을 자동화할 뿐이다. 거래당 33~103bp 의 왕복비용
  장벽은 그대로 있다.
- **실전 매매.** 승격 경로상 실전은 사람의 명시적 승인이 필요하고, AI 는 그
  승인을 내릴 수 없다. `KIWOOM_MODE=paper` 를 유지한다.
- **머신이 꺼져 있는 동안.** 크론은 밀린 잡을 나중에 몰아 돌리지 않는다.
  노트북이 잠들어 있었으면 그날은 비는 게 맞고, 세션 일지도 그날을 세지
  않는다. 60거래일을 채우려면 그만큼 더 걸린다.
- **60일 뒤의 판정.** 일지가 60거래일을 채웠다고 알려주지만, 그 결과가
  승격 기준을 넘는지는 별도로 판정해야 한다.
