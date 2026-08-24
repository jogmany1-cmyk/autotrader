#!/usr/bin/env bash
# 커밋·푸시 전에 돌리는 단일 검증 진입점.
#
#   ./scripts/verify.sh                    # 전체 검증
#   CACHE=data/kospi ./scripts/verify.sh   # 다른 캐시로 무결성 검사
#   PYTHON=python3.11 ./scripts/verify.sh  # 파이썬 지정
#
# 실패를 만나도 멈추지 않고 끝까지 돌린 뒤 한 번에 요약한다. 첫 실패에서
# 중단하면 "고치고 다시 돌리고" 를 실패 개수만큼 반복하게 되기 때문이다.
# 하나라도 실패하면 종료코드 1 — CI 나 pre-push 훅에 그대로 물릴 수 있다.
#
# 배열 전개 대신 카운터와 문자열만 쓴다. macOS 기본 bash 3.2 는 `set -u` 아래
# 빈 배열 전개에서 죽기 때문에, 어느 bash 에서나 같게 도는 쪽을 택했다.
set -o pipefail
cd "$(dirname "$0")/.." || exit 1

PY=${PYTHON:-python}
CACHE=${CACHE:-data/kiwoom}

N_FAIL=0
N_SKIP=0
SUMMARY=""

record() {         # record <기호> <색> <이름>
  SUMMARY="${SUMMARY}  $(printf '\033[%sm%s\033[0m' "$2" "$1") $3
"
}

step() {           # step <이름> <명령...>
  name="$1"; shift
  printf '\n\033[1m▶ %s\033[0m\n' "$name"
  if "$@"; then
    record "✓" 32 "$name"
  else
    N_FAIL=$((N_FAIL + 1))
    printf '\033[31m  ✗ %s 실패\033[0m\n' "$name"
    record "✗" 31 "$name"
  fi
}

skip() {           # skip <이름> <사유>
  N_SKIP=$((N_SKIP + 1))
  printf '\n\033[1m▶ %s\033[0m\n  – 건너뜀: %s\n' "$1" "$2"
  record "–" 33 "$1 (건너뜀)"
}

# 1. 단위 테스트 — 회귀 방어선
if $PY -c "import pytest" 2>/dev/null; then
  step "단위 테스트 (pytest)" $PY -m pytest -q
else
  skip "단위 테스트 (pytest)" "pytest 미설치 — pip install pytest"
fi

# 2. stdlib 전용 제약 — 개발 머신에 requests/yaml 이 깔려 있어도 유효한 검사
step "stdlib 전용 제약" $PY scripts/check_stdlib_only.py

# 3. 합성 데이터 스모크 — 파이프라인이 끝에서 끝까지 도는지
step "스모크: 스크리너"   $PY -m autotrader screen --top 3
step "스모크: 백테스트"   $PY -m autotrader --threshold 0.45 backtest
step "스모크: 페이퍼매매" $PY -m autotrader --threshold 0.45 --votes 1 paper --cycles 2 --dry-run

# 4. 데이터 무결성 — 실데이터가 있을 때만. 승격 경로 2단계의 게이트.
if [ -d "$CACHE" ]; then
  step "데이터 무결성 ($CACHE)" $PY -m autotrader --csv "$CACHE" validate-data
else
  skip "데이터 무결성" "$CACHE 없음 (아직 시세를 수집하지 않음)"
fi

# ---- 요약 --------------------------------------------------------------
printf '\n\033[1m== 검증 요약 ==\033[0m\n'
printf '%s' "$SUMMARY"

if [ "$N_FAIL" -gt 0 ]; then
  printf '\n\033[31m%d개 실패 — 푸시하지 마세요.\033[0m\n' "$N_FAIL"
  exit 1
fi
if [ "$N_SKIP" -gt 0 ]; then
  printf '\n\033[32m전부 통과.\033[0m \033[33m(%d개 건너뜀)\033[0m\n' "$N_SKIP"
else
  printf '\n\033[32m전부 통과.\033[0m\n'
fi
exit 0
