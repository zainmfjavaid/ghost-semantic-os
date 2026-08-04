"""Public-HTTP research adapter with SSRF controls and source provenance.

The adapter performs bounded, read-only fetches.  It never chooses an answer,
never silently substitutes a source, and stores full chunk collections behind
opaque episode-local handles.  DNS and every redirect target are checked for
globally routable addresses before a connection is made.
"""

from __future__ import annotations

import hashlib
import html
import base64
import binascii
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from threading import RLock
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit, urlunsplit

from .adapters import AdapterActionResult, AdapterContext, AdapterObservation, SemanticAdapter
from .data_handles import DataHandleRecord, DataHandleStore
from .protocol import ErrorCode, ProtocolError, utc_now


MAX_FETCH_BYTES = 4 * 1024 * 1024
MAX_URLS_PER_QUERY = 100
MAX_REDIRECTS = 5
MAX_DOCUMENT_CHUNKS = 5_000
ALLOWED_PORTS = {80, 443}


def _validate_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "HTTP host resolved to an invalid address") from error
    if not address.is_global:
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "HTTP destination is not globally routable")
    return str(address)


def validate_public_url(
    url: str,
    *,
    resolver: Callable[..., Sequence[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(url, str) or not url or len(url) > 8_192 or "\x00" in url:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "HTTP URL is invalid")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "only public HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "URL credentials and fragments are forbidden")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "HTTP destination port is not allowed")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "localhost is forbidden")
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (_validate_ip(str(literal)),)
    else:
        try:
            results = resolver(hostname, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as error:
            raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "HTTP DNS resolution failed", retryable=True) from error
        addresses = tuple(sorted({_validate_ip(str(result[4][0])) for result in results}))
        if not addresses:
            raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "HTTP host resolved to no addresses", retryable=True)
    host_part = f"[{hostname}]" if ":" in hostname else hostname
    if port != (443 if parsed.scheme == "https" else 80):
        host_part = f"{host_part}:{port}"
    normalized = urlunsplit((parsed.scheme, host_part, parsed.path or "/", parsed.query, ""))
    return normalized, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        self._address = address
        super().__init__(hostname, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        self._address = address
        context = ssl.create_default_context()
        super().__init__(hostname, port=port, timeout=timeout, context=context)

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


@dataclass(frozen=True)
class FetchResponse:
    requested_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    redirect_chain: tuple[str, ...]
    fetched_at: str


@dataclass(frozen=True)
class StreamResponse:
    """Metadata for a bounded streamed public-HTTP response.

    Unlike ``FetchResponse``, this record never retains the body. It exists for
    source-to-artifact compositions whose legitimate payloads are much larger
    than research documents and must not be materialized in model context (or
    twice in harness memory).
    """

    requested_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    redirect_chain: tuple[str, ...]
    fetched_at: str
    size: int
    content_hash: str


class PublicHTTPTransport:
    """IP-pinned HTTP transport; DNS is checked before every redirect hop."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        resolver: Callable[..., Sequence[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.resolver = resolver

    def fetch(self, url: str) -> FetchResponse:
        requested = url
        chain: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            normalized, addresses = validate_public_url(current, resolver=self.resolver)
            parsed = urlsplit(normalized)
            hostname = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
            connection = connection_type(hostname, addresses[0], port, self.timeout_seconds)
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            try:
                connection.request("GET", target, headers={
                    "Host": hostname if port in {80, 443} else f"{hostname}:{port}",
                    "User-Agent": "Ghost-Semantic-Research/1.0",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,application/pdf;q=0.8,*/*;q=0.2",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                headers = {key.casefold(): value for key, value in response.getheaders()}
                status = response.status
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location:
                        raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "HTTP redirect omitted Location")
                    chain.append(normalized)
                    current = urljoin(normalized, location)
                    continue
                body = response.read(MAX_FETCH_BYTES + 1)
                if len(body) > MAX_FETCH_BYTES:
                    raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "HTTP response exceeds fetch limit")
                return FetchResponse(requested, normalized, status, headers, body, tuple(chain), utc_now())
            except ProtocolError:
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "public HTTP fetch failed", retryable=True) from error
            finally:
                connection.close()
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "HTTP redirect limit exceeded")

    def stream(
        self,
        url: str,
        sink: Callable[[bytes], None],
        *,
        max_bytes: int,
        chunk_bytes: int = 384 * 1024,
    ) -> StreamResponse:
        """Fetch one public URL into a private bounded sink.

        DNS is pinned and every redirect is revalidated exactly as in
        ``fetch``. Bytes are counted before they reach the sink, so exceeding
        the caller's explicit cap cannot leave an over-limit committed
        artifact. The sink is infrastructure-private; no response bytes enter
        a semantic result.
        """

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
            or max_bytes > 1024 * 1024 * 1024
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "stream byte limit is invalid")
        if (
            isinstance(chunk_bytes, bool)
            or not isinstance(chunk_bytes, int)
            or chunk_bytes < 1
            or chunk_bytes > 512 * 1024
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "stream chunk size is invalid")

        requested = url
        chain: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            normalized, addresses = validate_public_url(current, resolver=self.resolver)
            parsed = urlsplit(normalized)
            hostname = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            connection_type = (
                _PinnedHTTPSConnection
                if parsed.scheme == "https"
                else _PinnedHTTPConnection
            )
            connection = connection_type(
                hostname, addresses[0], port, self.timeout_seconds
            )
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            try:
                connection.request("GET", target, headers={
                    "Host": hostname if port in {80, 443} else f"{hostname}:{port}",
                    "User-Agent": "Ghost-Semantic-Artifact/1.0",
                    "Accept": "application/octet-stream,application/zip,*/*;q=0.2",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                headers = {key.casefold(): value for key, value in response.getheaders()}
                status = response.status
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location:
                        raise ProtocolError(
                            ErrorCode.ADAPTER_UNAVAILABLE,
                            "HTTP redirect omitted Location",
                        )
                    chain.append(normalized)
                    current = urljoin(normalized, location)
                    continue
                if not 200 <= int(status) < 300:
                    raise ProtocolError(
                        ErrorCode.NO_EFFECT,
                        "public artifact download returned a non-success HTTP status",
                    )
                raw_length = headers.get("content-length")
                if raw_length is not None:
                    try:
                        declared_length = int(raw_length)
                    except ValueError as error:
                        raise ProtocolError(
                            ErrorCode.ADAPTER_UNAVAILABLE,
                            "public artifact response has an invalid Content-Length",
                        ) from error
                    if declared_length < 0 or declared_length > max_bytes:
                        raise ProtocolError(
                            ErrorCode.BUDGET_EXHAUSTED,
                            "public artifact exceeds its byte limit",
                        )
                size = 0
                digest = hashlib.sha256()
                while True:
                    chunk = response.read(chunk_bytes)
                    if not chunk:
                        break
                    next_size = size + len(chunk)
                    if next_size > max_bytes:
                        raise ProtocolError(
                            ErrorCode.BUDGET_EXHAUSTED,
                            "public artifact exceeds its byte limit",
                        )
                    sink(chunk)
                    digest.update(chunk)
                    size = next_size
                return StreamResponse(
                    requested_url=requested,
                    final_url=normalized,
                    status=int(status),
                    headers=headers,
                    redirect_chain=tuple(chain),
                    fetched_at=utc_now(),
                    size=size,
                    content_hash=digest.hexdigest(),
                )
            except ProtocolError:
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                raise ProtocolError(
                    ErrorCode.ADAPTER_UNAVAILABLE,
                    "public artifact fetch failed",
                    retryable=True,
                ) from error
            finally:
                connection.close()
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "HTTP redirect limit exceeded")


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[dict[str, str]] = []
        self._capture_title = False
        self._skip = 0
        self._current_heading: str | None = None
        self._buffer: list[str] = []

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
        if text:
            self.blocks.append({"heading": self._current_heading or "", "text": text})
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        elif tag == "title":
            self._capture_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._current_heading = ""
        elif tag in {"p", "li", "tr", "article", "section", "br"}:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._capture_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
            self._buffer = []
            self._current_heading = heading
        elif tag in {"p", "li", "tr", "article", "section", "body"}:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._capture_title:
            self.title_parts.append(data)
            return
        self._buffer.append(data)

    def result(self) -> tuple[str, list[dict[str, str]]]:
        self._flush()
        title = re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()
        return title, self.blocks


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and "result__a" in (attributes.get("class") or ""):
            self._href = attributes.get("href")
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            href = html.unescape(self._href)
            query = parse_qs(urlsplit(href).query)
            if query.get("uddg"):
                href = unquote(query["uddg"][0])
            title = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            if title and href.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": href})
            self._href = None
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)


class _BingSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._result_depth = 0
        self._heading_depth = 0
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._result_depth = 1
            return
        if self._result_depth:
            self._result_depth += 1
            if tag == "h2":
                self._heading_depth = self._result_depth
            elif tag == "a" and self._heading_depth and self._href is None:
                self._href = attributes.get("href")
                self._text = []

    def handle_endtag(self, tag: str) -> None:
        if self._result_depth and tag == "a" and self._href:
            title = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            href = html.unescape(self._href)
            query = parse_qs(urlsplit(href).query)
            encoded = query.get("u", [""])[0]
            if encoded.startswith("a1"):
                try:
                    raw = encoded[2:]
                    raw += "=" * (-len(raw) % 4)
                    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
                    if decoded.startswith(("http://", "https://")):
                        href = decoded
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    pass
            if title and href.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": href})
            self._href = None
            self._text = []
        if self._result_depth:
            if tag == "h2":
                self._heading_depth = 0
            self._result_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)


def _decode_body(response: FetchResponse) -> str:
    content_type = response.headers.get("content-type", "")
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if match:
        charset = match.group(1).strip("\"'")
    try:
        return response.body.decode(charset, errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


class PublicResearchAdapter(SemanticAdapter):
    adapter_id = "research.public-http@1"
    application = "public HTTP research"
    supported_versions = ("HTTP/1.1", "HTTPS")
    resources = frozenset({"research.search", "research.results", "research.documents", "research.sources"})
    capabilities = frozenset()
    resource_schemas = {
        "research.search": {
            "type": "object",
            "properties": {
                "queries": {
                    "oneOf": [
                        {"type": "string", "maxLength": 1_000},
                        {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "maxLength": 1_000}},
                    ]
                }
            },
            "required": ["queries"], "additionalProperties": False,
        },
        "research.documents": {
            "type": "object",
            "properties": {
                "urls": {
                    "oneOf": [
                        {"type": "string", "maxLength": 8_192},
                        {"type": "array", "minItems": 1, "maxItems": MAX_URLS_PER_QUERY, "items": {"type": "string", "maxLength": 8_192}},
                    ]
                },
                "collection_handle": {"type": "string"},
                "data_handle": {
                    "type": "string",
                    "description": "Deprecated alias for collection_handle",
                },
                "result_offset": {"type": "integer"},
                "result_limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "research.results": {
            "type": "object",
            "properties": {
                "collection_handle": {"type": "string"},
                "data_handle": {
                    "type": "string",
                    "description": "Deprecated alias for collection_handle",
                },
            },
            "oneOf": [
                {"required": ["collection_handle"]},
                {"required": ["data_handle"]},
            ],
            "additionalProperties": False,
        },
        "research.sources": {
            "type": "object",
            "properties": {
                "collection_handle": {"type": "string"},
                "data_handle": {
                    "type": "string",
                    "description": "Deprecated alias for collection_handle",
                },
            },
            "oneOf": [
                {"required": ["collection_handle"]},
                {"required": ["data_handle"]},
            ],
            "additionalProperties": False,
        },
    }

    def __init__(
        self,
        *,
        transport: PublicHTTPTransport | Any | None = None,
        handles: DataHandleStore | None = None,
        max_concurrency: int = 8,
        resolver: Callable[..., Sequence[tuple[Any, ...]]] | None = None,
    ) -> None:
        self.transport = transport or PublicHTTPTransport()
        self.handles = handles or DataHandleStore()
        self.max_concurrency = max(1, min(int(max_concurrency), 8))
        self.resolver = resolver or getattr(self.transport, "resolver", socket.getaddrinfo)
        self._native: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._generation = 0

    def probe(self) -> Mapping[str, Any]:
        return {
            "ok": callable(getattr(self.transport, "fetch", None)),
            "adapter_id": self.adapter_id,
            "max_concurrency": self.max_concurrency,
            "allowed_schemes": ["http", "https"],
        }

    def resolve_ref(self, ref: str) -> Mapping[str, Any]:
        with self._lock:
            record = self._native.get(ref)
            if record is None:
                raise ProtocolError(ErrorCode.STALE_REF, "research ref no longer resolves")
            return dict(record)

    def resolve_data_handle(self, handle: str) -> AdapterObservation:
        stored = self.handles.get(handle)
        records = tuple(dict(record) for record in stored.records)
        revision = hashlib.sha256(
            json.dumps(
                {
                    "handle": stored.handle,
                    "kind": stored.kind,
                    "records": records,
                    "metadata": stored.metadata,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return AdapterObservation(
            items=records,
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={
                "collection_handle": stored.handle,
                "kind": stored.kind,
                "record_count": len(records),
                **dict(stored.metadata),
            },
            native_revision=f"research_handle_{revision[:24]}",
        )

    @staticmethod
    def _queries(parameters: Mapping[str, Any]) -> list[str]:
        raw = parameters.get("queries")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw or len(raw) > 20 or not all(isinstance(value, str) and 0 < len(value) <= 1_000 for value in raw):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "research.search requires 1..20 bounded queries")
        return raw

    @staticmethod
    def _urls(parameters: Mapping[str, Any]) -> list[str]:
        raw = parameters.get("urls")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw or len(raw) > MAX_URLS_PER_QUERY or not all(isinstance(value, str) for value in raw):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "research.documents requires 1..100 URLs")
        return raw

    def _search_one(self, query: str) -> list[dict[str, Any]]:
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        validate_public_url(search_url, resolver=self.resolver)
        response = self.transport.fetch(search_url)
        validate_public_url(response.final_url, resolver=self.resolver)
        for redirected in response.redirect_chain:
            validate_public_url(redirected, resolver=self.resolver)
        parser = _SearchParser()
        parser.feed(_decode_body(response))
        provider = "duckduckgo-html"
        if not parser.results:
            search_url = f"https://www.bing.com/search?q={quote_plus(query)}"
            validate_public_url(search_url, resolver=self.resolver)
            response = self.transport.fetch(search_url)
            validate_public_url(response.final_url, resolver=self.resolver)
            for redirected in response.redirect_chain:
                validate_public_url(redirected, resolver=self.resolver)
            bing = _BingSearchParser()
            bing.feed(_decode_body(response))
            parser.results = bing.results
            provider = "bing-html"
        records = []
        for rank, result in enumerate(parser.results[:50], 1):
            # Validate result URLs before they become fetchable source records.
            normalized, _ = validate_public_url(result["url"], resolver=self.resolver)
            records.append({
                "kind": "research.result",
                "query": query,
                "rank": rank,
                "title": result["title"],
                "url": normalized,
                "search_source": response.final_url,
                "search_provider": provider,
                "fetched_at": response.fetched_at,
                "advertised_actions": [],
            })
        return records

    def _document_one(self, url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        validate_public_url(url, resolver=self.resolver)
        response = self.transport.fetch(url)
        validate_public_url(response.final_url, resolver=self.resolver)
        for redirected in response.redirect_chain:
            validate_public_url(redirected, resolver=self.resolver)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        digest = hashlib.sha256(response.body).hexdigest()
        title = ""
        chunks: list[dict[str, Any]] = []
        if content_type in {"text/html", "application/xhtml+xml", ""}:
            parser = _DocumentParser()
            parser.feed(_decode_body(response))
            title, blocks = parser.result()
            for index, block in enumerate(blocks):
                text = block["text"]
                for offset in range(0, len(text), 2_000):
                    chunks.append({
                        "kind": "research.document_chunk",
                        "url": response.final_url,
                        "section": block["heading"],
                        "index": len(chunks),
                        "text": text[offset:offset + 2_000],
                        "content_hash": digest,
                    })
        elif content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
            text = _decode_body(response)
            chunks = [{
                "kind": "research.document_chunk", "url": response.final_url,
                "section": "", "index": index, "text": text[offset:offset + 2_000],
                "content_hash": digest,
            } for index, offset in enumerate(range(0, len(text), 2_000))]
        else:
            chunks = []
        if len(chunks) > MAX_DOCUMENT_CHUNKS:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "research document produced too many chunks")
        source = {
            "kind": "research.source",
            "requested_url": response.requested_url,
            "url": response.final_url,
            "title": title,
            "http_status": response.status,
            "content_type": content_type,
            "content_hash": digest,
            "fetched_at": response.fetched_at,
            "redirect_chain": list(response.redirect_chain),
            "source_excerpt": chunks[0]["text"] if chunks else "",
            "temporal_scope": response.headers.get("last-modified") or response.headers.get("date"),
            "chunk_count": len(chunks),
            "advertised_actions": [],
        }
        return source, chunks

    def _parallel(self, values: Sequence[str], worker: Callable[[str], Any]) -> list[Any]:
        outputs: list[Any] = [None] * len(values)
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(values))) as pool:
            futures = {pool.submit(worker, value): index for index, value in enumerate(values)}
            for future in as_completed(futures):
                outputs[futures[future]] = future.result()
        return outputs

    def _handle_record(self, parameters: Mapping[str, Any]) -> DataHandleRecord:
        has_collection = "collection_handle" in parameters
        has_legacy = "data_handle" in parameters
        if has_collection == has_legacy:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "exactly one collection_handle is required (data_handle is a deprecated alias)",
            )
        handle = (
            parameters.get("collection_handle")
            if has_collection
            else parameters.get("data_handle")
        )
        if not isinstance(handle, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "collection_handle must be a string")
        return self.handles.get(handle)

    @staticmethod
    def _result_window(parameters: Mapping[str, Any]) -> tuple[int, int]:
        """Return an explicit bounded window into an ordered search collection.

        Requiring a limit is intentional: consuming a search handle must not
        turn one model query into an accidental unbounded crawl.  The limit is
        a collection bound, not a concurrency bound; `_parallel` still caps
        simultaneous network requests at eight.
        """

        offset = parameters.get("result_offset", 0)
        limit = parameters.get("result_limit")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "research.documents result_offset must be a non-negative integer",
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_URLS_PER_QUERY
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "research.documents requires result_limit between 1 and 100 when consuming search results",
            )
        return offset, limit

    def _documents_from_urls(
        self,
        urls: Sequence[str],
        *,
        search_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], Any]:
        documents = self._parallel(urls, self._document_one)
        combined: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        for document_index, (source_raw, chunks_raw) in enumerate(documents):
            source = dict(source_raw)
            source["document_index"] = document_index
            if search_results is not None:
                search_result = search_results[document_index]
                search_title = search_result.get("title")
                if not source.get("title") and isinstance(search_title, str):
                    source["title"] = search_title
                source["search_result"] = {
                    key: search_result[key]
                    for key in (
                        "query", "rank", "title", "url", "search_source",
                        "search_provider", "fetched_at",
                    )
                    if key in search_result
                }
            chunks: list[dict[str, Any]] = []
            for chunk_raw in chunks_raw:
                chunk = dict(chunk_raw)
                chunk["document_index"] = document_index
                chunks.append(chunk)
            combined.extend((source, *chunks))
            source_records.append(source)

        collection_handle = self.handles.create(
            "research.documents",
            combined,
            metadata={
                "source_count": len(source_records),
                "chunk_count": sum(
                    int(source.get("chunk_count") or 0) for source in source_records
                ),
            },
        )
        returned_sources = []
        for source in source_records:
            returned = dict(source)
            returned["collection_handle"] = collection_handle.handle
            returned_sources.append(returned)
        return returned_sources, collection_handle

    def observe(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterObservation:
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "research parameters must be an object")
        active_handle: str | None = None
        if context.resource == "research.search":
            queries = self._queries(parameters)
            groups = self._parallel(queries, self._search_one)
            records = [record for group in groups for record in group]
            handle = self.handles.create("research.results", records, metadata={"queries": queries})
            with self._lock:
                self._generation += 1
            for record in records:
                record["collection_handle"] = handle.handle
            active_handle = handle.handle
        elif context.resource == "research.documents":
            has_handle = (
                "collection_handle" in parameters or "data_handle" in parameters
            )
            has_urls = "urls" in parameters
            if has_handle == has_urls:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "research.documents requires exactly one of urls or collection_handle",
                )
            if has_handle:
                stored = self._handle_record(parameters)
                if stored.kind == "research.results":
                    offset, limit = self._result_window(parameters)
                    results = [
                        dict(record) for record in stored.records
                        if record.get("kind") == "research.result"
                    ]
                    selected = results[offset:offset + limit]
                    if not selected:
                        raise ProtocolError(
                            ErrorCode.NOT_FOUND,
                            "the selected search-result window is empty",
                        )
                    urls = [str(record["url"]) for record in selected]
                    records, collection_handle = self._documents_from_urls(
                        urls,
                        search_results=selected,
                    )
                    active_handle = collection_handle.handle
                    with self._lock:
                        self._generation += 1
                elif stored.kind == "research.documents":
                    if "result_offset" in parameters or "result_limit" in parameters:
                        raise ProtocolError(
                            ErrorCode.INVALID_REQUEST,
                            "result_offset/result_limit apply only to research.results handles",
                        )
                    records = [
                        dict(record) for record in stored.records
                        if record.get("kind") == "research.document_chunk"
                    ]
                    active_handle = stored.handle
                    for record in records:
                        record["collection_handle"] = active_handle
                else:
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST,
                        "data handle does not contain research results or documents",
                    )
            else:
                if "result_offset" in parameters or "result_limit" in parameters:
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST,
                        "result_offset/result_limit require a research.results collection_handle",
                    )
                urls = self._urls(parameters)
                records, collection_handle = self._documents_from_urls(urls)
                active_handle = collection_handle.handle
                with self._lock:
                    self._generation += 1
        elif context.resource in {"research.results", "research.sources"}:
            stored = self._handle_record(parameters)
            expected_kind = (
                "research.documents"
                if context.resource == "research.sources"
                else "research.results"
            )
            if stored.kind != expected_kind:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"{context.resource} requires a {expected_kind} data handle",
                )
            records = [dict(record) for record in stored.records]
            wanted = "research.source" if context.resource == "research.sources" else "research.result"
            records = [record for record in records if record.get("kind") == wanted]
            for record in records:
                record["collection_handle"] = stored.handle
            active_handle = stored.handle
        else:
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, context.resource)
        records = [dict(record) for record in records]
        with self._lock:
            for record in records:
                identity = hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[:30]
                native = f"native_research_{identity}"
                self._native[native] = dict(record)
                record["ref"] = native
        with self._lock:
            generation = self._generation
        native_revision = (
            "immutable_research_collections_v1"
            if context.resource in {
                "research.results", "research.sources", "research.documents",
            }
            else f"research_{generation}"
        )
        return AdapterObservation(
            items=tuple(records),
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={
                "record_count": len(records),
                "collection_handle": active_handle,
                "handles": self.handles.describe(),
            },
            # Results/documents are immutable collections selected by a query
            # fingerprint containing collection_handle. A constant resource
            # revision lets independent handles be paged in interleaved order;
            # the opaque cursor still cannot be replayed against another handle.
            native_revision=native_revision,
        )

    def act(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterActionResult:
        raise ProtocolError(ErrorCode.UNSUPPORTED, "research resources are read-only")

    def close(self) -> None:
        self.handles.clear()
        with self._lock:
            self._native.clear()
