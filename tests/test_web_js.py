"""web_js: the code-mode reduction must actually work end to end."""
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

PAGE = """<html><body>
<input id="checkin" type="text" placeholder="Check-in date"><div id="out">empty</div>
<select id="kind" onchange="document.getElementById('state').textContent=this.value">
  <option value="small">Small</option><option value="large">Large</option>
</select>
<input id="agree" type="checkbox">
<button id="apply" aria-pressed="false" onclick="
  this.setAttribute('aria-pressed','true');
  document.getElementById('state').textContent +=
  ':' + document.getElementById('agree').checked">Apply</button>
<div id="state">small</div>
<ul><li class="p">$120</li><li class="p">$85</li><li class="p">$240</li></ul>
<iframe srcdoc="<div id='inside'>FRAME-CONTENT</div>"></iframe>
<label for="labelled">Associated Form Label</label><input id="labelled">
<span id="airport-label">Departure Airport</span>
<div role="combobox" aria-labelledby="airport-label" aria-activedescendant="airport-bom"
  tabindex="0" style="width:220px;height:32px"></div>
<div role="option" id="airport-bom" aria-selected="false">Mumbai (BOM)</div>
<div id="shadow-host"></div>
<script>document.getElementById('checkin').addEventListener('change',
  e => document.getElementById('out').textContent = 'GOT:' + e.target.value);
const shadow = document.getElementById('shadow-host').attachShadow({mode:'open'});
shadow.innerHTML = `<button id="shadow-action"
  onclick="this.textContent='SHADOW-CLICKED'">Shadow Action</button>`;</script>
</body></html>"""

def main():
    ok=True
    t=Path("/tmp/js_page.html"); t.write_text(PAGE)
    ep=server.create_episode(server.CreateEpisode(
        task_path=str(REPO/"OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"), web=True))
    eid=ep["episode_id"]
    server.web_action(eid, server.WebAction(action="navigate", url=f"file://{t}"))

    r=server.web_action(eid, server.WebAction(action="js",
        code="(() => { const d=new Date(2026,6,30); d.setDate(d.getDate()+((8-d.getDay())%7||7)); return d.toDateString(); })()"))
    good="Aug 03" in r["result"] or "Aug 3" in r["result"]
    print(f"{'PASS' if good else 'FAIL'} date arithmetic in JS -> {r['result'][:40]}")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="js", expect_change=True,
        code="(() => { const i=document.getElementById('checkin'); i.value='08/14/2026'; i.dispatchEvent(new Event('change')); return document.getElementById('out').textContent; })()"))
    good="GOT:08/14/2026" in r["result"]
    print(f"{'PASS' if good else 'FAIL'} set input + fire event -> {r['result'][:40]}")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="js",
        code="Array.from(document.querySelectorAll('.p')).map(e=>e.textContent)"))
    good='"$85"' in r["result"]
    print(f"{'PASS' if good else 'FAIL'} structured extraction -> {r['result'][:40]}")
    ok&=good

    r=server.web_action(eid, server.WebAction(
        action="js", code="const answer = 41; return {answer: answer + 1};"))
    good='"answer": 42' in r["result"]
    print(f"{'PASS' if good else 'FAIL'} top-level return is normalized")
    ok&=good

    server.web_action(eid, server.WebAction(action="elements"))
    r=server.web_action(eid, server.WebAction(
        action="js",
        code="await new Promise(resolve => setTimeout(resolve, 10)); return {waited:true};"))
    good='"waited": true' in r["result"]
    print(f"{'PASS' if good else 'FAIL'} top-level await is normalized")
    ok&=good

    r=server.web_action(eid, server.WebAction(
        action="js", code="const answer = 41; console.log({answer: answer + 1});"))
    good='"logs":' in r["result"] and '\\"answer\\":42' in r["result"]
    print(f"{'PASS' if good else 'FAIL'} console-only inspection returns its logs")
    ok&=good

    r=server.web_action(eid, server.WebAction(
        action="js", expect_change=True,
        code="({before:ghost.inspect('input'),filled:ghost.fill('#checkin','2042-08-09')})"))
    state=server.web_action(
        eid, server.WebAction(action="js", code="document.body.innerText"))["result"]
    good=("2042-08-09" in r["result"] and "GOT:2042-08-09" in state
          and "web_no_change" not in r)
    print(f"{'PASS' if good else 'FAIL'} helper inspects and fills a reactive-style input")
    ok&=good

    server.web_action(eid, server.WebAction(action="elements"))
    r=server.web_action(eid, server.WebAction(
        action="js", code="ghost.find('Apply', 'button:contains(\"Apply\")')"))
    empty=server.web_action(eid, server.WebAction(
        action="js", code="ghost.find('Apply', '')"))["result"]
    good="Apply" in r["result"] and "Apply" in empty
    print(f"{'PASS' if good else 'FAIL'} helper normalizes common text/empty selectors")
    ok&=good

    r=server.web_action(eid, server.WebAction(
        action="js",
        code="document.body.style.display = 'none'; true",
        expect_change=True,
    ))
    good=any("DOM surgery rejected" in error for error in r.get("errors", []))
    print(f"{'PASS' if good else 'FAIL'} page-rewriting DOM surgery is rejected")
    ok&=good

    r=server.web_action(eid, server.WebAction(
        action="js",
        code="require('child_process').execSync('xdotool search Chrome')",
    ))
    good=any("Non-browser JavaScript rejected" in error for error in r.get("errors", []))
    print(f"{'PASS' if good else 'FAIL'} Node and shell escape attempts are rejected")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"select","selector":"#kind","value":"large"},
        {"op":"check","selector":"#agree","checked":True},
        {"op":"click","selector":"#apply"},
    ]))
    state=server.web_action(
        eid, server.WebAction(action="js", code="document.getElementById('state').textContent")
    )["result"]
    good='"ok": true' in r["result"] and "large:true" in state
    print(f"{'PASS' if good else 'FAIL'} ordered trusted selector program")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"fill","selector":"#checkin","value":"before-failure"},
        {"op":"click","selector":"#does-not-exist"},
        {"op":"click","selector":"#apply"},
    ]))
    state=server.web_action(
        eid, server.WebAction(action="js",
                              code="({value:checkin.value,state:state.textContent})")
    )["result"]
    good=('"ok": false' in r["result"] and '"failed_step": 1' in r["result"]
          and "before-failure" in state and "large:true:true" not in state)
    print(f"{'PASS' if good else 'FAIL'} action program stops at first failure")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"fill","by":"placeholder","name":"Check-in date",
         "value":"semantic-date","exact":True},
        {"op":"click","by":"role","role":"button","name":"Apply","exact":True},
    ]))
    state=server.web_action(
        eid, server.WebAction(
            action="js",
            code="({value:checkin.value,pressed:apply.getAttribute('aria-pressed')})",
        ),
    )["result"]
    good=('"ok": true' in r["result"] and "semantic-date" in state
          and '"pressed": "true"' in state)
    print(f"{'PASS' if good else 'FAIL'} semantic trusted action targets")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"click","by":"role","role":"button","value":"Apply"},
        {"op":"fill","by":"role","role":"textbox","index":0,
         "value":"role-without-name"},
    ]))
    good='"ok": true' in r["result"] and "role-without-name" in r["result"]
    print(f"{'PASS' if good else 'FAIL'} semantic action tolerates unambiguous omissions")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"click","selector":"#apply","index":-1},
    ]))
    good='"ok": false' in r["result"] and "zero or greater" in r["result"]
    print(f"{'PASS' if good else 'FAIL'} action program rejects negative indices")
    ok&=good

    r=server.web_action(eid, server.WebAction(action="js", code="((("))
    good='"ok": false' in r["result"] and '"error":' in r["result"]
    print(f"{'PASS' if good else 'FAIL'} syntax error surfaced, not crashed")
    ok&=good

    frames=server.web_action(eid, server.WebAction(action="frames"))
    r=server.web_action(eid, server.WebAction(
        action="js", frame=1,
        code="document.getElementById('inside').textContent"))
    good="FRAME-CONTENT" in r["result"] and "\t" in frames["result"]
    print(f"{'PASS' if good else 'FAIL'} script can target an embedded frame")
    ok&=good

    listing=server.web_action(eid, server.WebAction(action="elements"))["web_elements"]
    inspect=server.web_action(
        eid, server.WebAction(
            action="js",
            code="({label:ghost.find('Associated Form Label'),shadow:ghost.find('Shadow Action')})",
        ),
    )["result"]
    good="Associated Form Label" in listing and "Associated Form Label" in inspect
    print(f"{'PASS' if good else 'FAIL'} associated form labels are exposed")
    ok&=good
    aria=server.web_action(
        eid, server.WebAction(
            action="js",
            code="({combo:ghost.find('Departure Airport'),option:ghost.find('Mumbai (BOM)')})",
        ),
    )["result"]
    good=("Departure Airport" in listing and "Mumbai (BOM)" in listing
          and "active=airport-bom" in listing and "Departure Airport" in aria
          and "Mumbai (BOM)" in aria)
    print(f"{'PASS' if good else 'FAIL'} ARIA labels and option controls are exposed")
    ok&=good
    r=server.web_action(eid, server.WebAction(action="actions", actions=[
        {"op":"click","selector":"#shadow-action"},
    ]))
    shadow=server.web_action(
        eid, server.WebAction(action="js", code="ghost.find('SHADOW-CLICKED')")
    )["result"]
    good=('"ok": true' in r["result"] and "SHADOW-CLICKED" in shadow
          and "Shadow Action" in listing)
    print(f"{'PASS' if good else 'FAIL'} open shadow-DOM controls inspect and act")
    ok&=good

    server.close_episode(eid)
    print("\nRESULT:","ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)
if __name__ == "__main__":
    main()
