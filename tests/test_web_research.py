"""Task-agnostic checks for temporary-tab batch web research."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

from web_provider import WebProvider, _public_http_url  # noqa: E402


class FakeTemporaryPage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.closed = False
        self.navigated = []

    def goto(self, url, **_kwargs):
        self.url = url
        self.navigated.append(url)

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, source, limit):
        if "document.querySelectorAll('a h3')" in source:
            return [{
                "title": "Visible result",
                "url": "https://example.com/evidence",
                "snippet": f"result text, limit {limit}",
            }]
        return {
            "title": "Evidence page",
            "url": self.url,
            "text": "public evidence"[:limit],
            "total_characters": 15,
            "truncated": False,
        }

    def close(self):
        self.closed = True


class FakeOriginalPage:
    def __init__(self):
        self.context = FakeContext()
        self.fronted = 0

    def is_closed(self):
        return False

    def bring_to_front(self):
        self.fronted += 1


class FakeContext:
    def __init__(self):
        self.temporary_pages = []

    def new_page(self):
        page = FakeTemporaryPage(self)
        self.temporary_pages.append(page)
        return page


def main() -> None:
    provider = WebProvider("127.0.0.1")
    original = FakeOriginalPage()
    provider._page = lambda: original  # type: ignore[method-assign]

    searched = json.loads(WebProvider.search.__wrapped__(
        provider, ["first query", "second query"], 3,
    ))
    search_page = original.context.temporary_pages[-1]
    assert searched["ok"] is True
    assert len(searched["queries"]) == 2
    assert searched["queries"][0]["results"][0]["title"] == "Visible result"
    assert all("google.com/search?q=" in url for url in search_page.navigated)
    assert search_page.closed
    assert provider._active is original
    assert original.fronted == 1
    print("PASS batch search closes its temporary page and restores the task tab")

    read = json.loads(WebProvider.read_pages.__wrapped__(
        provider, ["https://example.com/one", "https://example.org/two"], 1200,
    ))
    read_page = original.context.temporary_pages[-1]
    assert read["ok"] is True
    assert [page["requested_url"] for page in read["pages"]] == [
        "https://example.com/one", "https://example.org/two",
    ]
    assert all(page["text"] == "public evidence" for page in read["pages"])
    assert read_page.closed
    assert provider._active is original
    assert original.fronted == 2
    print("PASS batch reading preserves ordered evidence and restores the task tab")

    for rejected in (
        "file:///home/user/secret", "http://localhost:8079/episodes",
        "http://127.0.0.1:9222/json", "http://metadata.google.internal/",
    ):
        try:
            _public_http_url(rejected)
            raise AssertionError(f"private URL accepted: {rejected}")
        except ValueError:
            pass
    assert _public_http_url("https://example.com/a") == "https://example.com/a"
    print("PASS batch reading rejects file, loopback and special-use destinations")

    try:
        WebProvider.search.__wrapped__(provider, ["x"] * 9, 3)
        raise AssertionError("oversized batch accepted")
    except ValueError:
        pass
    print("PASS research batches are bounded independently of model schemas")
    provider._pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
