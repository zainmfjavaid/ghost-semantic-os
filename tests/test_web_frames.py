import sys
import json
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "envserver"))
from web_provider import WebProvider
ok = True
wp = WebProvider("127.0.0.1", 1337)
try:
    # 1. redirect-heavy navigation must not raise
    r = wp.navigate("http://duckduckgo.com")   # http -> https redirect
    print("PASS redirect-tolerant navigate ->", r)

    # 2. synchronous runaway code must be terminated without poisoning the page
    wp._script_timeout_ms = 100
    started = time.monotonic()
    timed = json.loads(wp.run_js(
        "(() => { const start=performance.now(); "
        "while (performance.now()-start < 1000) {} return 1; })()"
    ))
    sync_elapsed = time.monotonic() - started
    started = time.monotonic()
    promised = json.loads(wp.run_js("new Promise(() => {})"))
    async_elapsed = time.monotonic() - started
    wp._script_timeout_ms = 10_000
    responsive = json.loads(wp.run_js("'still-responsive'"))["result"]
    if (not timed["ok"] and not promised["ok"]
            and sync_elapsed < 0.8 and async_elapsed < 0.8
            and responsive == "still-responsive"):
        print(
            f"PASS runaway scripts terminated in {sync_elapsed:.2f}s sync / "
            f"{async_elapsed:.2f}s async; page remains responsive"
        )
    else:
        print(
            f"FAIL script deadline -> sync={timed}, async={promised}, "
            f"elapsed={sync_elapsed:.2f}/{async_elapsed:.2f}, responsive={responsive}"
        )
        ok = False

    # 3. iframe content must be listed
    wp.run_js("""(async () => {
      document.body.innerHTML = `<h1>outer</h1><button>Outer Button</button>
        <iframe srcdoc="<button onclick=&quot;document.title='INNER_CLICKED'&quot;>Inner Button</button><input placeholder='inner field'>"
                style="width:400px;height:200px"></iframe>`;
      await new Promise(resolve => setTimeout(resolve, 600));
      return true;
    })()""")
    els = wp.elements()
    labels = [e["label"] for e in els]
    frames_used = sorted({e.get("frame", 0) for e in els})
    print(f"\nelements={len(els)} frames_represented={frames_used}")
    print("labels:", labels)
    if any("Inner" in l for l in labels):
        print("PASS iframe content is listed")
    else:
        print("FAIL iframe content missing"); ok = False

    # 4. clicking an iframe element via handle must work
    idx = next((i for i, e in enumerate(els) if "Inner Button" in e["label"]), None)
    if idx is not None:
        print(wp.click(idx, els))
        title = json.loads(wp.run_js("document.title", 1))["result"]
        if title == "INNER_CLICKED":
            print("PASS clicked an element inside an iframe without coordinates")
        else:
            print(f"FAIL iframe click landed incorrectly ({title!r})"); ok = False
    else:
        print("SKIP no inner button to click"); ok = False
except Exception as e:
    print("FAIL", type(e).__name__, str(e)[:160]); ok = False
finally:
    wp.close()
print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
