"""Warm OSWorld's 11.4GB Docker-VM image cache exactly once per GCP host.

Running concurrent first episodes on a cold host races multiple downloads into
the same path. Constructing and closing one environment serially avoids that
thundering herd without modifying upstream OSWorld.
"""
from __future__ import annotations

import os
import sys
import time

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "ghost-semantic-os")
sys.path.insert(0, os.path.join(REPO, "OSWorld"))

try:
    import pydrive  # noqa: F401
except ImportError:
    import pydrive2
    import pydrive2.auth
    import pydrive2.drive

    sys.modules.setdefault("pydrive", pydrive2)
    sys.modules.setdefault("pydrive.auth", pydrive2.auth)
    sys.modules.setdefault("pydrive.drive", pydrive2.drive)

from desktop_env.desktop_env import DesktopEnv

started = time.time()
env = DesktopEnv(
    provider_name="docker",
    action_space="pyautogui",
    headless=True,
    require_a11y_tree=True,
)
print(f"WARM_CONSTRUCTED {time.time() - started:.0f}s", flush=True)
env.close()
print("WARM_OK", flush=True)
