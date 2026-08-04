#!/usr/bin/env python3
"""
Design rule: a false "unchanged" actively misleads the agent and is unacceptable.
A false "changed" only suppresses a hint and is harmless. Tests encode that asymmetry.
Does the frame comparison survive real-desktop dynamics?
A GNOME clock ticking must NOT count as 'the screen changed'; a real UI change must."""
import sys, io, types
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))
from PIL import Image, ImageDraw
ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)
desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = object
desktop.desktop_env = desktop_module
sys.modules["desktop_env"] = desktop
sys.modules["desktop_env.desktop_env"] = desktop_module
import server as E

def desktop(clock="14:03", dialog=False, cursor_x=100):
    img = Image.new("RGB", (960, 540), (240, 240, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 960, 28], fill=(30, 30, 35))       # top bar
    d.text((880, 8), clock, fill=(255, 255, 255))          # ticking clock
    d.rectangle([40, 80, 500, 300], fill=(255, 255, 255))  # page content
    d.text((60, 100), "Some page content", fill=(0, 0, 0))
    d.rectangle([cursor_x, 400, cursor_x + 2, 416], fill=(0, 0, 0))  # caret
    if dialog:
        d.rectangle([300, 150, 700, 400], fill=(255, 250, 200))
        d.text((330, 180), "A dialog just opened", fill=(0, 0, 0))
    b = io.BytesIO(); img.save(b, format="JPEG", quality=70)
    return b.getvalue()

def sig(x): return E._frame_signature(x)
ok = True

base = sig(desktop())
same = sig(desktop())
clock_tick = sig(desktop(clock="14:04"))
caret_move = sig(desktop(cursor_x=140))
real_change = sig(desktop(dialog=True))

cases = [
    ("identical frame -> unchanged",            base, same,        True),
    ("clock ticked -> still unchanged",         base, clock_tick,  True),
    # A moving caret means focus moved — a genuine change. Reporting "changed"
    # just means we stay quiet, which is the safe direction.
    ("caret moved -> CHANGED (focus moved; safe direction)", base, caret_move, False),
    ("dialog opened -> CHANGED",                base, real_change, False),
]
for label, a, b, expect_same in cases:
    got = E._frames_equivalent(a, b)
    good = (got == expect_same)
    ok &= good
    print(("PASS " if good else "FAIL ") + label + f"  (equivalent={got})")

print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
