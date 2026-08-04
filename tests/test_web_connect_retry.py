"""A failed first CDP attach must not poison every later web action."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "envserver"))

from web_provider import WebProvider  # noqa: E402


def main() -> None:
    provider = WebProvider(
        "127.0.0.1",
        port=65534,
        fallback_ports=(),
        call_timeout_s=10,
    )
    errors: list[str] = []
    try:
        for _ in range(2):
            try:
                provider.elements()
            except Exception as exc:
                errors.append(str(exc))
    finally:
        provider.close()

    good = (
        len(errors) == 2
        and all("could not reach Chrome over CDP" in error for error in errors)
        and all("inside the asyncio loop" not in error for error in errors)
    )
    print(
        f"{'PASS' if good else 'FAIL'} failed CDP attach remains retryable "
        f"without poisoning the provider"
    )
    if not good:
        print(errors)
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
