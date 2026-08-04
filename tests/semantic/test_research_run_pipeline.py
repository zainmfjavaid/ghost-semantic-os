from __future__ import annotations

import unittest
import uuid

from envserver.semantic.research_adapter import FetchResponse, PublicResearchAdapter
from envserver.semantic.runtime import SemanticRuntime


def public_resolver(host: str, port: int, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class PipelineTransport:
    resolver = staticmethod(public_resolver)

    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        if "duckduckgo" in url:
            body = b"".join((
                b'<a class="result__a" href="https://example.com/a">Result A</a>',
                b'<a class="result__a" href="https://example.com/b">Result B</a>',
                b'<a class="result__a" href="https://example.com/c">Result C</a>',
            ))
            return FetchResponse(
                url, url, 200, {"content-type": "text/html"}, body, (),
                "2026-01-01T00:00:00Z",
            )

        slug = url.rstrip("/").rsplit("/", 1)[-1]
        body = (
            f"<html><head><title>Document {slug}</title></head>"
            f"<body><h1>Facts</h1><p>Bounded source {slug}.</p></body></html>"
        ).encode()
        return FetchResponse(
            url,
            url,
            200,
            {
                "content-type": "text/html",
                "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            },
            body,
            (),
            "2026-01-01T00:00:01Z",
        )


class ResearchRunPipelineTests(unittest.TestCase):
    def test_run_composes_search_documents_sources_and_structured_rows(self) -> None:
        transport = PipelineTransport()
        adapter = PublicResearchAdapter(
            transport=transport,
            resolver=public_resolver,
            max_concurrency=2,
        )
        runtime = SemanticRuntime(
            episode_id="research-run-pipeline",
            max_tool_calls=10,
            guest_request=lambda *_args: {},
            guest_capabilities=[],
            adapters=[adapter],
        )
        code = """
search = computer.query(resource='research.search', parameters={'queries': ['generic systems research']}, limit=30)
search_handle = search['records'][0]['collection_handle']
fetched = computer.query(resource='research.documents', parameters={'collection_handle': search_handle, 'result_offset': 1, 'result_limit': 2}, limit=30)
documents_handle = fetched['records'][0]['collection_handle']
sources = computer.query(resource='research.sources', parameters={'collection_handle': documents_handle}, limit=30)
chunks = computer.query(resource='research.documents', parameters={'collection_handle': documents_handle}, limit=30)
chunk_text = dict()
for chunk in chunks['records']:
    key = str(chunk['document_index'])
    if key not in chunk_text:
        chunk_text[key] = []
    chunk_text[key].append(chunk['text'])
rows = []
for source in sources['records']:
    key = str(source['document_index'])
    rows.append({
        'title': source['title'],
        'url': source['url'],
        'http_status': source['http_status'],
        'content_hash': source['content_hash'],
        'fetched_at': source['fetched_at'],
        'temporal_scope': source['temporal_scope'],
        'source_excerpt': source['source_excerpt'],
        'redirect_chain': source['redirect_chain'],
        'text': ' '.join(chunk_text.get(key, [])),
        'source_adapter': source['source'],
        'freshness': source['freshness'],
        'collection_handle': source['collection_handle']
    })
emit({'search_handle': search_handle, 'documents_handle': documents_handle, 'rows': rows})
"""
        response = runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "research-run-pipeline",
            "operation": "run",
            "payload": {"code": code},
        })

        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["result"]["operation_count"], 4)
        self.assertIsNone(response["result"]["failed_operation"])
        emitted = response["result"]["output"][0]
        self.assertTrue(emitted["search_handle"].startswith("data_"))
        self.assertTrue(emitted["documents_handle"].startswith("data_"))
        self.assertNotEqual(emitted["search_handle"], emitted["documents_handle"])
        self.assertEqual(
            [row["url"] for row in emitted["rows"]],
            ["https://example.com/b", "https://example.com/c"],
        )
        self.assertEqual(
            [row["text"] for row in emitted["rows"]],
            ["Bounded source b.", "Bounded source c."],
        )
        for row in emitted["rows"]:
            self.assertEqual(row["collection_handle"], emitted["documents_handle"])
            self.assertEqual(row["source_adapter"], "research.public-http@1")
            self.assertEqual(row["freshness"], "live")
            self.assertEqual(row["http_status"], 200)
            self.assertEqual(len(row["content_hash"]), 64)
            self.assertEqual(row["redirect_chain"], [])
            self.assertEqual(
                row["temporal_scope"], "Mon, 01 Jan 2024 00:00:00 GMT",
            )

        fetched_urls = [url for url in transport.urls if "duckduckgo" not in url]
        self.assertCountEqual(
            fetched_urls,
            ["https://example.com/b", "https://example.com/c"],
        )
        self.assertNotIn("https://example.com/a", fetched_urls)


if __name__ == "__main__":
    unittest.main()
