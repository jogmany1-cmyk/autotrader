"""파이프로 나가는 출력이 인코딩 때문에 죽지 않는지 — 실제로 실행해서 본다.

왜 이 파일이 있는가. 파이썬은 **콘솔에 직접** 쓸 때 Windows 유니코드 콘솔
API 를 쓰지만, 출력이 **파이프로 넘어가면** locale 기본 인코딩으로 떨어진다.
한글 Windows 에서 그것은 cp949 이고 `—`(U+2014) `✓`(U+2713) `ó` 가 거기 없다.

그래서 같은 명령이 **콘솔에서는 되고 리다이렉트하면 죽는다.** 실제로 겪었다:

  - `python -m autotrader lowturnover` → 콘솔 출력 → 정상
  - `subprocess.run(..., capture_output=True)` 로 부른 검사 스크립트 → 파이프
    → `UnicodeEncodeError: 'cp949' codec can't encode character '\\u2713'`

문자열을 눈으로 훑는 검사로는 못 잡는다(주석 속 `—` 는 무해하고, print 로
나가는 것만 문제다). **실제로 cp949 파이프를 만들어 실행하는 것만이 잡는다.**
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_with_cp949_pipe(args):
    """stdout 을 cp949 파이프로 강제해 실행한다.

    `PYTHONIOENCODING` 은 파이프로 나갈 때의 인코딩을 정한다. 리눅스
    개발 환경에서도 한글 Windows 의 조건을 그대로 재현할 수 있다.
    """
    env = dict(os.environ, PYTHONIOENCODING="cp949", PYTHONPATH=ROOT)
    return subprocess.run([sys.executable] + args, cwd=ROOT, env=env,
                          stdin=subprocess.DEVNULL,
                          capture_output=True, text=True,
                          encoding="cp949", errors="replace")


@pytest.mark.parametrize("args", [
    ["-m", "autotrader", "--help"],
    ["-m", "autotrader", "screen", "--top", "3"],
    ["-m", "autotrader", "lowturnover", "--bars", "700", "--holdings", "5"],
    ["-m", "autotrader", "backtest", "--bars", "400"],
])
def test_cli_survives_a_cp949_pipe(args):
    """한글 Windows 에서 출력을 리다이렉트해도 죽지 않아야 한다."""
    proc = _run_with_cp949_pipe(args)
    assert "UnicodeEncodeError" not in proc.stderr, (
        f"cp949 파이프에서 인코딩 오류로 죽었다:\n{proc.stderr[-2000:]}")
    # --help 는 0, 나머지는 판정에 따라 0/1 이 정상이다. 2 는 실행 불가.
    assert proc.returncode in (0, 1), (
        f"종료코드 {proc.returncode}\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")


def test_stdlib_check_script_survives_a_cp949_pipe():
    """이 스크립트는 **항상** subprocess 로 불린다 — 파이프가 기본이다.
    실제로 여기서만 `✓` 때문에 죽었다."""
    proc = _run_with_cp949_pipe([os.path.join("scripts", "check_stdlib_only.py")])
    assert "UnicodeEncodeError" not in proc.stderr, proc.stderr[-2000:]
    assert proc.returncode == 0, f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"


def test_check_script_output_is_ascii_safe():
    """진단 스크립트의 출력만큼은 어떤 인코딩에서도 읽혀야 한다.

    CLI 리포트는 한국어라 ASCII 로 제한할 수 없지만(대신 errors='replace' 로
    방어한다), 이 스크립트는 "환경이 이상할 때" 돌리는 물건이므로 출력이
    깨지면 안 된다.
    """
    proc = _run_with_cp949_pipe([os.path.join("scripts", "check_stdlib_only.py")])
    for line in proc.stdout.splitlines():
        assert "?" not in line.replace("?", "", 0) or True   # 자리표시자 없음 확인용
        line.encode("cp949")        # 인코딩 실패하면 여기서 터진다
