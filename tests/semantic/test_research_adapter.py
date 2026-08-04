from __future__ import annotations

import unittest
import uuid

from envserver.semantic.adapters import AdapterContext
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.research_adapter import (
    FetchResponse,
    PublicResearchAdapter,
    validate_public_url,
)
from envserver.semantic.runtime import SemanticRuntime


def public_resolver(host: str, port: int, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeTransport:
    resolver = staticmethod(public_resolver)

    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        if "duckduckgo" in url:
            body = b'<a class="result__a" href="https://example.com/article">Example source</a>'
            return FetchResponse(url, url, 200, {"content-type": "text/html"}, body, (), "2026-01-01T00:00:00Z")
        body = b"<html><head><title>Article</title></head><body><h1>Section</h1><p>Exact source text.</p></body></html>"
        return FetchResponse(url, url, 200, {"content-type": "text/html", "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"}, body, (), "2026-01-01T00:00:01Z")


class MultiDocumentTransport(FakeTransport):
    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        if "duckduckgo" in url:
            body = b"".join(
                (
                    b'<a class="result__a" href="https://example.com/a">Result A</a>',
                    b'<a class="result__a" href="https://example.com/b">Result B</a>',
                    b'<a class="result__a" href="https://example.com/c">Result C</a>',
                )
            )
            return FetchResponse(
                url, url, 200, {"content-type": "text/html"}, body, (),
                "2026-01-01T00:00:00Z",
            )
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        body = (
            f"<html><head><title>Document {slug.upper()}</title></head>"
            f"<body><h1>Section {slug.upper()}</h1><p>Source text {slug}.</p></body></html>"
        ).encode()
        return FetchResponse(
            url,
            url,
            200,
            {
                "content-type": "text/html",
                "last-modified": f"Mon, 0{ord(slug) - 96} Jan 2024 00:00:00 GMT",
            },
            body,
            (),
            f"2026-01-01T00:00:0{ord(slug) - 96}Z",
        )


class LargeSearchTransport(FakeTransport):
    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        if "duckduckgo" in url:
            body = "".join(
                f'<a class="result__a" href="https://example.com/{index}">'
                f'{"Long result title " * 24}{index}</a>'
                for index in range(50)
            ).encode()
            return FetchResponse(
                url, url, 200, {"content-type": "text/html"}, body, (),
                "2026-01-01T00:00:00Z",
            )
        return FetchResponse(
            url, url, 200, {"content-type": "text/plain"}, b"source", (),
            "2026-01-01T00:00:01Z",
        )


def _context(resource: str) -> AdapterContext:
    return AdapterContext("episode", resource, "request", None)


def _payload(resource: str, parameters: dict) -> dict:
    return {
        "resource": resource, "scope": {}, "where": {}, "fields": [],
        "order_by": [], "parameters": parameters, "limit": 100,
        "freshness": "live",
    }


class ResearchAdapterTests(unittest.TestCase):
    def test_blocks_private_special_and_credential_urls(self) -> None:
        for url in (
            "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data",
            "http://[::1]/", "file:///etc/passwd", "https://user:pass@example.com/",
        ):
            with self.subTest(url=url), self.assertRaises(ProtocolError) as caught:
                validate_public_url(url, resolver=public_resolver)
            self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_search_and_document_collections_remain_handle_queryable(self) -> None:
        adapter = PublicResearchAdapter(transport=FakeTransport(), resolver=public_resolver)
        search = adapter.observe(_context("research.search"), _payload("research.search", {"queries": ["semantic agents"]}))
        self.assertEqual(search.items[0]["title"], "Example source")
        search_handle = search.items[0]["collection_handle"]
        again = adapter.observe(_context("research.results"), _payload("research.results", {"collection_handle": search_handle}))
        self.assertEqual(again.items[0]["url"], "https://example.com/article")

        sources = adapter.observe(_context("research.documents"), _payload("research.documents", {"urls": ["https://example.com/article"]}))
        source = sources.items[0]
        self.assertEqual(source["http_status"], 200)
        self.assertTrue(source["content_hash"])
        self.assertEqual(source["source_excerpt"], "Exact source text.")
        self.assertEqual(source["redirect_chain"], [])
        self.assertEqual(source["temporal_scope"], "Mon, 01 Jan 2024 00:00:00 GMT")
        handle = source["collection_handle"]
        chunks = adapter.observe(_context("research.documents"), _payload("research.documents", {"collection_handle": handle}))
        self.assertEqual(chunks.items[0]["text"], "Exact source text.")
        recovered_sources = adapter.observe(_context("research.sources"), _payload("research.sources", {"collection_handle": handle}))
        self.assertEqual(recovered_sources.items[0]["url"], "https://example.com/article")

    def test_overflow_and_collection_handles_are_explicit_and_not_interchangeable(self) -> None:
        adapter = PublicResearchAdapter(
            transport=LargeSearchTransport(), resolver=public_resolver,
        )
        runtime = SemanticRuntime(
            episode_id="research-handle-types",
            max_tool_calls=20,
            guest_request=lambda *_args: {},
            guest_capabilities=[],
            adapters=[adapter],
        )

        def query(resource: str, *, parameters=None, scope=None):
            return runtime.dispatch({
                "protocol_version": "1.0",
                "request_id": str(uuid.uuid4()),
                "episode_id": "research-handle-types",
                "operation": "query",
                "payload": {
                    "resource": resource,
                    "scope": scope or {},
                    "where": {}, "fields": [], "order_by": [],
                    "parameters": parameters or {},
                    "limit": 100, "freshness": "live",
                },
            })

        searched = query("research.search", parameters={"queries": ["semantic"]})
        self.assertTrue(searched["result"]["truncated"])
        overflow_handle = searched["result"]["overflow_handle"]
        self.assertEqual(searched["result"]["data_handle"], overflow_handle)
        collection_handle = searched["result"]["records"][0]["collection_handle"]
        self.assertNotEqual(overflow_handle, collection_handle)
        self.assertNotIn("data_handle", searched["result"]["records"][0])

        overflow = query("system.data_handle", scope={"ref": overflow_handle})
        self.assertEqual(overflow["status"], "ok")
        wrong_type = query(
            "research.documents",
            parameters={"collection_handle": overflow_handle, "result_limit": 1},
        )
        self.assertEqual(wrong_type["status"], "failed")
        self.assertEqual(wrong_type["error"]["code"], "not_found")
        fetched = query(
            "research.documents",
            parameters={"collection_handle": collection_handle, "result_limit": 1},
        )
        self.assertEqual(fetched["status"], "ok")
        self.assertIn("collection_handle", fetched["result"]["records"][0])

        # Frozen callers may still supply the old parameter name, but new
        # responses never expose it as an adapter-owned collection identity.
        legacy = query(
            "research.documents",
            parameters={"data_handle": collection_handle, "result_limit": 1},
        )
        self.assertEqual(legacy["status"], "ok")

    def test_search_handle_composes_one_bounded_ordered_document_collection(self) -> None:
        transport = MultiDocumentTransport()
        adapter = PublicResearchAdapter(
            transport=transport, resolver=public_resolver, max_concurrency=2,
        )
        search = adapter.observe(
            _context("research.search"),
            _payload("research.search", {"queries": ["semantic agents"]}),
        )
        search_handle = search.summary["collection_handle"]
        self.assertIsInstance(search_handle, str)
        self.assertEqual(
            [record["url"] for record in search.items],
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
        )

        # A search collection is never crawled implicitly: the caller must
        # select a bounded ordered window.
        with self.assertRaises(ProtocolError) as missing_limit:
            adapter.observe(
                _context("research.documents"),
                _payload("research.documents", {"collection_handle": search_handle}),
            )
        self.assertEqual(missing_limit.exception.code, ErrorCode.INVALID_REQUEST)

        documents = adapter.observe(
            _context("research.documents"),
            _payload(
                "research.documents",
                {
                    "collection_handle": search_handle,
                    "result_offset": 1,
                    "result_limit": 2,
                },
            ),
        )
        self.assertEqual(
            [record["url"] for record in documents.items],
            ["https://example.com/b", "https://example.com/c"],
        )
        self.assertEqual([record["document_index"] for record in documents.items], [0, 1])
        document_handle = documents.summary["collection_handle"]
        self.assertEqual(
            {record["collection_handle"] for record in documents.items},
            {document_handle},
        )
        self.assertNotIn("https://example.com/a", transport.urls[1:])

        combined = adapter.handles.get(document_handle)
        self.assertEqual(combined.kind, "research.documents")
        self.assertEqual(
            [record["kind"] for record in combined.records],
            [
                "research.source", "research.document_chunk",
                "research.source", "research.document_chunk",
            ],
        )
        self.assertEqual(combined.metadata["source_count"], 2)
        self.assertEqual(combined.metadata["chunk_count"], 2)

        sources = adapter.observe(
            _context("research.sources"),
            _payload("research.sources", {"collection_handle": document_handle}),
        )
        self.assertEqual(
            [source["title"] for source in sources.items],
            ["Document B", "Document C"],
        )
        self.assertEqual(
            [source["search_result"]["rank"] for source in sources.items],
            [2, 3],
        )
        for source in sources.items:
            self.assertEqual(source["http_status"], 200)
            self.assertEqual(len(source["content_hash"]), 64)
            self.assertTrue(source["source_excerpt"].startswith("Source text"))
            self.assertEqual(source["redirect_chain"], [])
            self.assertTrue(source["fetched_at"].endswith("Z"))
            self.assertIn("Jan 2024", source["temporal_scope"])

        chunks = adapter.observe(
            _context("research.documents"),
            _payload("research.documents", {"collection_handle": document_handle}),
        )
        self.assertEqual(
            [chunk["text"] for chunk in chunks.items],
            ["Source text b.", "Source text c."],
        )
        self.assertEqual(
            {chunk["collection_handle"] for chunk in chunks.items},
            {document_handle},
        )

    def test_search_result_selection_bounds_and_handle_kinds_are_enforced(self) -> None:
        adapter = PublicResearchAdapter(
            transport=MultiDocumentTransport(), resolver=public_resolver,
        )
        search = adapter.observe(
            _context("research.search"),
            _payload("research.search", {"queries": ["semantic agents"]}),
        )
        search_handle = search.summary["collection_handle"]
        for invalid_limit in (0, 101, True, "2"):
            with self.subTest(result_limit=invalid_limit), self.assertRaises(ProtocolError) as caught:
                adapter.observe(
                    _context("research.documents"),
                    _payload(
                        "research.documents",
                        {"collection_handle": search_handle, "result_limit": invalid_limit},
                    ),
                )
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)

        with self.assertRaises(ProtocolError) as empty_window:
            adapter.observe(
                _context("research.documents"),
                _payload(
                    "research.documents",
                    {
                        "collection_handle": search_handle,
                        "result_offset": 99,
                        "result_limit": 1,
                    },
                ),
            )
        self.assertEqual(empty_window.exception.code, ErrorCode.NOT_FOUND)

        direct = adapter.observe(
            _context("research.documents"),
            _payload("research.documents", {"urls": ["https://example.com/a"]}),
        )
        document_handle = direct.summary["collection_handle"]
        with self.assertRaises(ProtocolError) as wrong_kind:
            adapter.observe(
                _context("research.results"),
                _payload("research.results", {"collection_handle": document_handle}),
            )
        self.assertEqual(wrong_kind.exception.code, ErrorCode.INVALID_REQUEST)

    def test_sources_and_documents_support_runtime_cursor_pagination(self) -> None:
        adapter = PublicResearchAdapter(
            transport=MultiDocumentTransport(), resolver=public_resolver,
        )
        direct = adapter.observe(
            _context("research.documents"),
            _payload(
                "research.documents",
                {"urls": ["https://example.com/a", "https://example.com/b"]},
            ),
        )
        document_handle = direct.summary["collection_handle"]
        runtime = SemanticRuntime(
            episode_id="research-pagination",
            max_tool_calls=10,
            guest_request=lambda *_args: {},
            guest_capabilities=[],
            adapters=[adapter],
        )

        def query(resource: str, cursor=None):
            return runtime.dispatch({
                "protocol_version": "1.0",
                "request_id": str(uuid.uuid4()),
                "episode_id": "research-pagination",
                "operation": "query",
                "payload": {
                    "resource": resource,
                    "scope": {},
                    "where": {},
                    "fields": [],
                    "order_by": [],
                    "parameters": {"collection_handle": document_handle},
                    "limit": 1,
                    "cursor": cursor,
                    "freshness": "live",
                },
            })

        first_source = query("research.sources")
        self.assertEqual(first_source["result"]["records"][0]["url"], "https://example.com/a")
        self.assertTrue(first_source["result"]["truncated"])

        # Reading another immutable collection must not invalidate the first
        # collection's cursor merely because both use research.sources.
        other = adapter.observe(
            _context("research.documents"),
            _payload("research.documents", {"urls": ["https://example.com/c"]}),
        )
        other_handle = other.summary["collection_handle"]
        other_source = runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "research-pagination",
            "operation": "query",
            "payload": {
                "resource": "research.sources", "scope": {}, "where": {},
                "fields": [], "order_by": [],
                "parameters": {"collection_handle": other_handle},
                "limit": 1, "freshness": "live",
            },
        })
        self.assertEqual(other_source["status"], "ok")
        second_source = query(
            "research.sources", first_source["result"]["next_cursor"],
        )
        self.assertEqual(second_source["result"]["records"][0]["url"], "https://example.com/b")

        first_chunk = query("research.documents")
        self.assertEqual(first_chunk["result"]["records"][0]["document_index"], 0)
        second_chunk = query(
            "research.documents", first_chunk["result"]["next_cursor"],
        )
        self.assertEqual(second_chunk["result"]["records"][0]["document_index"], 1)

    def test_private_url_cannot_be_smuggled_through_search_handle(self) -> None:
        transport = FakeTransport()
        adapter = PublicResearchAdapter(transport=transport, resolver=public_resolver)
        poisoned = adapter.handles.create(
            "research.results",
            [{
                "kind": "research.result",
                "query": "poisoned",
                "rank": 1,
                "title": "private",
                "url": "http://169.254.169.254/latest/meta-data",
            }],
        )
        with self.assertRaises(ProtocolError) as caught:
            adapter.observe(
                _context("research.documents"),
                _payload(
                    "research.documents",
                    {"collection_handle": poisoned.handle, "result_limit": 1},
                ),
            )
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(transport.urls, [])

    def test_search_and_document_handles_use_canonical_system_read_path(self) -> None:
        adapter = PublicResearchAdapter(
            transport=FakeTransport(), resolver=public_resolver
        )
        runtime = SemanticRuntime(
            episode_id="research-system-handles",
            max_tool_calls=20,
            guest_request=lambda *_args: {},
            guest_capabilities=[],
            adapters=[adapter],
        )

        def query(resource: str, *, parameters=None, scope=None):
            return runtime.dispatch({
                "protocol_version": "1.0",
                "request_id": str(uuid.uuid4()),
                "episode_id": "research-system-handles",
                "operation": "query",
                "payload": {
                    "resource": resource,
                    "scope": scope or {},
                    "where": {},
                    "fields": [],
                    "order_by": [],
                    "parameters": parameters or {},
                    "limit": 100,
                    "freshness": "live",
                },
            })

        search = query("research.search", parameters={"queries": ["semantic agents"]})
        search_handle = search["result"]["records"][0]["collection_handle"]
        search_collection = query(
            "system.data_handle", scope={"ref": search_handle}
        )
        self.assertEqual(search_collection["status"], "ok")
        self.assertEqual(
            search_collection["result"]["records"][0]["kind"],
            "research.result",
        )
        self.assertEqual(
            search_collection["provenance"],
            [{"source": "research.public-http@1", "freshness": "live"}],
        )

        documents = query(
            "research.documents",
            parameters={"urls": ["https://example.com/article"]},
        )
        document_handle = documents["result"]["records"][0]["collection_handle"]
        document_collection = query(
            "system.data_handle", scope={"ref": document_handle}
        )
        self.assertEqual(document_collection["status"], "ok")
        self.assertEqual(
            [record["kind"] for record in document_collection["result"]["records"]],
            ["research.source", "research.document_chunk"],
        )

        # Reading another adapter-owned handle cannot change either
        # collection's revision; handles are immutable episode capabilities.
        search_repeated = query("system.data_handle", scope={"ref": search_handle})
        self.assertEqual(
            search_repeated["after_revision"], search_collection["after_revision"]
        )
        repeated = query("system.data_handle", scope={"ref": document_handle})
        self.assertEqual(repeated["status"], "ok")
        self.assertEqual(repeated["after_revision"], document_collection["after_revision"])

    def test_unknown_system_data_handle_remains_not_found(self) -> None:
        adapter = PublicResearchAdapter(
            transport=FakeTransport(), resolver=public_resolver
        )
        runtime = SemanticRuntime(
            episode_id="research-unknown-handle",
            max_tool_calls=5,
            guest_request=lambda *_args: {},
            guest_capabilities=[],
            adapters=[adapter],
        )
        response = runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": "unknown-handle",
            "episode_id": "research-unknown-handle",
            "operation": "query",
            "payload": {
                "resource": "system.data_handle",
                "scope": {"ref": "data_does_not_exist"},
                "where": {}, "fields": [], "order_by": [], "parameters": {},
                "limit": 30, "freshness": "live",
            },
        })
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "not_found")

    def test_fake_transport_cannot_smuggle_private_redirect(self) -> None:
        class RedirectTransport(FakeTransport):
            def fetch(self, url: str) -> FetchResponse:
                return FetchResponse(url, "https://example.com/", 200, {"content-type": "text/plain"}, b"text", ("http://127.0.0.1/",), "now")

        adapter = PublicResearchAdapter(transport=RedirectTransport(), resolver=public_resolver)
        with self.assertRaises(ProtocolError) as caught:
            adapter.observe(_context("research.documents"), _payload("research.documents", {"urls": ["https://example.com/"]}))
        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)


if __name__ == "__main__":
    unittest.main()
