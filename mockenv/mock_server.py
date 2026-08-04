"""A tiny stand-in for the OSWorld env server.

Purpose: exercise the real agent harness end-to-end without an OSWorld VM, so
harness changes can be verified while AWS is unavailable. It speaks the same
endpoints and returns the same fields (screenshot, elements, screen_unchanged,
repeated_action), and it renders a trivial two-button "app" with PIL.

This is a TEST FIXTURE, never an evaluation surface. Nothing it produces is a
benchmark number — OSWorld tasks are the only thing allowed to score the agent.
It exists to answer one question: when the agent repeats a useless action, does
the anti-loop feedback actually reach it and change what it does next?
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image, ImageDraw

W, H = 1920, 1080   # must match the resolution stated in the system prompt
# The decoy is a large tempting button that does nothing; the real one is small.
DECOY = (120, 240, 900, 440)
REAL = (1280, 820, 1680, 920)

EPISODES: dict[str, dict] = {}


def render(state: dict) -> bytes:
    img = Image.new("RGB", (W, H), (245, 245, 248))
    d = ImageDraw.Draw(img)
    d.text((80, 90), "Mock app — click the Submit button to finish", fill=(20, 20, 20))
    d.rectangle(DECOY, fill=(210, 60, 60))
    d.text((DECOY[0] + 40, DECOY[1] + 90), "Big Red Button (does nothing)", fill=(255, 255, 255))
    d.rectangle(REAL, fill=(40, 140, 70) if not state["done"] else (90, 90, 90))
    d.text((REAL[0] + 150, REAL[1] + 40), "Submit", fill=(255, 255, 255))
    if state["done"]:
        d.text((140, 620), "SUBMITTED", fill=(20, 120, 40))
    # Deliberately NOT rendering a click counter: a failed click must leave the
    # screen genuinely unchanged, which is the case the intervention targets.
    # A ticking clock is rendered instead, to mimic the real desktop nuisance.
    d.rectangle([0, 0, W, 36], fill=(30, 30, 35))
    d.text((W - 140, 10), state["clock"], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _signature(image_bytes: bytes):
    """Same approach as the real env server: mask the top bar, compare RGB."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cut = int(img.height * 0.04)
    img = img.crop((0, cut, img.width, img.height)).resize((128, 128))
    return list(img.getdata())


def _equivalent(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return False
    diff = sum(1 for p, q in zip(a, b)
               if abs(p[0]-q[0]) > 10 or abs(p[1]-q[1]) > 10 or abs(p[2]-q[2]) > 10)
    return diff / len(a) <= 0.0002


def observe(ep: dict) -> dict:
    shot = render(ep["state"])
    digest = _signature(shot)
    payload: dict = {
        "screenshot": base64.b64encode(shot).decode(),
        "media_type": "image/jpeg",
        "steps": ep["state"]["clicks"],
        "done": ep["state"]["done"],
    }
    if _equivalent(ep.get("last_digest"), digest):
        ep["unchanged"] = ep.get("unchanged", 0) + 1
        payload["screen_unchanged"] = True
        payload["unchanged_streak"] = ep["unchanged"]
    else:
        ep["unchanged"] = 0
    ep["last_digest"] = digest
    payload["elements"] = (
        f"0\tpush button\tBig Red Button\t\"\"\n1\tpush button\tSubmit\t\"\""
    )
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the test output readable
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send({"ok": True, "episodes": list(EPISODES)})
        if self.path.endswith("/obs"):
            ep = EPISODES[self.path.split("/")[2]]
            return self._send(observe(ep))
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/episodes":
            eid = uuid.uuid4().hex[:12]
            EPISODES[eid] = {"state": {"clicks": 0, "done": False, "clock": "14:00"},
                             "cmds": []}
            return self._send({
                "episode_id": eid,
                "instruction": "Click the Submit button. Only the Submit button finishes "
                               "this task; the big red button does nothing.",
                "task_id": "mock-001", "domain": "mock",
                **observe(EPISODES[eid]),
            })
        parts = self.path.split("/")
        eid = parts[2] if len(parts) > 2 else None
        ep = EPISODES.get(eid)
        if ep is None:
            return self._send({"error": "unknown episode"}, 404)

        if self.path.endswith("/evaluate"):
            return self._send({"score": 1.0 if ep["state"]["done"] else 0.0,
                               "steps": ep["state"]["clicks"]})

        if self.path.endswith("/element"):
            idx = body.get("index")
            cmd = f"element:{idx}:{body.get('action')}"
            hit_real = idx == 1
        else:
            cmd = body.get("command", "")
            hit_real = _hits(cmd, REAL)

        repeat = 0
        if ep["cmds"] and ep["cmds"][-1] == cmd:
            repeat = ep.get("repeat", 0) + 1
        ep["repeat"] = repeat
        ep["cmds"].append(cmd)
        ep["state"]["clicks"] += 1
        # advance the clock every action, as a real desktop would
        mm = 60 * 14 + ep["state"]["clicks"]
        ep["state"]["clock"] = "%02d:%02d" % (mm // 60, mm % 60)
        if hit_real:
            ep["state"]["done"] = True

        out = observe(ep)
        if repeat:
            out["repeated_action"] = repeat
        self._send(out)

    def do_DELETE(self):
        EPISODES.pop(self.path.split("/")[2], None)
        self._send({"closed": True})


def _hits(cmd: str, box) -> bool:
    import re
    m = re.search(r"click\((\d+)\s*,\s*(\d+)", cmd)
    if not m:
        return False
    x, y = int(m.group(1)), int(m.group(2))
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


if __name__ == "__main__":
    print("mock env server on http://127.0.0.1:8099", flush=True)
    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
