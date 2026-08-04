"""Black-box security and filesystem canaries for the guest semantic daemon."""
from __future__ import annotations

import json
import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENT = REPO / "guest_agent/semantic_agent.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class GuestSemanticAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.token = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment["GHOST_SEMANTIC_TOKEN"] = cls.token
        environment["GHOST_SEMANTIC_PORT"] = str(cls.port)
        environment["GHOST_SEMANTIC_BUNDLE_HASH"] = "b" * 64
        cls.process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        for _ in range(80):
            try:
                cls.call("GET", "/v1/health")
                break
            except Exception:
                if cls.process.poll() is not None:
                    out, err = cls.process.communicate(timeout=1)
                    raise RuntimeError(f"guest agent exited: {out!r} {err!r}")
                time.sleep(0.05)
        else:
            cls.process.terminate()
            raise RuntimeError("guest agent did not become healthy")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.call("POST", "/v1/shutdown", {})
        finally:
            cls.process.wait(timeout=5)
            if cls.process.stdout:
                cls.process.stdout.close()
            if cls.process.stderr:
                cls.process.stderr.close()

    @classmethod
    def call(cls, method: str, path: str, payload=None, *, token=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {cls.token if token is None else token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response, json.loads(response.read())

    def test_auth_health_and_capabilities(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.call("GET", "/v1/health", token="wrong")
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()
        response, health = self.call("GET", "/v1/health")
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertTrue(health["ok"])
        result = health["result"]
        self.assertEqual(result["agent_version"], "1.0.0-alpha.1")
        self.assertEqual(result["bundle_hash"], "b" * 64)
        self.assertIn(result["guest_platform"], {"linux", "darwin", "windows"})
        _, capabilities = self.call("GET", "/v1/capabilities")
        encoded = json.dumps(capabilities)
        self.assertNotIn("screenshot", encoded.casefold())
        self.assertNotIn("coordinate", encoded.casefold())

    def test_versioned_native_adapter_routes_are_authenticated_and_typed(self) -> None:
        # The local macOS test runner has no python-dbus; a real guest should
        # report healthy, while this environment must still prove that the
        # versioned bridge module was loaded and failed through its typed
        # native dependency check rather than a missing HTTP route.
        try:
            _, health = self.call("GET", "/v1/adapters/mpris-media@1/health")
        except urllib.error.HTTPError as unavailable:
            self.assertEqual(unavailable.code, 400)
            health = json.loads(unavailable.read())
            unavailable.close()
            self.assertEqual(health["error"]["code"], "adapter_unavailable")
            self.assertIn("dbus", health["error"]["message"])
        else:
            self.assertTrue(health["ok"])
            self.assertEqual(health["result"]["adapter_id"], "mpris-media@1")
            self.assertEqual(health["result"]["execution_path"], "native_api")

        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.call("GET", "/v1/adapters/thunderbird-extension@1/health")
        self.assertEqual(missing.exception.code, 400)
        payload = json.loads(missing.exception.read())
        missing.exception.close()
        self.assertEqual(payload["error"]["code"], "representation_gap")
        self.assertEqual(payload["error"]["side_effect_state"], "none")

    def test_filesystem_query_and_atomic_text_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("before", encoding="utf-8")
            _, queried = self.call("POST", "/v1/query", {
                "resource": "filesystem.entries",
                "scope": {"path": directory},
                "where": {},
                "parameters": {},
                "limit": 30,
            })
            records = queried["result"]["records"]
            self.assertEqual([record["name"] for record in records], ["source.txt"])
            output = root / "output.txt"
            _, acted = self.call("POST", "/v1/act", {
                "target": {"resource": "filesystem.entries"},
                "action": "write_text",
                "arguments": {"path": str(output), "content": "after"},
            })
            self.assertTrue(acted["ok"])
            self.assertEqual(output.read_text(encoding="utf-8"), "after")
            self.assertEqual(len(acted["result"]["sha256"]), 64)

    def test_structural_parse_and_expected_hash_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_text('{"before":true}', encoding="utf-8")
            _, file_query = self.call("POST", "/v1/query", {
                "resource": "filesystem.file", "scope": {"path": str(artifact)},
                "where": {}, "parameters": {}, "limit": 30,
            })
            advertised_hash = file_query["result"]["records"][0]["sha256"]
            self.assertEqual(advertised_hash, hashlib.sha256(artifact.read_bytes()).hexdigest())
            with self.assertRaises(urllib.error.HTTPError) as conflict:
                self.call("POST", "/v1/act", {
                    "action": "write_text",
                    "arguments": {"path": str(artifact), "content": '{"after":true}'},
                })
            self.assertEqual(conflict.exception.code, 400)
            conflict_payload = json.loads(conflict.exception.read())
            conflict.exception.close()
            self.assertEqual(conflict_payload["error"]["code"], "artifact_conflict")
            expected = advertised_hash
            _, acted = self.call("POST", "/v1/act", {
                "action": "write_text",
                "arguments": {
                    "path": str(artifact), "content": '{"after":true}',
                    "expected_hash": expected,
                },
            })
            self.assertTrue(acted["ok"])
            _, queried = self.call("POST", "/v1/query", {
                "resource": "artifact.structure", "scope": {"path": str(artifact)},
                "where": {}, "parameters": {}, "limit": 30,
            })
            record = queried["result"]["records"][0]
            self.assertEqual(record["format"], "json")
            self.assertEqual(record["value"], {"after": True})

    def test_non_xhtml_html_is_accepted_and_structurally_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.html"
            _, acted = self.call("POST", "/v1/act", {
                "action": "write_text",
                "arguments": {
                    "path": str(artifact),
                    "content": "<!doctype html><html><body><p>one<br><p>two</body></html>",
                },
            })
            self.assertTrue(acted["ok"])
            _, queried = self.call("POST", "/v1/query", {
                "resource": "artifact.structure",
                "scope": {"path": str(artifact)},
                "where": {}, "parameters": {}, "limit": 30,
            })
            record = queried["result"]["records"][0]
            self.assertEqual(record["format"], "html")
            self.assertEqual(record["root_tag"], "html")
            self.assertIn("one", record["text_excerpt"])

    def test_chunked_binary_commit_is_atomic_and_parseable(self) -> None:
        # The deterministic PNG parser needs only the standard signature and
        # IHDR width/height fields for this transport canary.
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (3).to_bytes(4, "big") + (5).to_bytes(4, "big")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            transfer = "transport_canary_123456"
            first, second = data[:13], data[13:]
            _, staged = self.call("POST", "/v1/act", {
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer, "offset": 0, "path": str(output),
                    "base64": base64.b64encode(first).decode(), "final": False,
                },
            })
            self.assertFalse(staged["result"]["complete"])
            _, committed = self.call("POST", "/v1/act", {
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer, "offset": len(first),
                    "base64": base64.b64encode(second).decode(), "final": True,
                },
            })
            self.assertTrue(committed["result"]["complete"])
            self.assertEqual(output.read_bytes(), data)
            _, queried = self.call("POST", "/v1/query", {
                "resource": "artifact.structure", "scope": {"path": str(output)},
                "where": {}, "parameters": {}, "limit": 30,
            })
            record = queried["result"]["records"][0]
            self.assertEqual((record["width"], record["height"]), (3, 5))
            self.assertEqual(record["visual_derivation"], "deterministic")


if __name__ == "__main__":
    unittest.main()
