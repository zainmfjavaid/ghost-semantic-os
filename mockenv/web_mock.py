"""Mock env exposing ONLY the /web endpoint, backed by real local Chrome via CDP.
Lets the full harness+model loop be tested on a genuine web page without AWS."""
import base64, io, json, sys, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "envserver"))
from web_provider import WebProvider
from PIL import Image

WP = WebProvider("127.0.0.1", 1337)
EPS = {}

def blank_shot():
    b = io.BytesIO(); Image.new("RGB", (1280, 720), (235, 235, 240)).save(b, "JPEG", quality=60)
    return base64.b64encode(b.getvalue()).decode()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, o, c=200):
        b = json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health": return self._s({"ok": True, "episodes": list(EPS)})
        self._s({"steps": 0, "screenshot": blank_shot(), "media_type": "image/jpeg"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/episodes":
            eid = uuid.uuid4().hex[:12]; EPS[eid] = {"els": []}
            WP.navigate("https://duckduckgo.com")
            return self._s({"episode_id": eid, "task_id": "web-mock", "domain": "chrome",
                            "instruction": "Search DuckDuckGo for 'OSWorld benchmark' and "
                                           "tell me the title of the first result.",
                            "screenshot": blank_shot(), "media_type": "image/jpeg"})
        eid = self.path.split("/")[2]; ep = EPS.get(eid, {"els": []})
        if self.path.endswith("/evaluate"):
            txt = WP.page_text(4000).lower()
            return self._s({"score": 1.0 if "osworld" in txt and "duckduckgo" in txt else 0.0})
        if self.path.endswith("/web"):
            a = body.get("action"); out = {}
            try:
                if a == "elements":
                    ep["els"] = WP.elements(); out["web_elements"] = WP.describe(ep["els"])
                elif a == "navigate":
                    out["result"] = WP.navigate(body.get("url", "about:blank"))
                    ep["els"] = WP.elements(); out["web_elements"] = WP.describe(ep["els"])
                elif a == "text":
                    out["page_text"] = WP.page_text()
                elif a in ("click", "type"):
                    if not ep["els"]: ep["els"] = WP.elements()
                    i = body.get("index", 0)
                    out["result"] = (WP.click(i, ep["els"]) if a == "click"
                                     else WP.type_into(i, body.get("text", ""), ep["els"]))
                    ep["els"] = WP.elements(); out["web_elements"] = WP.describe(ep["els"])
            except Exception as e:
                out["errors"] = [f"{type(e).__name__}: {e}"]
            out.update({"steps": 1, "screenshot": blank_shot(), "media_type": "image/jpeg"})
            return self._s(out)
        self._s({"steps": 1, "screenshot": blank_shot(), "media_type": "image/jpeg"})
    def do_DELETE(self): EPS.pop(self.path.split("/")[2], None); self._s({"closed": True})

print("web mock on :8098", flush=True)
HTTPServer(("127.0.0.1", 8098), H).serve_forever()
