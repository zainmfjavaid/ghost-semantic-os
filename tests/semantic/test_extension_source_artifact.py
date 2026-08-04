from __future__ import annotations

import hashlib
import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from envserver.semantic.adapters import AdapterContext
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.research_adapter import PublicHTTPTransport, StreamResponse
from envserver.semantic.runtime import LibreOfficeGuestProxyAdapter
from envserver.semantic.source_artifact import StagedSourceArtifact
from guest_agent import semantic_agent
from guest_agent.semantic_agent import CAPABILITIES


def public_resolver(host: str, port: int, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class _Response:
    def __init__(self, status: int, headers=(), chunks=()) -> None:
        self.status = status
        self._headers = list(headers)
        self._chunks = list(chunks)

    def getheaders(self):
        return self._headers

    def read(self, _size: int):
        return self._chunks.pop(0) if self._chunks else b""


class _Connection:
    responses: list[_Response] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.response = self.responses.pop(0)

    def request(self, *_args, **_kwargs) -> None:
        pass

    def getresponse(self):
        return self.response

    def close(self) -> None:
        pass


class PublicArtifactStreamTests(unittest.TestCase):
    def test_stream_hashes_chunks_without_retaining_a_body(self) -> None:
        _Connection.responses = [_Response(
            200,
            [("Content-Type", "application/octet-stream"), ("Content-Length", "6")],
            [b"abc", b"def"],
        )]
        chunks: list[bytes] = []
        with patch(
            "envserver.semantic.research_adapter._PinnedHTTPSConnection", _Connection
        ):
            result = PublicHTTPTransport(resolver=public_resolver).stream(
                "https://example.com/extension.oxt", chunks.append, max_bytes=10
            )
        self.assertEqual(chunks, [b"abc", b"def"])
        self.assertEqual(result.size, 6)
        self.assertEqual(result.content_hash, hashlib.sha256(b"abcdef").hexdigest())
        self.assertFalse(hasattr(result, "body"))

    def test_stream_revalidates_redirect_and_rejects_private_destination(self) -> None:
        _Connection.responses = [_Response(
            302, [("Location", "http://127.0.0.1/extension.oxt")]
        )]
        with patch(
            "envserver.semantic.research_adapter._PinnedHTTPSConnection", _Connection
        ), self.assertRaises(ProtocolError) as caught:
            PublicHTTPTransport(resolver=public_resolver).stream(
                "https://example.com/start", lambda _chunk: None, max_bytes=10
            )
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_declared_oversize_is_rejected_before_any_sink_mutation(self) -> None:
        _Connection.responses = [_Response(
            200, [("Content-Length", "11")], [b"not-delivered"]
        )]
        chunks: list[bytes] = []
        with patch(
            "envserver.semantic.research_adapter._PinnedHTTPSConnection", _Connection
        ), self.assertRaises(ProtocolError) as caught:
            PublicHTTPTransport(resolver=public_resolver).stream(
                "https://example.com/large.oxt", chunks.append, max_bytes=10
            )
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertEqual(chunks, [])


class _Stager:
    def __init__(self) -> None:
        self.removed = False

    def stage_libreoffice_extension(self, source_url: str) -> StagedSourceArtifact:
        return StagedSourceArtifact(
            path="/home/user/Downloads/.ghost-semantic/source.oxt",
            sha256="a" * 64,
            size=123,
            requested_url=source_url,
            final_url="https://cdn.example/extension.oxt",
            http_status=200,
            fetched_at="2026-01-01T00:00:00Z",
            redirect_chain=(source_url,),
            content_type="application/vnd.openofficeorg.extension",
        )

    def remove(self, _staged: StagedSourceArtifact) -> bool:
        self.removed = True
        return True


class _Guest:
    def __init__(self) -> None:
        self.install_arguments = None

    def request(self, method, path, payload=None):
        if method == "POST" and path == "/v1/act":
            self.install_arguments = dict(payload["arguments"])
            return {"ok": True, "result": {
                "execution_path": "native_api",
                "identifier": "org.example.extension",
                "version": "2.0",
                "installed": True,
                "postcondition": {
                    "registry": "unopkg",
                    "identifier": "org.example.extension",
                    "version": "2.0",
                    "installed": True,
                },
            }}
        raise AssertionError((method, path, payload))


class LibreOfficeSourceCompositionTests(unittest.TestCase):
    def adapter(self):
        descriptor = next(
            item for item in CAPABILITIES
            if item["adapter_id"] == "libreoffice.uno@1"
        )
        guest = _Guest()
        stager = _Stager()
        return LibreOfficeGuestProxyAdapter(
            descriptor, guest.request, source_stager=stager
        ), guest, stager

    def test_schema_requires_exactly_one_local_path_or_public_source(self) -> None:
        adapter, _guest, _stager = self.adapter()
        schema = adapter.descriptor()["action_schemas"]["install_extension"][
            "arguments_schema"
        ]
        self.assertIn("source_url", schema["properties"])
        for invalid in ({}, {"path": "/tmp/a.oxt", "source_url": "https://x/a.oxt"}):
            with self.subTest(arguments=invalid), self.assertRaises(ProtocolError):
                adapter.validate_arguments("install_extension", invalid)
        adapter.validate_arguments(
            "install_extension", {"source_url": "https://example.com/a.oxt"}
        )

    def test_source_is_staged_installed_verified_and_removed_with_provenance(self) -> None:
        adapter, guest, stager = self.adapter()
        result = adapter.act(
            AdapterContext("episode", "libreoffice.extensions", "request", None),
            {
                "action": "install_extension",
                "arguments": {"source_url": "https://example.com/a.oxt"},
            },
        )
        self.assertEqual(
            guest.install_arguments,
            {"path": "/home/user/Downloads/.ghost-semantic/source.oxt"},
        )
        self.assertTrue(stager.removed)
        self.assertTrue(result.result["installed"])
        self.assertTrue(result.result["source_artifact"]["staged_artifact_removed"])
        self.assertEqual(result.provenance[-1]["extension_identifier"], "org.example.extension")
        self.assertEqual(result.provenance[-1]["registry"], "unopkg")

    def test_guest_stages_oxt_to_disk_and_exposes_an_empty_registry_owner(self) -> None:
        description = b"""<?xml version='1.0'?>
<description xmlns='http://openoffice.org/extensions/description/2006'>
  <identifier value='org.example.streamed'/><version value='1.0'/>
</description>"""
        encoded = io.BytesIO()
        with zipfile.ZipFile(encoded, "w") as archive:
            archive.writestr("description.xml", description)
        content = encoded.getvalue()

        with tempfile.TemporaryDirectory() as raw, patch.object(
            semantic_agent.Path, "home", return_value=Path(raw)
        ):
            path = Path(raw) / "Downloads/.ghost-semantic/streamed.oxt"
            transfer_id = "streamed_extension_123456"
            first = semantic_agent.act({
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer_id,
                    "offset": 0,
                    "path": str(path),
                    "artifact_kind": "libreoffice_extension",
                    "base64": base64.b64encode(content[:50]).decode("ascii"),
                    "final": False,
                },
            })
            self.assertFalse(first["complete"])
            private = semantic_agent.STATE.blob_staging[transfer_id]
            self.assertNotIn("data", private)
            self.assertTrue(private["temporary"].is_file())
            final = semantic_agent.act({
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer_id,
                    "offset": 50,
                    "base64": base64.b64encode(content[50:]).decode("ascii"),
                    "final": True,
                },
            })
            self.assertEqual(final["artifact"]["identifier"], "org.example.streamed")
            self.assertEqual(final["sha256"], hashlib.sha256(content).hexdigest())
            semantic_agent.act({
                "action": "remove_staged_artifact",
                "arguments": {"path": str(path), "expected_hash": final["sha256"]},
            })
            self.assertFalse(path.exists())

        with patch.object(
            semantic_agent, "_libreoffice_extension_records", return_value=[]
        ):
            queried = semantic_agent._query_libreoffice_extensions({
                "resource": "libreoffice.extensions",
                "where": {}, "parameters": {}, "limit": 30,
            })
        records = queried["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "libreoffice.extension_registry")
        self.assertEqual(records[0]["advertised_actions"], ["install_extension"])


if __name__ == "__main__":
    unittest.main()
