#!/bin/bash
# harness-init 세션 시작 훅
#
# 목적: Claude Code 세션이 열릴 때마다, 이 프로젝트에 CLAUDE.md/AGENTS.md
# (안내 문서)가 있는지 조용히 확인한다. 없으면 "harness-init 스킬로 만들어드릴까요?"라고
# 먼저 제안하도록 Claude에게 안내 문구를 넘겨준다. 있으면 아무것도 하지 않는다
# (매번 알림이 뜨면 오히려 방해가 되므로).
#
# 이 스크립트는 안전하게 여러 번 실행돼도 문제없다 (읽기만 하고 아무것도 바꾸지 않음).

set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" 2>/dev/null || exit 0

# stdin으로 들어오는 세션 정보는 이 훅에서는 쓰지 않지만, 훅 규약상 읽어서 비워준다.
cat > /dev/null 2>&1 || true

# 이미 안내 문서가 있으면 조용히 종료 (아무 출력도 하지 않음 = 아무 일도 안 일어남)
if [ -f "CLAUDE.md" ] || [ -f "AGENTS.md" ]; then
  exit 0
fi

# 없으면, Claude가 세션 시작 시점에 참고할 안내 문구를 additionalContext로 넘긴다.
CONTEXT_MSG='이 프로젝트 루트에 CLAUDE.md/AGENTS.md(프로젝트 안내 문서)가 없습니다. 사용자가 실질적인 작업을 요청하면, 먼저 harness-init 스킬을 실행해서 이 프로젝트 성격에 맞는 CLAUDE.md/docs/scripts/verify.sh 세트를 만들지 제안하세요. 이미 스킬이 설치돼 있지 않다면 harness-init 스킬을 참고용으로 안내하고, 사용자가 원치 않으면 그냥 평소대로 작업을 진행하세요. 세션 시작 직후 사용자에게 먼저 말을 걸어 방해하지 말고, 사용자가 첫 요청을 했을 때 자연스럽게 첫 응답에 이 제안을 포함하세요.'

python3 - "$CONTEXT_MSG" << 'PYEOF'
import json, sys
msg = sys.argv[1]
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": msg
    }
}, ensure_ascii=False))
PYEOF
