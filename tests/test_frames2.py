#!/usr/bin/env python3
"""
Design rule: a false "unchanged" actively misleads the agent and is unacceptable.
A false "changed" only suppresses a hint and is harmless. Tests encode that asymmetry.
Harder cases for the frame comparator, aimed at realistic desktop changes
that are visually small. False "unchanged" is the harmful direction."""
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

def page(**kw):
    img = Image.new("RGB", (1920, 1080), (250, 250, 252))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1920, 36], fill=(28, 28, 32))
    d.text((1780, 10), kw.get("clock", "14:03"), fill=(255, 255, 255))
    d.rectangle([80, 120, 900, 200], fill=(255, 255, 255), outline=(200, 200, 205))
    d.text((100, 150), kw.get("field", ""), fill=(10, 10, 10))
    if kw.get("dropdown"):
        d.rectangle([80, 200, 500, 420], fill=(255, 255, 255), outline=(180, 180, 190))
        for i in range(4):
            d.text((100, 220 + i*45), f"suggestion {i}", fill=(20, 20, 20))
    if kw.get("checked"):
        d.rectangle([1200, 300, 1224, 324], fill=(40, 120, 220))
    else:
        d.rectangle([1200, 300, 1224, 324], outline=(120, 120, 130))
    if kw.get("spinner"):
        d.ellipse([1500, 700, 1530, 730], outline=(90, 90, 200), width=4)
    b = io.BytesIO(); img.save(b, format="JPEG", quality=70)
    return E._frame_signature(b.getvalue())

base = page()
cases = [
    ("clock tick only -> unchanged",              page(clock="14:04"), True),
    # A spinner means the page is loading — a real state change. Flagging it as
    # changed only suppresses the hint, which is harmless.
    ("spinner appears -> CHANGED (page loading; safe direction)", page(spinner=True), False),
    ("autocomplete dropdown opens -> CHANGED",    page(dropdown=True), False),
    ("text typed into field -> CHANGED",          page(field="hello world"), False),
    ("small checkbox ticked -> CHANGED",          page(checked=True),  False),
]
ok = True
for label, other, expect_same in cases:
    got = E._frames_equivalent(base, other)
    good = got == expect_same
    ok &= good
    print(("PASS " if good else "FAIL ") + label + f"  (equivalent={got})")
print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
