from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from envserver.semantic.adapters import AdapterContext
from envserver.semantic.artifact_adapter import (
    ArtifactFilesystemAdapter,
    ArtifactLeaseRegistry,
    parse_artifact,
    sha256_file,
)
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.runtime import SemanticRuntime
from envserver.semantic.research_adapter import FetchResponse


def _context(resource: str) -> AdapterContext:
    return AdapterContext("episode", resource, "request", None)


def _query(path: str) -> dict:
    return {
        "resource": "filesystem.file",
        "scope": {"path": path},
        "where": {},
        "fields": [],
        "order_by": [],
        "parameters": {},
        "limit": 30,
        "freshness": "live",
    }


class ArtifactAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = ArtifactFilesystemAdapter(self.root, guest_root="/home/user")

    def tearDown(self) -> None:
        self.adapter.close()
        self.temporary.cleanup()

    def _native(self, guest_path: str) -> str:
        observation = self.adapter.observe(_context("filesystem.file"), _query(guest_path))
        return str(observation.items[0]["ref"])

    def test_atomic_create_overwrite_and_stale_hash(self) -> None:
        root_ref = self._native("/home/user")
        created = self.adapter.act(_context("filesystem.file"), {
            "target": {"ref": root_ref}, "action": "write_text",
            "arguments": {"path": "/home/user/result.txt", "text": "first"},
        })
        self.assertTrue(created.changed)
        target = self.root / "result.txt"
        first_hash = sha256_file(target)
        target_ref = self._native("/home/user/result.txt")
        with self.assertRaises(ProtocolError) as missing:
            self.adapter.act(_context("filesystem.file"), {
                "target": {"ref": target_ref}, "action": "write_text",
                "arguments": {"text": "second"},
            })
        self.assertEqual(missing.exception.code, ErrorCode.ARTIFACT_CONFLICT)
        target.write_text("outside change", encoding="utf-8")
        with self.assertRaises(ProtocolError) as stale:
            self.adapter.act(_context("filesystem.file"), {
                "target": {"ref": target_ref}, "action": "write_text",
                "arguments": {"text": "second", "expected_hash": first_hash},
            })
        self.assertEqual(stale.exception.code, ErrorCode.ARTIFACT_CONFLICT)
        current = sha256_file(target)
        result = self.adapter.act(_context("filesystem.file"), {
            "target": {"ref": target_ref}, "action": "write_text",
            "arguments": {"text": "second", "expected_hash": current},
        })
        self.assertTrue(result.changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        self.assertFalse(list(self.root.glob(".result.txt.*")))

    def test_patch_requires_hash_and_live_modified_lease_blocks_disk_edit(self) -> None:
        path = self.root / "document.md"
        path.write_text("alpha beta beta", encoding="utf-8")
        native = self._native("/home/user/document.md")
        with self.assertRaises(ProtocolError) as missing:
            self.adapter.act(_context("filesystem.file"), {
                "target": {"ref": native}, "action": "patch_text",
                "arguments": {"replacements": [{"old": "beta", "new": "done", "count": 2}]},
            })
        self.assertEqual(missing.exception.code, ErrorCode.ARTIFACT_CONFLICT)
        self.adapter.leases.set(path, owner="libreoffice.uno@1", modified=True)
        with self.assertRaises(ProtocolError) as conflict:
            self.adapter.act(_context("filesystem.file"), {
                "target": {"ref": native}, "action": "patch_text",
                "arguments": {
                    "expected_hash": sha256_file(path),
                    "replacements": [{"old": "beta", "new": "done", "count": 2}],
                },
            })
        self.assertEqual(conflict.exception.code, ErrorCode.ARTIFACT_CONFLICT)
        self.adapter.leases.clear(path)
        result = self.adapter.act(_context("filesystem.file"), {
            "target": {"ref": native}, "action": "patch_text",
            "arguments": {
                "expected_hash": sha256_file(path),
                "replacements": [{"old": "beta", "new": "done", "count": 2}],
            },
        })
        self.assertTrue(result.changed)
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha done done")

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "escape").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(ProtocolError) as caught:
                self.adapter.observe(_context("filesystem.file"), _query("/home/user/escape/secret.txt"))
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_large_text_is_server_side_handle(self) -> None:
        (self.root / "large.txt").write_text("x" * 5_000, encoding="utf-8")
        observation = self.adapter.observe(_context("filesystem.file"), _query("/home/user/large.txt"))
        record = observation.items[0]
        self.assertTrue(record["content_truncated"])
        stored = self.adapter.handles.get(record["data_handle"], kind="artifact.text")
        self.assertEqual("".join(chunk["text"] for chunk in stored.records), "x" * 5_000)

    def test_structural_parsers_for_ooxml_odf_and_pdf(self) -> None:
        docx = self.root / "a.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>""")
        self.assertEqual(parse_artifact(docx)["paragraphs"][0]["text"], "Hello")

        xlsx = self.root / "a.xlsx"
        with zipfile.ZipFile(xlsx, "w") as archive:
            archive.writestr("xl/workbook.xml", """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""")
            archive.writestr("xl/_rels/workbook.xml.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>""")
            archive.writestr("xl/worksheets/sheet1.xml", """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>42</v></c><c r="B1"><f>A1*2</f><v>84</v></c></row></sheetData></worksheet>""")
        workbook = parse_artifact(xlsx)
        self.assertEqual(workbook["sheets"][0]["cells"][0]["value"], 42)
        self.assertEqual(workbook["sheets"][0]["cells"][1]["formula"], "A1*2")

        pptx = self.root / "a.pptx"
        with zipfile.ZipFile(pptx, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>""")
        self.assertEqual(parse_artifact(pptx)["slides"][0]["text"], "Slide text")

        odt = self.root / "a.odt"
        with zipfile.ZipFile(odt, "w") as archive:
            archive.writestr("content.xml", """<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>ODF text</text:p></office:text></office:body></office:document-content>""")
        self.assertEqual(parse_artifact(odt)["paragraphs"][0]["text"], "ODF text")

        pdf = self.root / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj<</Type /Page>>endobj\n%%EOF")
        self.assertEqual(parse_artifact(pdf)["page_count"], 1)

    def test_runtime_reobservation_keeps_scoped_file_ref_actionable(self) -> None:
        path = self.root / "runtime.txt"
        path.write_text("before", encoding="utf-8")
        runtime = SemanticRuntime(
            episode_id="episode", max_tool_calls=10,
            guest_request=lambda *_args: {}, guest_capabilities=(),
            adapters=(self.adapter,),
        )
        query = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "q", "episode_id": "episode",
            "operation": "query",
            "payload": {
                "resource": "filesystem.file", "scope": {"path": "/home/user/runtime.txt"},
                "where": {}, "fields": [], "order_by": [], "parameters": {},
                "limit": 30, "freshness": "live",
            },
        })
        public_ref = query["result"]["records"][0]["ref"]
        result = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "a", "episode_id": "episode",
            "operation": "act",
            "payload": {
                "target": {"ref": public_ref}, "action": "write_text",
                "arguments": {"text": "after", "expected_hash": sha256_file(path)},
                "preconditions": [], "postconditions": [], "timeout_ms": 10_000,
                "idempotency_key": None, "confirm": True,
            },
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["status"], "applied")
        self.assertEqual(path.read_text(encoding="utf-8"), "after")

    def test_public_download_is_ssrf_checked_committed_and_traced(self) -> None:
        class Transport:
            resolver = staticmethod(lambda host, port, **kwargs: [(2, 1, 6, "", ("93.184.216.34", port))])

            def fetch(self, url: str) -> FetchResponse:
                return FetchResponse(url, url, 200, {"content-type": "text/plain"}, b"downloaded", (), "2026-01-01T00:00:00Z")

        adapter = ArtifactFilesystemAdapter(
            self.root, guest_root="/home/user", http_transport=Transport()
        )
        root_ref = adapter.observe(_context("filesystem.file"), _query("/home/user")).items[0]["ref"]
        result = adapter.act(_context("filesystem.file"), {
            "target": {"ref": root_ref}, "action": "download",
            "arguments": {"url": "https://example.com/file.txt", "path": "/home/user/file.txt"},
        })
        self.assertTrue(result.changed)
        self.assertEqual((self.root / "file.txt").read_text(encoding="utf-8"), "downloaded")
        downloads = adapter.observe(_context("artifact.downloads"), {
            "scope": {}, "parameters": {},
        }).items
        self.assertEqual(downloads[0]["status"], "completed")
        self.assertEqual(downloads[0]["content_hash"], sha256_file(self.root / "file.txt"))


if __name__ == "__main__":
    unittest.main()
