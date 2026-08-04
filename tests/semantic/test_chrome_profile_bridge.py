from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from envserver.semantic.chrome_adapter import ChromeSemanticAdapter
from envserver.semantic.adapters import AdapterContext
from envserver.semantic.protocol import ErrorCode, ProtocolError, SideEffectState
from guest_agent import semantic_agent


class _BrowserStub:
    pass


class _AsyncPage:
    def __init__(self, *, available: bool, mutation_error: Exception | None = None):
        self.available = available
        self.mutation_error = mutation_error

    async def goto(self, *_args, **_kwargs):
        return None

    async def evaluate(self, expression, _argument=None):
        if expression == "capability":
            return self.available
        if self.mutation_error is not None:
            raise self.mutation_error
        return True

    async def close(self):
        return None


class _AsyncContext:
    def __init__(self, page: _AsyncPage):
        self.page = page

    async def new_page(self):
        return self.page


class _AsyncBrowser:
    def __init__(self, page: _AsyncPage):
        self.context = _AsyncContext(page)

    async def _context(self):
        return self.context


class _FailingCallBrowser:
    def _call(self, coroutine):
        coroutine.close()
        raise ProtocolError(ErrorCode.TIMEOUT, "call timed out", retryable=True)


class _RelaunchPage:
    url = "https://example.com/"

    def is_closed(self):
        return False


class _RelaunchBrowser:
    def __init__(self):
        self.context = type("Context", (), {"pages": [_RelaunchPage()]})()
        self.reconnects = 0

    async def _context(self):
        return self.context

    async def _reconnect_after_browser_restart(self):
        self.reconnects += 1


class ChromeProfileGuestBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.profile = Path(self.temporary.name)
        (self.profile / "Preferences").write_text(json.dumps({
            "browser": {"show_bookmark_bar": True},
            "identity": {"access_token": "must-not-leak"},
            "extensions": {"settings": {
                "abcdefghijklmnop": {
                    "state": 1,
                    "location": 4,
                    "path": "/home/oai/share/extension",
                    "manifest": {"name": "Fixture", "version": "1.0"},
                }
            }},
        }), encoding="utf-8")
        (self.profile / "Bookmarks").write_text(json.dumps({
            "roots": {
                "bookmark_bar": {
                    "id": "1", "name": "Bookmarks bar", "type": "folder",
                    "children": [{
                        "id": "2", "name": "Example", "type": "url",
                        "url": "https://example.com/?token=private",
                    }],
                }
            }
        }), encoding="utf-8")
        connection = sqlite3.connect(self.profile / "History")
        connection.executescript("""
          CREATE TABLE urls(url TEXT,title TEXT,visit_count INTEGER,last_visit_time INTEGER);
          INSERT INTO urls VALUES('https://example.com','Example',2,123);
          CREATE TABLE downloads(
            id INTEGER,current_path TEXT,target_path TEXT,tab_url TEXT,
            total_bytes INTEGER,received_bytes INTEGER,state INTEGER,
            danger_type INTEGER,start_time INTEGER
          );
          INSERT INTO downloads VALUES(
            7,'/home/oai/share/a.pdf','/home/oai/share/a.pdf',
            'https://example.com/a.pdf',10,10,1,0,100
          );
        """)
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def query(self, kind: str):
        with mock.patch.object(
            semantic_agent,
            "_chrome_profile_context",
            return_value=(123, ["google-chrome", "--remote-debugging-port=9222"], self.profile),
        ):
            return semantic_agent._query_chrome_private({
                "parameters": {"kind": kind}, "limit": 100,
            })

    def test_bookmarks_extensions_history_and_downloads_are_structured(self) -> None:
        bookmarks = self.query("bookmarks")["records"]
        self.assertEqual([item["id"] for item in bookmarks], ["1", "2"])
        extensions = self.query("extensions")["records"]
        self.assertEqual(extensions[0]["name"], "Fixture")
        self.assertTrue(extensions[0]["enabled"])
        history = self.query("history")["records"]
        self.assertEqual(history[0]["visit_count"], 2)
        downloads = self.query("downloads")["records"]
        self.assertEqual(downloads[0]["bytes_received"], 10)

    def test_sensitive_preference_values_are_never_returned(self) -> None:
        settings = self.query("settings")["records"]
        encoded = json.dumps(settings)
        self.assertNotIn("must-not-leak", encoded)
        secret = next(
            item for item in settings if item["key"] == "identity.access_token"
        )
        self.assertEqual(secret["value"], "[redacted]")
        self.assertTrue(secret["secret_value_redacted"])

    def test_outer_private_pager_assembles_stable_collection(self) -> None:
        records = [{"kind": "profile", "index": index} for index in range(225)]

        def guest_request(_method, _path, payload):
            offset = int(payload.get("internal_offset", 0))
            page = records[offset:offset + 100]
            return {
                "ok": True,
                "result": {
                    "records": page,
                    "revision": "profile-1",
                    "next_internal_offset": offset + 100
                    if offset + 100 < len(records) else None,
                },
            }

        adapter = ChromeSemanticAdapter(_BrowserStub(), guest_request)  # type: ignore[arg-type]
        self.assertEqual(len(adapter._profile_query("settings")), 225)

    def test_missing_capability_is_proven_before_mutation(self) -> None:
        adapter = ChromeSemanticAdapter(  # type: ignore[arg-type]
            _AsyncBrowser(_AsyncPage(available=False)), lambda *_args: {}
        )
        with self.assertRaises(ProtocolError) as caught:
            asyncio.run(adapter._internal(
                "chrome://settings/", "mutation", mutation=True,
                capability_expression="capability",
                missing_capability="chrome.settingsPrivate.setPref",
            ))
        self.assertEqual(caught.exception.code, ErrorCode.REPRESENTATION_GAP)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.NONE)

    def test_mutation_exception_is_uncertain(self) -> None:
        adapter = ChromeSemanticAdapter(  # type: ignore[arg-type]
            _AsyncBrowser(_AsyncPage(available=True, mutation_error=RuntimeError("lost"))),
            lambda *_args: {},
        )
        with self.assertRaises(ProtocolError) as caught:
            asyncio.run(adapter._internal(
                "chrome://settings/", "mutation", mutation=True,
                capability_expression="capability",
                missing_capability="chrome.settingsPrivate.setPref",
            ))
        self.assertEqual(caught.exception.code, ErrorCode.UNCERTAIN)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.UNKNOWN)

    def test_outer_action_timeout_is_never_reported_safe_to_retry(self) -> None:
        adapter = ChromeSemanticAdapter(_FailingCallBrowser(), lambda *_args: {})  # type: ignore[arg-type]
        with self.assertRaises(ProtocolError) as caught:
            adapter.act(
                AdapterContext("episode", "chrome.settings", "request", None),
                {"target": {"ref": "x"}, "action": "set_pref", "arguments": {}},
            )
        self.assertEqual(caught.exception.code, ErrorCode.UNCERTAIN)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.UNKNOWN)

    def test_load_unpacked_uses_native_relaunch_and_reconnects_cdp(self) -> None:
        browser = _RelaunchBrowser()

        def guest_request(method, path, payload):
            self.assertEqual((method, path), ("POST", "/v1/act"))
            self.assertEqual(payload["action"], "chrome_load_unpacked")
            self.assertEqual(payload["arguments"]["restore_urls"], ["https://example.com/"])
            return {"ok": True, "result": {
                "execution_path": "native_api", "guarded_relaunch": True,
                "extension_id": "abcdefghijklmnop", "name": "Fixture",
                "path": "/home/user/Desktop/Fixture", "enabled": True,
            }}

        adapter = ChromeSemanticAdapter(browser, guest_request)  # type: ignore[arg-type]
        with mock.patch.object(
            adapter,
            "_extensions",
            new=mock.AsyncMock(return_value=[{
                "id": "abcdefghijklmnop", "name": "Fixture", "enabled": True,
            }]),
        ):
            result = asyncio.run(adapter._load_unpacked_via_guarded_relaunch(
                "/home/user/Desktop/Fixture"
            ))
        self.assertTrue(result["guarded_relaunch"])
        self.assertTrue(result["live_registry_verified"])
        self.assertEqual(browser.reconnects, 1)
        self.assertEqual(
            adapter._loaded_extension_paths["abcdefghijklmnop"],
            "/home/user/Desktop/Fixture",
        )


if __name__ == "__main__":
    unittest.main()
