#!/usr/bin/python3
"""Private desktop launcher that makes indirectly opened Chrome attachable.

Applications such as LibreOffice and Thunderbird open HTTP links through the
desktop's registered browser entry rather than the OSWorld task setup path.
Without a debugging switch, that first launch creates a perfectly visible
Chrome window which the semantic browser adapter can never attach to.  This
wrapper preserves the normal browser/profile and URL handling while adding the
generic CDP and accessibility switches required by the modified Ghost image.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path


_BROWSERS = {
    "google-chrome": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ),
    "google-chrome-stable": (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ),
    "chromium": (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ),
    "chromium-browser": (
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ),
}


def browser_argv(
    executable: str, arguments: list[str], *, debugging_port: int = 9222,
) -> list[str]:
    """Compose one browser argv without duplicating caller-provided switches."""

    output = [executable]
    if not any(
        value == "--remote-debugging-port"
        or value.startswith("--remote-debugging-port=")
        for value in arguments
    ):
        output.extend((
            "--remote-debugging-address=0.0.0.0",
            f"--remote-debugging-port={debugging_port}",
        ))
    if "--force-renderer-accessibility" not in arguments:
        output.append("--force-renderer-accessibility")
    output.extend(arguments)
    return output


def _port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def ensure_cdp_bridge() -> int:
    """Return Chrome's private debug port after exposing guest port 9222.

    Modern Chrome can bind DevTools to loopback even when given a wildcard
    address. OSWorld therefore conventionally runs Chrome on 1337 and exposes
    it through a socat listener on 9222, the container-mapped port. Reuse an
    existing listener from task setup; otherwise start the same generic bridge.
    If socat is unavailable, fall back to direct port 9222 for older images.
    """

    if _port_is_listening(9222):
        return 1337
    socat = next(
        (
            candidate for candidate in ("/usr/bin/socat", "/bin/socat")
            if Path(candidate).is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if socat is None:
        return 9222
    process = subprocess.Popen(
        [
            socat,
            "TCP-LISTEN:9222,reuseaddr,fork",
            "TCP:127.0.0.1:1337",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(20):
        if _port_is_listening(9222):
            return 1337
        if process.poll() is not None:
            break
        time.sleep(0.05)
    # Do not leave a late-starting bridge racing Chrome for the direct fallback
    # port. A failed bridge attempt must have no continuing side effect.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    return 9222


def resolve_browser(alias: str) -> str:
    candidates = _BROWSERS.get(alias)
    if candidates is None:
        raise ValueError("browser alias is not allowed")
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError(f"browser executable is unavailable for {alias}")


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        print("browser alias is required", file=sys.stderr)
        return 64
    try:
        executable = resolve_browser(values[0])
    except (ValueError, FileNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 69
    os.execv(
        executable,
        browser_argv(
            executable, values[1:], debugging_port=ensure_cdp_bridge(),
        ),
    )
    return 70  # pragma: no cover - execv does not return


if __name__ == "__main__":
    raise SystemExit(main())
