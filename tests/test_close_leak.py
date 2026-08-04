"""close() must kill Chrome even when the pinned thread is busy.

The leak this guards: teardown used to run on the env's single pinned thread, so
if that thread was mid page operation, close blocked forever, the episode stayed
in the registry and its Chrome stayed resident. 14 Chromes for 2 workers, then
an OOM kill.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

_ocr = types.ModuleType("easyocr")
_ocr.Reader = lambda *a, **k: None
sys.modules.setdefault("easyocr", _ocr)

# The close path does not use OSWorld's optional Drive setup/getters, but the
# benchmark imports their symbols at module load. Keep this lifecycle test
# hermetic instead of requiring cloud-integration packages on the Mac runner.
_pydrive = types.ModuleType("pydrive")
_pydrive_auth = types.ModuleType("pydrive.auth")
_pydrive_drive = types.ModuleType("pydrive.drive")
_pydrive_auth.GoogleAuth = type("GoogleAuth", (), {})
for _name in ("GoogleDrive", "GoogleDriveFile", "GoogleDriveFileList"):
    setattr(_pydrive_drive, _name, type(_name, (), {}))
_pydrive.auth = _pydrive_auth
_pydrive.drive = _pydrive_drive
sys.modules.setdefault("pydrive", _pydrive)
sys.modules.setdefault("pydrive.auth", _pydrive_auth)
sys.modules.setdefault("pydrive.drive", _pydrive_drive)
_desktop = types.ModuleType("desktop_env")
_desktop_module = types.ModuleType("desktop_env.desktop_env")
_desktop_module.DesktopEnv = type("DesktopEnv", (), {})
_desktop.desktop_env = _desktop_module
sys.modules.setdefault("desktop_env", _desktop)
sys.modules.setdefault("desktop_env.desktop_env", _desktop_module)

from local_chrome_env import LocalChromeEnv  # noqa: E402


def chrome_count(port: int) -> int:
    out = subprocess.run(["ps", "-eo", "command"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if f"remote-debugging-port={port}" in line)


def main() -> None:
    ok = True

    # 1. plain close
    env = LocalChromeEnv(headless=True)
    env._launch()
    port = env.chromium_port
    assert chrome_count(port) >= 1, "Chrome did not start"
    env.close()
    time.sleep(2)
    n = chrome_count(port)
    print(f"{'PASS' if n == 0 else 'FAIL'} plain close -> {n} chrome(s) left")
    ok &= n == 0

    # 2. close while the pinned thread is occupied -- the case that leaked
    env2 = LocalChromeEnv(headless=True)
    env2._launch()
    port2 = env2.chromium_port
    env2.active_url()

    def hog():
        try:
            env2._pool.submit(lambda: time.sleep(45)).result()
        except Exception:
            pass

    threading.Thread(target=hog, daemon=True).start()
    time.sleep(1)
    t0 = time.time()
    env2.close()
    elapsed = time.time() - t0
    time.sleep(2)
    n2 = chrome_count(port2)
    print(f"{'PASS' if n2 == 0 else 'FAIL'} close with busy thread -> {n2} chrome(s) "
          f"left after {elapsed:.1f}s")
    ok &= n2 == 0
    print(f"{'PASS' if elapsed < 30 else 'FAIL'} close did not block on the busy thread")
    ok &= elapsed < 30

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
