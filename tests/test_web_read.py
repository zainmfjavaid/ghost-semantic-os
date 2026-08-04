"""web_read must announce truncation and be able to search past it."""
from __future__ import annotations
import sys, types
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/"envserver"))
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
server.DesktopEnv=_E

FILLER = "lorem ipsum dolor sit amet " * 400          # ~10k chars, past the 4k cap
PAGE = f"<html><body><p>{FILLER}</p><p>FINAL ANSWER: 42 degrees</p></body></html>"

def main():
    ok=True
    t=Path("/tmp/read_page.html"); t.write_text(PAGE)
    ep=server.create_episode(server.CreateEpisode(
        task_path=str(REPO/"OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"), web=True))
    eid=ep["episode_id"]
    server.web_action(eid, server.WebAction(action="navigate", url=f"file://{t}"))

    plain=server.web_action(eid, server.WebAction(action="text"))["page_text"]
    announced="TRUNCATED" in plain
    print(f"{'PASS' if announced else 'FAIL'} truncation announced")
    ok&=announced
    hidden="FINAL ANSWER" not in plain
    print(f"{'PASS' if hidden else 'FAIL'} answer genuinely past the cut")
    ok&=hidden

    found=server.web_action(eid, server.WebAction(action="text", query="FINAL ANSWER"))["page_text"]
    got="42 degrees" in found
    print(f"{'PASS' if got else 'FAIL'} query reaches text past the cut")
    ok&=got

    miss=server.web_action(eid, server.WebAction(action="text", query="zzz"))["page_text"]
    clear="no occurrence" in miss
    print(f"{'PASS' if clear else 'FAIL'} miss explains itself")
    ok&=clear
    server.close_episode(eid)
    print("\nRESULT:","ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)
if __name__ == "__main__":
    main()
