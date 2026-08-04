"""Tab listing/switching/closing.

Two things worth guarding:
 - a click that opens a NEW tab silently moves the agent; it must be visible
 - switch_tab must actually redirect later actions. Playwright's ctx.pages
   returns a fresh list, so reordering it does nothing: a switch could report
   success while every subsequent action still hit the old tab.
"""
from __future__ import annotations
import sys, types
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(REPO/"envserver"))
_o=types.ModuleType("easyocr"); _o.Reader=lambda *a,**k: None
sys.modules.setdefault("easyocr",_o)
class _E:
    def __init__(s,*a,**k): s.vm_ip="127.0.0.1"; s.chromium_port=9222
    def reset(s,task_config=None): return {"screenshot":None,"accessibility_tree":None}
    def _get_obs(s): return {"screenshot":None,"accessibility_tree":None}
    def step(s,c,pause=1.0): return {"screenshot":None},0,False,{}
    def evaluate(s): return 0.0
    def close(s): pass
f=types.ModuleType("desktop_env"); m=types.ModuleType("desktop_env.desktop_env")
m.DesktopEnv=_E; f.desktop_env=m
sys.modules["desktop_env"]=f; sys.modules["desktop_env.desktop_env"]=m
import server  # noqa: E402
from web_provider import WebProvider  # noqa: E402
server.DesktopEnv=_E

A="""<html><body><h1>PAGE-A</h1>
<a href="https://example.com" target="_blank">Open B</a></body></html>"""

def main():
    ok=True
    class FakePage:
        def __init__(self, url):
            self.url=url
        def is_closed(self):
            return False
    wanted=FakePage("https://expected.example/path/")
    other=FakePage("https://other.example/")
    fake_context=types.SimpleNamespace(pages=[wanted, other])
    fake_provider=WebProvider(
        "127.0.0.1", initial_active_url="https://expected.example/path",
    )
    fake_provider._browser=types.SimpleNamespace(contexts=[fake_context])
    selected=fake_provider._page()
    fake_provider._pool.shutdown(wait=False)
    initial_ok=selected is wanted
    print(f"{'PASS' if initial_ok else 'FAIL'} configured last URL pins initial active tab")
    ok&=initial_ok

    d=Path("/tmp/tabtest"); d.mkdir(exist_ok=True)
    (d/"a.html").write_text(A)
    ep=server.create_episode(server.CreateEpisode(
        task_path=str(REPO/"OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"), web=True))
    eid=ep["episode_id"]
    server.web_action(eid, server.WebAction(action="navigate", url=f"file://{d}/a.html"))

    t=server.web_action(eid, server.WebAction(action="tabs"))
    n0=len(t["tabs"]); print(f"tabs at start: {n0}")
    # Pin the existing page explicitly. A target=_blank click still has to
    # adopt the newly opened tab for subsequent actions.
    server.web_action(eid, server.WebAction(action="switch_tab", index=0))

    # open a second tab via target=_blank
    els=server.web_action(eid, server.WebAction(action="find", query="Open B"))
    server.web_action(eid, server.WebAction(action="click", index=0))
    t=server.web_action(eid, server.WebAction(action="tabs"))
    grew=len(t["tabs"])>n0
    print(f"{'PASS' if grew else 'FAIL'} new tab is visible ({n0} -> {len(t['tabs'])})")
    ok&=grew

    body=server.web_action(eid, server.WebAction(action="text"))["page_text"]
    onB="Example Domain" in body
    print(f"{'PASS' if onB else 'FAIL'} actions follow the newly opened tab")
    ok&=onB

    # switch BACK to tab 0 -- the case that silently fails if only pages order is used
    server.web_action(eid, server.WebAction(action="switch_tab", index=0))
    body=server.web_action(eid, server.WebAction(action="text"))["page_text"]
    backOnA="PAGE-A" in body
    print(f"{'PASS' if backOnA else 'FAIL'} switch_tab actually redirects later actions")
    ok&=backOnA

    if grew:
        r=server.web_action(eid, server.WebAction(action="close_tab", index=1))
        t=server.web_action(eid, server.WebAction(action="tabs"))
        closed=len(t["tabs"])==n0
        print(f"{'PASS' if closed else 'FAIL'} close_tab removes it ({r.get('result')})")
        ok&=closed

    # The code-execution paths must adopt popups too; otherwise code-first can
    # open the right result and then silently keep inspecting the old page.
    server.web_action(eid, server.WebAction(action="switch_tab", index=0))
    server.web_action(eid, server.WebAction(
        action="js", expect_change=True, code="ghost.click('a')",
    ))
    t=server.web_action(eid, server.WebAction(action="tabs"))
    body=server.web_action(eid, server.WebAction(action="text"))["page_text"]
    js_popup=len(t["tabs"])==n0+1 and "Example Domain" in body
    print(f"{'PASS' if js_popup else 'FAIL'} web_js adopts its newly opened tab")
    ok&=js_popup
    if len(t["tabs"]) > n0:
        server.web_action(eid, server.WebAction(action="close_tab", index=1))

    server.web_action(eid, server.WebAction(action="switch_tab", index=0))
    server.web_action(eid, server.WebAction(
        action="actions", actions=[{"op":"click","selector":"a"}],
    ))
    t=server.web_action(eid, server.WebAction(action="tabs"))
    body=server.web_action(eid, server.WebAction(action="text"))["page_text"]
    action_popup=len(t["tabs"])==n0+1 and "Example Domain" in body
    print(f"{'PASS' if action_popup else 'FAIL'} web_actions adopts its newly opened tab")
    ok&=action_popup
    if len(t["tabs"]) > n0:
        server.web_action(eid, server.WebAction(action="close_tab", index=1))

    try:
        server.web_action(eid, server.WebAction(action="switch_tab", index=99))
        print("FAIL out-of-range switch accepted"); ok=False
    except Exception:
        print("PASS out-of-range switch rejected")

    server.close_episode(eid)
    print("\nRESULT:","ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)
if __name__ == "__main__":
    main()
