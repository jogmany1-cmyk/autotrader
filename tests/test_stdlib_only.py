"""런타임 코어의 stdlib 전용 제약을 pytest 에서도 강제한다.

이 제약은 CLAUDE.md 에 적혀만 있고 어디서도 검사되지 않았다. 개발 머신이나 CI
컨테이너에 requests/yaml 이 깔려 있으면 (이 저장소의 개발 환경이 실제로 그렇다)
모듈 상단에 import pandas 를 넣어도 테스트가 전부 통과한다.

검사는 별도 프로세스로 돌린다. 임포트 차단 훅을 현재 인터프리터에 꽂으면 이미
sys.modules 에 올라온 autotrader 모듈들 때문에 검사가 무의미해지기 때문이다.
"""
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_stdlib_only.py"


def test_runtime_core_works_without_third_party_packages():
    assert SCRIPT.exists(), f"검사 스크립트가 없습니다: {SCRIPT}"
    # stdin 을 반드시 명시한다. `capture_output=True` 는 stdout/stderr 만
    # 파이프로 잡고 **stdin 은 부모에서 상속**하는데, pytest 캡처 아래의
    # Windows 에서는 부모 stdin 핸들이 유효하지 않을 수 있다. 그러면
    # subprocess 가 핸들을 복제하다 죽는다:
    #
    #   OSError: [WinError 6] 핸들이 잘못되었습니다
    #     ... _make_inheritable → _winapi.DuplicateHandle
    #
    # 실제로 Windows + Python 3.14 에서 이 테스트만 실패했다. 검사 스크립트는
    # 입력을 읽지 않으므로 DEVNULL 로 끊는 것이 맞다.
    proc = subprocess.run([sys.executable, str(SCRIPT)],
                          stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        "런타임 코어가 표준 라이브러리만으로 동작하지 않습니다.\n"
        f"{proc.stdout}\n{proc.stderr}")
