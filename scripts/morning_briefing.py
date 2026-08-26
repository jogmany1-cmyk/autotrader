"""Windows 작업 스케줄러에서 부를 수 있는 아침 요약 실행 파일."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/morning_briefing.py`로 실행하면 Python은 scripts 폴더만
# 모듈 경로에 넣는다. 저장소 루트를 명시해 설치 없이도 autotrader를 찾는다.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autotrader.intelligence.runner import run_morning_briefing


def main() -> int:
    parser = argparse.ArgumentParser(description="아침 시장정보 그림자 보고서")
    parser.add_argument("--config", required=True, help="관심종목 JSON 경로")
    parser.add_argument("--output-dir", default="data/intelligence")
    parser.add_argument("--no-kakao", action="store_true",
                        help="카카오 전송 없이 콘솔·JSONL만 검증")
    args = parser.parse_args()
    run_morning_briefing(
        args.config, output_dir=args.output_dir,
        send_kakao=not args.no_kakao)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
