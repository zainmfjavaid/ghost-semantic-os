import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "envserver"))
from web_provider import WebProvider

wp = WebProvider("127.0.0.1", 1337)
ok = True
try:
    els = wp.elements()
    print(f"PASS connected; {len(els)} interactive elements on example.com")
    print(wp.describe(els)[:300])

    print("\n-- navigate to a form-bearing page --")
    print(wp.navigate("https://duckduckgo.com"))
    els = wp.elements()
    print(f"elements after navigate: {len(els)}")
    print(wp.describe(els)[:400])

    # find a search box and type into it
    idx = next((i for i, e in enumerate(els)
                if e["tag"] in ("input", "textarea")
                or "search" in (e["label"] + e["role"] + e["type"]).lower()), None)
    if idx is None:
        print("FAIL no search input found"); ok = False
    else:
        print("\n" + wp.type_into(idx, "osworld benchmark", els))
        after = wp.elements()
        print(f"PASS typed; element count now {len(after)}")
        txt = wp.page_text(300).replace("\n", " ")
        print("page text sample:", txt[:160])
except Exception as e:
    print("FAIL", type(e).__name__, e); ok = False
finally:
    wp.close()
print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
