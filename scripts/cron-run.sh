#!/usr/bin/env bash
# autotrader 잡 하나를 크론에서 안전하게 실행한다.
#
# 왜 파이썬을 직접 부르지 않는가 — 크론은 다음을 주지 않는다:
#   · PATH 가 /usr/bin:/bin 뿐이라 venv 파이썬을 못 찾는다
#   · 로그인 셸이 아니라 ~/.bashrc 를 안 읽는다. KIWOOM_APP_KEY 가 없다
#   · 작업 디렉터리가 $HOME 이라 상대경로(./data/kiwoom)가 딴 데를 가리킨다
#   · stdout/stderr 를 메일로 보내려다 MTA 가 없으면 통째로 버린다
#
# 그리고 크론은 실패를 알려주지 않는다. 종료코드를 삼키면 60일 동안 아무
# 일도 안 일어났는데 아무도 모르는 상태가 된다. 이 스크립트가 그 틈을 메운다.
#
# 사용법:
#   scripts/cron-run.sh <잡이름> [추가인자...]
#
# 환경변수로 조절:
#   AUTOTRADER_ENV     자격증명 파일 경로 (기본 <저장소>/.env)
#   AUTOTRADER_PYTHON  파이썬 실행파일  (기본 <저장소>/.venv/bin/python → python3)
#   AUTOTRADER_CACHE   시세 캐시        (기본 <저장소>/data/kiwoom)
#   AUTOTRADER_RUNS    세션 산출물      (기본 <저장소>/runs)
#   AUTOTRADER_REGISTRY 레지스트리 JSON (기본 <저장소>/runs/registry.json)

set -euo pipefail

JOB="${1:-}"
if [ -z "$JOB" ]; then
    echo "사용법: $(basename "$0") <잡이름> [추가인자...]" >&2
    exit 64
fi
shift

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
cd "$REPO"

# --- 자격증명 -------------------------------------------------------------
# cron 은 ~/.bashrc 를 안 읽으므로 여기서 명시적으로 읽는다.
ENV_FILE="${AUTOTRADER_ENV:-$REPO/.env}"
if [ -f "$ENV_FILE" ]; then
    # 파일에 API 키가 들어간다. 남이 읽을 수 있으면 경고한다 — 조용히 넘기면
    # 키가 새는 것을 아무도 모른다.
    perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%A' "$ENV_FILE" 2>/dev/null || echo '')"
    case "$perms" in
        ''|*00) ;;
        *) echo "[warn] $ENV_FILE 권한이 $perms 입니다. chmod 600 을 권합니다." >&2 ;;
    esac
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# --- 경로 -----------------------------------------------------------------
PYTHON="${AUTOTRADER_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then
        PYTHON="$REPO/.venv/bin/python"
    else
        PYTHON="$(command -v python3 || true)"
    fi
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    echo "[error] 파이썬을 찾을 수 없습니다. AUTOTRADER_PYTHON 을 지정하세요." >&2
    exit 69
fi

CACHE="${AUTOTRADER_CACHE:-$REPO/data/kiwoom}"
RUNS="${AUTOTRADER_RUNS:-$REPO/runs}"
REGISTRY="${AUTOTRADER_REGISTRY:-$RUNS/registry.json}"
LOG_DIR="$RUNS/logs"
STATUS_DIR="$RUNS/status"
LOCK_DIR="$RUNS/locks"
mkdir -p "$LOG_DIR" "$STATUS_DIR" "$LOCK_DIR"

LOG="$LOG_DIR/$JOB.log"
STATUS="$STATUS_DIR/$JOB.json"
LOCK="$LOCK_DIR/$JOB.lock"

# --- 중복 실행 방지 -------------------------------------------------------
# 앞 실행이 안 끝났는데 다음이 시작되면 같은 계좌 파일에 둘이 쓴다. 나중에
# 저장하는 쪽이 이기고, 그 사이의 체결은 흔적 없이 사라진다.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK"
    if ! flock -n 9; then
        echo "[$(date -Is)] $JOB: 이전 실행이 아직 돌고 있습니다 — 이번은 건너뜁니다" >>"$LOG"
        exit 0
    fi
else
    echo "[warn] flock 이 없어 중복 실행을 막지 못합니다." >&2
fi

# --- 실행 -----------------------------------------------------------------
ARGS=(run-job "$JOB" --cache "$CACHE" --runs "$RUNS")
# 레지스트리는 있을 때만 넘긴다. 없는 경로를 넘기면 빈 레지스트리로 읽혀서
# "승인 전략 없음" 이 아니라 "레지스트리 있음" 으로 잘못 판정된다.
if [ -f "$REGISTRY" ]; then
    ARGS+=(--registry "$REGISTRY")
fi

started_at="$(date -Is)"
start_s="$(date +%s)"
echo "[$started_at] $JOB 시작 (python=$PYTHON cache=$CACHE runs=$RUNS)" >>"$LOG"

set +e
"$PYTHON" -m autotrader "${ARGS[@]}" "$@" >>"$LOG" 2>&1
code=$?
set -e

elapsed=$(( $(date +%s) - start_s ))
echo "[$(date -Is)] $JOB 종료 code=$code ${elapsed}s" >>"$LOG"

# --- 상태 기록 ------------------------------------------------------------
# `autotrader schedule --check` 가 이 파일을 읽는다. 로그를 파싱하는 것보다
# 낫다 — 로그 형식이 바뀌어도 검증이 안 깨진다.
tmp="$STATUS.tmp"
cat >"$tmp" <<JSON
{
 "job": "$JOB",
 "started_at": "$started_at",
 "finished_at": "$(date -Is)",
 "exit_code": $code,
 "elapsed_seconds": $elapsed,
 "log": "$LOG"
}
JSON
mv "$tmp" "$STATUS"

# 크론에게 종료코드를 그대로 넘긴다. 여기서 0 으로 뭉개면 MAILTO 알림도,
# --check 의 판정도 무의미해진다.
exit $code
