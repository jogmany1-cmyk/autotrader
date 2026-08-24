"""런타임 코어가 정말 표준 라이브러리만으로 도는지 강제하는 검사.

CLAUDE.md 의 제약:

    Keep runtime code stdlib-only unless explicitly needed — the core (models,
    indicators, strategies, backtest, risk, portfolio, live, streaming) must
    work without numpy/pandas/requests because tests rely on that.

문제는 이 제약이 **아무 데서도 검사되지 않았다** 는 것이다. 개발 머신이나 CI
컨테이너에 `requests` 나 `yaml` 이 깔려 있으면 (실제로 흔하다) 테스트가 전부
통과해도 제약이 지켜졌다는 증거가 되지 못한다. 누군가 `import pandas` 를 모듈
상단에 하나 넣어도 그 환경에서는 아무 일도 일어나지 않는다.

그래서 "설치돼 있지 않은지" 를 보지 않는다. 대신 임포트 자체를 **차단한 상태로**
전 모듈을 임포트하고 백테스트를 한 바퀴 돌린다. 금지된 패키지가 깔려 있는
머신에서도 똑같이 유효한 검사가 된다.

선택적 의존성(`requests` · `websockets` · `yaml`)은 함수 안에서 늦게 임포트하는
것이 이 저장소의 규칙이다. 그래야 어댑터 모듈도 임포트는 성공하고, 실제로 호출할
때만 실패한다. 이 검사는 그 규칙도 함께 지킨다.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import sys

# scripts/ 에서 실행되므로 sys.path[0] 이 scripts/ 다. 저장소 루트를 넣어 줘야
# autotrader 패키지를 (설치 없이도) 임포트할 수 있다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 런타임 코어가 절대 의존하면 안 되는 패키지들.
BLOCKED = ("numpy", "pandas", "requests", "websockets", "yaml", "scipy",
           "sklearn", "matplotlib", "pykrx")

# `python -m autotrader` 진입점이라 임포트하는 순간 argparse 가 돌면서 SystemExit
# 한다. 임포트 검사 대상에서 뺀다 (cli.py 는 그대로 검사되므로 손실 없음).
SKIP_MODULES = ("autotrader.__main__",)


class _ImportBlocker:
    """지정한 최상위 패키지의 임포트를 ImportError 로 막는 meta_path 훅."""

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in self.names:
            raise ImportError(
                f"'{fullname}' 는 stdlib-only 검사에서 차단되었습니다 "
                f"(런타임 코어는 이 패키지 없이 동작해야 합니다)")
        return None


def _purge(names):
    for mod in list(sys.modules):
        if mod.split(".")[0] in names:
            del sys.modules[mod]


def check_imports() -> list:
    """autotrader 패키지의 모든 모듈이 차단 상태에서 임포트되는지."""
    try:
        import autotrader
    except Exception as exc:                           # noqa: BLE001
        # __init__.py 가 코어 모듈들을 끌어오므로 위반이 여기서 먼저 터진다.
        # 날 traceback 대신 다른 실패와 같은 형식으로 보고한다.
        return [("autotrader/__init__.py", f"{type(exc).__name__}: {exc}")]

    failures = []
    for info in pkgutil.walk_packages(autotrader.__path__, prefix="autotrader."):
        if info.name in SKIP_MODULES:
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:                       # noqa: BLE001
            failures.append((info.name, f"{type(exc).__name__}: {exc}"))
    return failures


def check_backtest_runs() -> list:
    """임포트만이 아니라 실제 백테스트 한 바퀴가 도는지."""
    try:
        from autotrader.backtest import Backtester
        from autotrader.config import Config
        from autotrader.data import SyntheticProvider

        provider = SyntheticProvider(symbols=("AAA", "BBB", "CCC"), n=260)
        cfg = Config.default()
        cfg.universe.symbols = provider.universe()
        cfg.universe.min_price = 0
        cfg.universe.min_avg_dollar_vol = 0
        report = Backtester(provider, cfg, ensemble_threshold=0.45,
                            ensemble_min_votes=1, trail_pct=0.05).run()
    except Exception as exc:                           # noqa: BLE001
        return [("backtest", f"{type(exc).__name__}: {exc}")]
    if not report.equity_curve:
        return [("backtest", "자산곡선이 비어 있음 — 백테스트가 돌지 않았다")]
    return []


def main() -> int:
    _purge(BLOCKED)
    sys.meta_path.insert(0, _ImportBlocker(BLOCKED))

    print(f"차단한 패키지: {', '.join(BLOCKED)}")
    # 임포트가 깨져 있으면 백테스트 검사는 같은 원인으로 또 터질 뿐이다.
    failures = check_imports() or check_backtest_runs()
    if failures:
        print(f"\n\033[31m✗ stdlib-only 위반 {len(failures)}건\033[0m")
        for name, why in failures:
            print(f"  {name}: {why}")
        print("\n런타임 코드에서 위 패키지를 쓰려면 함수 안에서 늦게 임포트하고,"
              "\n없을 때의 동작을 정의해야 합니다 (broker/kis.py 참고).")
        return 1
    print("\n\033[32m✓ 전 모듈 임포트 + 백테스트 한 바퀴가 표준 라이브러리만으로 동작\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
