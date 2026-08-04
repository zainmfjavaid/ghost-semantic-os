"""Off-screen elements + CDP scroll.

Two things must hold or this change makes the harness worse, not better:

1. Descriptors must be generated from the exact retained handle array, because
   actions map descriptor index -> handle index. Taking two DOM snapshots can
   silently mis-target every later control on a dynamic page — the worst
   possible failure, since it looks like the model choosing the wrong element.
2. Clicking an element that is below the fold must actually work, since that is
   the entire justification for listing it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "envserver"))
from web_provider import WebProvider  # noqa: E402

PAGE = """<html><body style="font-family:sans-serif">
<h1>top</h1>
<button id="b0" onclick="document.title='CLICKED_FIRST'">First Button</button>
<div style="height:3000px">tall filler</div>
<button id="b1" onclick="document.title='CLICKED_BELOW_FOLD'">Bottom Button</button>
<a href="#x" onclick="document.title='CLICKED_LINK'; return false">Bottom Link</a>
</body></html>"""


def js_result(wp: WebProvider, code: str):
    return json.loads(wp.run_js(code))["result"]


def main() -> None:
    ok = True
    tmp = Path("/tmp/osworld_offscreen.html")
    tmp.write_text(PAGE)

    wp = WebProvider("127.0.0.1", port=9222)
    wp.navigate(f"file://{tmp}")
    els = wp.elements()

    print(wp.describe(els))

    # 1. index alignment: descriptors and handles must be 1:1
    handles = getattr(wp, "_handles", [])
    if len(handles) != len(els):
        print(f"FAIL handle/descriptor mismatch: {len(handles)} vs {len(els)}")
        ok = False
    else:
        print(f"PASS handle/descriptor alignment ({len(els)} each)")

    # Verify alignment by EFFECT, not just count — a same-length but shuffled
    # handle list is the dangerous case a count check would pass. Public
    # provider methods keep all Playwright objects on their owning thread.
    expected = ["CLICKED_FIRST", "CLICKED_BELOW_FOLD", "CLICKED_LINK"]
    for i, title in enumerate(expected):
        wp.click(i, els)
        actual = js_result(wp, "document.title")
        if actual != title:
            print(f"FAIL index {i}: expected effect {title!r}, got {actual!r}")
            ok = False
    print("PASS descriptor/handle effects agree")

    # 2. the below-fold button must be listed AND flagged
    below = [e for e in els if not e.get("onscreen", True)]
    if not below:
        print("FAIL no below-fold element listed (filter still dropping them)")
        ok = False
    else:
        print(f"PASS {len(below)} below-fold element(s) listed and flagged")

    # 3. clicking it must work via auto-scroll
    idx = next(i for i, e in enumerate(els) if e["label"].startswith("Bottom Button"))
    print("click ->", wp.click(idx, els))
    title = js_result(wp, "document.title")
    if title == "CLICKED_BELOW_FOLD":
        print("PASS below-fold click landed without any scrolling by the agent")
    else:
        print(f"FAIL below-fold click did not fire (title={title!r})")
        ok = False

    # 4. CDP scroll moves the page
    y0 = js_result(wp, "window.scrollY")
    wp.scroll("up", 20)
    wp.scroll("down", 4)
    y1 = js_result(wp, "window.scrollY")
    if y1 > 0 and y1 != y0:
        print(f"PASS scroll moved page {y0} -> {y1}")
    else:
        print(f"FAIL scroll did nothing ({y0} -> {y1})")
        ok = False

    # 5. A detached handle must fail loudly, never fall back to the stale
    # coordinate where some unrelated replacement control may now sit.
    wp.run_js(
        "document.body.innerHTML='<button id=\"gone\">Gone Soon</button>'"
    )
    stale = wp.elements()
    wp.run_js("document.getElementById('gone').remove()")
    try:
        wp.click(0, stale)
        print("FAIL detached handle silently fell back to coordinates")
        ok = False
    except RuntimeError as exc:
        print(f"PASS detached handle rejected -> {str(exc)[:70]}")

    wp.close()
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")


if __name__ == "__main__":
    main()
