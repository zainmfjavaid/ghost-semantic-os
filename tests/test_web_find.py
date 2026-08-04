"""Truncation must be announced, and web_find must reach past the display cap.

The defect: elements() cut the list at 120 and said nothing, so on a large page
a control past that point was invisible to the agent forever — re-listing could
never reveal it, and silent truncation reads as "this is the whole page".
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))
_ocr = types.ModuleType("easyocr"); _ocr.Reader = lambda *a, **k: None
sys.modules.setdefault("easyocr", _ocr)


class _StubEnv:
    def __init__(self, *a, **k): self.vm_ip="127.0.0.1"; self.chromium_port=9222
    def reset(self, task_config=None): return {"screenshot": None, "accessibility_tree": None}
    def _get_obs(self): return {"screenshot": None, "accessibility_tree": None}
    def step(self, c, pause=1.0): return {"screenshot": None}, 0, False, {}
    def evaluate(self): return 0.0
    def close(self): pass


fake=types.ModuleType("desktop_env"); fm=types.ModuleType("desktop_env.desktop_env")
fm.DesktopEnv=_StubEnv; fake.desktop_env=fm
sys.modules["desktop_env"]=fake; sys.modules["desktop_env.desktop_env"]=fm
import server  # noqa: E402
server.DesktopEnv=_StubEnv

# 200 filler buttons, then the one that matters — well past the 120 display cap.
BUTTONS = "".join(f'<button>Filler {i}</button>' for i in range(200))
PAGE = f"""<html><body>{BUTTONS}
<button id="target" onclick="document.getElementById('out').textContent='REACHED'">Proceed To Checkout</button>
<div id="out">not-clicked</div>
</body></html>"""


def main() -> None:
    ok = True
    tmp = Path("/tmp/find_page.html"); tmp.write_text(PAGE)
    ep = server.create_episode(server.CreateEpisode(
        task_path=str(REPO/"OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"), web=True))
    eid = ep["episode_id"]
    server.web_action(eid, server.WebAction(action="navigate", url=f"file://{tmp}"))

    r = server.web_action(eid, server.WebAction(action="elements"))
    listing = r["web_elements"]
    rows = [l for l in listing.splitlines()[1:] if not l.startswith("...")]
    print(f"listing shows {len(rows)} rows of {r['web_element_count']} known")

    announced = "more interactive elements exist" in listing
    print(f"{'PASS' if announced else 'FAIL'} truncation is announced, not silent")
    ok &= announced

    hidden = "Proceed To Checkout" not in listing
    print(f"{'PASS' if hidden else 'FAIL'} target is genuinely past the cap "
          f"(so this test is testing something)")
    ok &= hidden

    r = server.web_action(eid, server.WebAction(action="find", query="checkout"))
    print("find ->", r.get("result"))
    found = "Proceed To Checkout" in (r.get("web_elements") or "")
    print(f"{'PASS' if found else 'FAIL'} web_find reaches past the cap")
    ok &= found

    server.web_action(eid, server.WebAction(action="click", index=0))
    # Read the result THROUGH the server, not by touching the provider directly:
    # Playwright is pinned to the episode thread, and reaching in from the test
    # thread raises "cannot switch to a different thread". That is the pinning
    # working, not a bug -- the earlier version of this test was wrong.
    body = server.web_action(eid, server.WebAction(action="text")).get("page_text", "")
    hit = "REACHED" in body
    print(f"{'PASS' if hit else 'FAIL'} clicking a found index hits the right element")
    ok &= hit

    r = server.web_action(eid, server.WebAction(action="find", query="zzz-nonexistent"))
    helpful = "No interactive element matches" in (r.get("result") or "")
    print(f"{'PASS' if helpful else 'FAIL'} empty search explains itself")
    ok &= helpful

    server.close_episode(eid)
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
