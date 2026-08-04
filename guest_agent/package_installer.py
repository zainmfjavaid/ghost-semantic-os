#!/usr/bin/python3 -I
"""Root-owned, argv-only Debian package installer for the semantic guest.

This program is the complete privilege boundary for ``os.packages``. The
unprivileged semantic daemon may invoke it through one narrowly scoped sudoers
entry, but it cannot choose a command, an option, an environment variable, or
more than one package. Keep this file dependency-free and safe under Python's
isolated mode because it is installed root-owned in ``/usr/local/libexec``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys


_PACKAGE_NAME = re.compile(
    r"^[a-z0-9][a-z0-9+.-]{0,127}(?::[a-z0-9][a-z0-9-]{0,31})?$"
)
_APT_GET = "/usr/bin/apt-get"
_PREFLIGHT_NO_EFFECT = 65
_FIXED_ENVIRONMENT = {
    "DEBIAN_FRONTEND": "noninteractive",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or _PACKAGE_NAME.fullmatch(argv[1]) is None:
        print("expected exactly one validated Debian package name", file=sys.stderr)
        return 64
    if os.geteuid() != 0:
        print("package installer must run as root", file=sys.stderr)
        return 77
    package = argv[1]
    # Prove apt can construct a transaction before crossing the mutation
    # boundary. A failed simulation cannot have changed package state.
    preflight_argv = [
        _APT_GET,
        "install",
        "--simulate",
        "--no-install-recommends",
        "--",
        package,
    ]
    try:
        preflight = subprocess.run(
            preflight_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
            env=_FIXED_ENVIRONMENT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(
            f"package preflight failed without mutation: {type(error).__name__}",
            file=sys.stderr,
        )
        return _PREFLIGHT_NO_EFFECT
    if preflight.returncode != 0:
        print("package preflight failed without mutation", file=sys.stderr)
        if preflight.stderr:
            print(preflight.stderr[-8192:], file=sys.stderr, end="")
        return _PREFLIGHT_NO_EFFECT
    os.execve(
        _APT_GET,
        [
            _APT_GET,
            "install",
            "--yes",
            "--no-install-recommends",
            "--",
            package,
        ],
        _FIXED_ENVIRONMENT,
    )
    return 70  # pragma: no cover - execve either replaces the process or raises


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
