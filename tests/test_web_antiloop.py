"""Repeat + DOM-no-op detection on the WEB path.

Both were absent from /web, so in browser-only mode -- the configuration every
result was produced in -- the anti-loop machinery was inactive over the dominant
failure mode (loop to budget exhaustion).

Guards the two ways this can be wrong:
  - false negatives: a genuinely repeated action not flagged
  - FALSE POSITIVES: legitimate work flagged as a loop, which would actively
    push the agent off a working plan. That is the worse failure.
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
    def __init__(self, *a, **k):
        self.vm_ip = "127.0.0.1"; self.chromium_port = 9222
    def reset(self, task_config=None): return {"screenshot": None, "accessibility_tree": None}
    def _get_obs(self): return {"screenshot": None, "accessibility_tree": None}
    def step(self, command, pause=1.0): return {"screenshot": None}, 0, False, {}
    def evaluate(self): return 0.0
    def close(self): pass


fake = types.ModuleType("desktop_env"); fm = types.ModuleType("desktop_env.desktop_env")
fm.DesktopEnv = _StubEnv; fake.desktop_env = fm
sys.modules["desktop_env"] = fake; sys.modules["desktop_env.desktop_env"] = fm

import server  # noqa: E402
server.DesktopEnv = _StubEnv

PAGE = """<html><body>
<button id="dead" onclick="void 0">Dead Button</button>
<button id="live" onclick="document.getElementById('slot').innerHTML=
  '<a href=/x>New Link</a>'">Live Button</button>
<div id="slot"></div>
</body></html>"""


def main() -> None:
    ok = True
    tmp = Path("/tmp/antiloop_page.html"); tmp.write_text(PAGE)
    ep = server.create_episode(server.CreateEpisode(
        task_path=str(REPO / "OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"), web=True))
    eid = ep["episode_id"]
    server.web_action(eid, server.WebAction(action="navigate", url=f"file://{tmp}"))

    els = server.web_action(eid, server.WebAction(action="elements"))
    idx = {}
    for line in (els["web_elements"] or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 6:
            idx[parts[5]] = int(parts[0])

    dead, live = idx.get("Dead Button"), idx.get("Live Button")
    print(f"indices: dead={dead} live={live}")

    # 1. repeated identical click -> repeated_action climbs
    counts = []
    for _ in range(3):
        r = server.web_action(eid, server.WebAction(action="click", index=dead))
        counts.append(r.get("repeated_action", 0))
    print(f"{'PASS' if counts == [0, 1, 2] else 'FAIL'} repeat counter on identical "
          f"clicks -> {counts}")
    ok &= counts == [0, 1, 2]

    # 2. dead click -> DOM no-op flagged and escalating
    r = server.web_action(eid, server.WebAction(action="click", index=dead))
    n = r.get("web_no_change", 0)
    print(f"{'PASS' if n >= 3 else 'FAIL'} DOM no-op escalates on a dead button -> {n}")
    ok &= n >= 3

    # 3. FALSE POSITIVE guard: a click that really changes the page must reset
    r = server.web_action(eid, server.WebAction(action="click", index=live))
    n2 = r.get("web_no_change", 0)
    print(f"{'PASS' if n2 == 0 else 'FAIL'} real page change clears the no-op "
          f"counter -> {n2}")
    ok &= n2 == 0

    # 4. FALSE POSITIVE guard: different targets are not repetition
    server.web_action(eid, server.WebAction(action="click", index=dead))
    r = server.web_action(eid, server.WebAction(action="click", index=live))
    rep = r.get("repeated_action", 0)
    print(f"{'PASS' if rep == 0 else 'FAIL'} alternating targets not counted as "
          f"repeats -> {rep}")
    ok &= rep == 0

    # 5. re-listing/reading is legitimate polling, never a repeat
    server.web_action(eid, server.WebAction(action="elements"))
    r = server.web_action(eid, server.WebAction(action="elements"))
    print(f"{'PASS' if not r.get('repeated_action') else 'FAIL'} repeated elements() "
          f"not flagged")
    ok &= not r.get("repeated_action")

    # 6. Distinct read-only programs remain available up to the capability-
    # level limit. The next one is rejected without execution until the model
    # changes strategy through another interface or a causal action.
    reads = [
        server.web_action(
            eid,
            server.WebAction(action="js", code=f"document.title + {i}"),
        )
        for i in range(server.MAX_CONSECUTIVE_READONLY_JS)
    ]
    blocked = server.web_action(
        eid, server.WebAction(action="js", code="document.body.innerText")
    )
    server.web_action(eid, server.WebAction(action="click", index=dead))
    resumed = server.web_action(
        eid, server.WebAction(action="js", code="document.title")
    )
    good = (all("result" in item for item in reads)
            and "result" not in blocked
            and any("Strategy gate" in error for error in blocked.get("errors", []))
            and "result" in resumed
            and not resumed.get("readonly_js_streak"))
    print(f"{'PASS' if good else 'FAIL'} read-only JS strategy gate and reset")
    ok &= good

    server.close_episode(eid)
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
