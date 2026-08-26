import runpy
import sys
from pathlib import Path

import pytest


def test_standalone_script_can_import_project_without_installing_it(capsys):
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "morning_briefing.py"
    old_path, old_argv = list(sys.path), list(sys.argv)
    saved_modules = {name: module for name, module in sys.modules.items()
                     if name == "autotrader" or name.startswith("autotrader.")}
    try:
        for name in saved_modules:
            sys.modules.pop(name, None)
        sys.path[:] = [p for p in sys.path
                       if Path(p or ".").resolve() != root.resolve()]
        sys.argv[:] = [str(script), "--help"]
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(script), run_name="__main__")
        assert stopped.value.code == 0
        assert "--no-kakao" in capsys.readouterr().out
    finally:
        for name in list(sys.modules):
            if name == "autotrader" or name.startswith("autotrader."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = old_path
        sys.argv[:] = old_argv
