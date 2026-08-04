"""Env server backed by real OSWorld VMs via the Docker provider.

This is the GCP reference runner. The outer GCE instance supplies nested KVM;
OSWorld's Docker provider boots the benchmark's actual Ubuntu desktop VM inside
it. The HTTP surface remains identical to the AWS/local runners, so changing
clouds does not change the agent loop or tools.
"""
from __future__ import annotations

import os
import socket
import sys

HOME = os.path.expanduser("~")
REPO = os.environ.get("GHOST_OSWORLD_RUNTIME_REPO", os.path.join(HOME, "ghost-semantic-os"))
if not os.path.isabs(REPO) or not os.path.isdir(REPO):
    raise RuntimeError(f"invalid GHOST_OSWORLD_RUNTIME_REPO: {REPO!r}")
os.environ.setdefault("OSWORLD_OUTER_PROVIDER", "self-hosted")
os.environ.setdefault("OSWORLD_OUTER_VM_NAME", socket.gethostname())
sys.path.insert(0, os.path.join(REPO, "envserver"))
sys.path.insert(0, os.path.join(REPO, "OSWorld"))

# OSWorld still imports the abandoned `pydrive` package. pydrive2 is API
# compatible for the code paths used here.
try:
    import pydrive  # noqa: F401
except ImportError:
    import pydrive2
    import pydrive2.auth
    import pydrive2.drive

    sys.modules.setdefault("pydrive", pydrive2)
    sys.modules.setdefault("pydrive.auth", pydrive2.auth)
    sys.modules.setdefault("pydrive.drive", pydrive2.drive)

from desktop_env.desktop_env import DesktopEnv as _RealDesktopEnv  # noqa: E402


def _factory(*args, **kwargs):
    kwargs.pop("provider_name", None)
    kwargs.pop("region", None)
    kwargs.setdefault("action_space", "pyautogui")
    kwargs.setdefault("headless", True)
    kwargs.setdefault("require_a11y_tree", True)
    return _RealDesktopEnv(provider_name="docker", **kwargs)


import server  # noqa: E402

server.DesktopEnv = _factory


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(server.app, host="127.0.0.1", port=8079, log_level="warning")
