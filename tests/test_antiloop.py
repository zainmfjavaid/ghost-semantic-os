#!/usr/bin/env python3
"""Offline test of the anti-loop logic. No VM required: feeds synthetic frames
through the real _encode/_compress code paths on the host."""
import sys, io, types
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))
from PIL import Image
ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)
desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = object
desktop.desktop_env = desktop_module
sys.modules["desktop_env"] = desktop
sys.modules["desktop_env.desktop_env"] = desktop_module
import server as envserver

def frame(color):
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), color).save(buf, format="PNG")
    return buf.getvalue()

red, red2, blue = frame((200, 0, 0)), frame((200, 0, 0)), frame((0, 0, 200))
entry = {"som": False}
ok = True

# 1. first frame: no "unchanged" claim
p1 = envserver._encode({"screenshot": red}, entry)
assert "screen_unchanged" not in p1, "first frame must not claim unchanged"
print("PASS first frame not flagged")

# 2. identical frame -> flagged
p2 = envserver._encode({"screenshot": red2}, entry)
if not p2.get("screen_unchanged"):
    print("FAIL identical frame not detected"); ok = False
else:
    print("PASS identical frame detected, streak=%s" % p2.get("unchanged_streak"))

# 3. streak escalates
p3 = envserver._encode({"screenshot": red}, entry)
if p3.get("unchanged_streak") != 2:
    print("FAIL streak did not escalate: %s" % p3.get("unchanged_streak")); ok = False
else:
    print("PASS streak escalates to 2")

# 4. changed frame clears it
p4 = envserver._encode({"screenshot": blue}, entry)
if p4.get("screen_unchanged"):
    print("FAIL changed frame wrongly flagged"); ok = False
else:
    print("PASS changed frame clears the flag")

# 5. compression still valid + guards bad input
c = envserver._compress(red)
assert c and c[1] == "image/jpeg", "compression should yield jpeg"
print("PASS compression -> %s, %d bytes" % (c[1], len(c[0])))
if envserver._compress(b"") is not None:
    print("FAIL empty bytes not rejected"); ok = False
else:
    print("PASS empty frame rejected")
if envserver._compress(b"not-an-image-at-all") is not None:
    print("FAIL corrupt bytes not rejected"); ok = False
else:
    print("PASS corrupt frame rejected")

print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
