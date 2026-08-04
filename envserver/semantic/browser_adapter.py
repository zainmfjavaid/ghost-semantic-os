"""Async Playwright/CDP semantic browser adapter.

One adapter owns one asyncio loop for the lifetime of an episode.  Synchronous
FastAPI handlers submit coroutines to that loop, so Playwright objects never
cross loops or threads and the v15 sync-inside-async failure class is absent.
CSS/XPath selectors, DOM object IDs, backend node IDs, and geometry remain
private adapter state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .adapters import AdapterActionResult, AdapterContext, AdapterObservation, SemanticAdapter
from .protocol import ErrorCode, ProtocolError, SideEffectState, Status


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _ax_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value")
    return value


_SENSITIVE_URL_KEYS = frozenset({
    "access_token", "auth", "authorization", "code", "credential", "key",
    "password", "secret", "session", "sig", "signature", "token",
})


# EpisodeState intentionally refuses observations larger than 5,000 records.
# Real applications such as Colab can expose substantially larger, mostly
# empty accessibility trees.  Keep this bound colocated with the adapter so an
# oversized browser tree is compacted before it reaches shared episode state.
MAX_BROWSER_AX_RECORDS = 5_000

_AX_INVOKABLE_ROLES = frozenset({
    "button", "link", "menuitem", "tab", "treeitem", "combobox", "listbox",
    "option",
})
_AX_TEXT_ROLES = frozenset({"textbox", "searchbox"})
_AX_TOGGLE_ROLES = frozenset({"checkbox", "radio", "switch"})
_AX_STRUCTURAL_ROLES = frozenset({
    "alert", "article", "banner", "blockquote", "caption", "cell",
    "columnheader", "complementary", "contentinfo", "definition",
    "dialog", "document", "figure", "form", "grid", "gridcell", "group",
    "heading", "image", "img", "list", "listitem", "log", "main", "menu",
    "menubar", "navigation", "note", "paragraph", "region", "row",
    "rowgroup", "rowheader", "search", "separator", "status", "table",
    "tabpanel", "term", "text", "toolbar", "tooltip", "tree", "treegrid",
    # CDP uses these role spellings for rendered text and the document root.
    "inlineTextBox".casefold(), "rootWebArea".casefold(),
    "staticText".casefold(),
})
_AX_STATEFUL_PROPERTIES = frozenset({
    "busy", "checked", "current", "disabled", "editable", "expanded",
    "focusable", "focused", "haspopup", "invalid", "level", "modal",
    "multiline", "multiselectable", "orientation", "pressed", "protected",
    "readonly", "required", "selected", "settable", "valuemax", "valuemin",
    "valuetext",
})


def _intrinsic_ax_actions(role: str) -> list[str]:
    """Return actions implied by a node's semantic role.

    ``scroll_into_view`` is deliberately excluded here.  It is advertised on
    retained records, but treating it as an intrinsic action would make every
    backend node high priority and defeat semantic compaction.
    """

    folded = role.casefold()
    actions: list[str] = []
    if folded in _AX_INVOKABLE_ROLES:
        actions.append("invoke")
    if folded in _AX_TEXT_ROLES:
        actions.extend(("set_text", "set_value"))
    if folded in _AX_TOGGLE_ROLES:
        actions.extend(("check", "uncheck", "toggle"))
    if folded in {"combobox", "listbox"}:
        actions.append("select_option")
    if folded == "form":
        actions.append("submit")
    return actions


def _meaningful_ax_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _normalized_ax_states(properties: Mapping[str, Any]) -> dict[str, Any]:
    """Translate CDP AX property names into the canonical state vocabulary."""

    states = dict(properties)
    if properties.get("disabled") is True:
        states["enabled"] = False
    if properties.get("readonly") is True:
        states["read_only"] = True
    return states


def _compile_ax_records(
    page: Any,
    trees: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]],
    remember: Callable[[str, Any, Mapping[str, Any]], str],
    *,
    limit: int = MAX_BROWSER_AX_RECORDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compile and deterministically compact raw CDP accessibility trees.

    High-value nodes are selected by semantic priority, then emitted in their
    original document order.  Relationships are rebuilt over the retained
    graph, climbing through discarded intermediates, so no public ref points
    at an absent record.
    """

    if limit < 1:
        raise ValueError("AX record limit must be positive")

    total_records = 0
    ignored_records = 0
    empty_records = 0
    candidates: list[dict[str, Any]] = []
    parent_by_ax_id: dict[str, str | None] = {}

    for frame, nodes in trees:
        frame_id = str(frame.get("id") or "main")

        def scoped(node_id: Any) -> str | None:
            if node_id is None:
                return None
            return f"{frame_id}:{node_id}"

        # Include all AX nodes in the ancestry map, including nodes with no DOM
        # backend identity. They can still connect retained semantic records.
        for node in nodes:
            ax_id = scoped(node.get("nodeId"))
            if ax_id is not None:
                parent_by_ax_id[ax_id] = scoped(node.get("parentId"))

        for node in nodes:
            backend = node.get("backendDOMNodeId")
            if backend is None:
                continue
            total_records += 1
            if bool(node.get("ignored", False)):
                ignored_records += 1
                continue

            role = str(_ax_value(node.get("role")) or "unknown")
            name = str(_ax_value(node.get("name")) or "")[:2_000]
            value = _ax_value(node.get("value"))
            description = str(_ax_value(node.get("description")) or "")[:2_000]
            properties = {
                str(prop.get("name")): _ax_value(prop.get("value"))
                for prop in node.get("properties") or []
                if prop.get("name")
            }
            if properties.get("protected") is True:
                value = "[redacted]"

            states = _normalized_ax_states(properties)
            actions = _intrinsic_ax_actions(role)
            stateful = any(
                key.casefold() in _AX_STATEFUL_PROPERTIES and item is not None
                for key, item in properties.items()
            )
            has_text = any(
                _meaningful_ax_value(item) for item in (name, value, description)
            )
            structural = role.casefold() in _AX_STRUCTURAL_ROLES
            if not (actions or stateful or has_text or structural):
                empty_records += 1
                continue

            # Actionable/stateful nodes outrank text nodes, which outrank
            # structure-only nodes. Stable sort order is the original AX order.
            priority = 0 if actions or stateful else 1 if has_text else 2
            candidates.append({
                "kind": "browser.element",
                "role": role,
                "name": name,
                "value": value,
                "description": description,
                "states": states,
                "ignored": False,
                "frame_url": _public_url(frame.get("url")),
                "frame_name": str(frame.get("name") or "")[:500],
                "advertised_actions": [*actions, "scroll_into_view"],
                "_priority": priority,
                "_ax_id": scoped(node.get("nodeId")) or f"{frame_id}:backend:{backend}",
                "_parent_ax_id": scoped(node.get("parentId")),
                "_native_identity": f"{id(page)}:{frame_id}:{backend}",
                "_native_payload": {
                    "page": page,
                    "backend_node_id": int(backend),
                    "frame_id": frame_id,
                    # Private semantic identity used only to reconcile an
                    # uncertain text mutation after a DOM refresh. It is never
                    # exposed as a selector or accepted from the model.
                    "semantic_identity": {
                        "role": role.casefold(),
                        "name": name,
                        "description": description,
                        "frame_name": str(frame.get("name") or "")[:500],
                    },
                },
            })

    if len(candidates) > limit:
        ranked = sorted(
            range(len(candidates)),
            key=lambda index: (int(candidates[index]["_priority"]), index),
        )
        retained_indexes = set(ranked[:limit])
        retained = [
            candidate
            for index, candidate in enumerate(candidates)
            if index in retained_indexes
        ]
    else:
        retained = candidates

    retained_ax_ids = {str(candidate["_ax_id"]) for candidate in retained}
    ax_id_to_ref: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for candidate in retained:
        ref = remember(
            "backend_node",
            candidate["_native_identity"],
            candidate["_native_payload"],
        )
        ax_id_to_ref[str(candidate["_ax_id"])] = ref
        records.append({
            key: item
            for key, item in candidate.items()
            if not key.startswith("_")
        } | {"ref": ref, "parent_ref": None, "child_refs": []})

    record_by_ref = {str(record["ref"]): record for record in records}
    for candidate, record in zip(retained, records):
        parent_ax_id = candidate.get("_parent_ax_id")
        visited: set[str] = set()
        while parent_ax_id is not None and parent_ax_id not in retained_ax_ids:
            parent_ax_id = str(parent_ax_id)
            if parent_ax_id in visited:
                parent_ax_id = None
                break
            visited.add(parent_ax_id)
            parent_ax_id = parent_by_ax_id.get(parent_ax_id)
        if parent_ax_id is None:
            continue
        parent_ref = ax_id_to_ref.get(str(parent_ax_id))
        if parent_ref is None:
            continue
        record["parent_ref"] = parent_ref
        record_by_ref[parent_ref]["child_refs"].append(record["ref"])

    retained_count = len(records)
    budget_truncated = max(0, len(candidates) - retained_count)
    summary = {
        "total_records": total_records,
        "retained_records": retained_count,
        "truncated_records": total_records - retained_count,
        "truncated": retained_count < total_records,
        "ignored_records": ignored_records,
        "empty_records": empty_records,
        "budget_truncated_records": budget_truncated,
    }
    return records, summary


def _public_url(value: Any) -> str:
    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw[:2_000]
    if not parsed.scheme or not parsed.netloc:
        return raw[:2_000]
    host = parsed.hostname or ""
    try:
        if parsed.port:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return raw[:2_000]
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        sensitive = key.casefold() in _SENSITIVE_URL_KEYS or any(
            token in key.casefold() for token in ("token", "secret", "password", "auth")
        )
        query.append((key, "[redacted]" if sensitive else item))
    return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))[:2_000]


class AsyncBrowserAdapter(SemanticAdapter):
    adapter_id = "browser.cdp@1"
    resources = frozenset({
        "browser.targets", "browser.tabs", "browser.frames", "browser.page",
        "browser.text", "browser.elements", "browser.forms", "browser.links",
        "browser.storage", "browser.downloads", "browser.navigation_state",
    })
    capabilities = frozenset({
        "open_tab", "switch_tab", "close_tab", "navigate", "reload", "back",
        "forward", "invoke", "set_text", "set_value", "select_option", "check",
        "uncheck", "toggle", "submit", "scroll_into_view", "wait",
    })
    resource_actions = {
        "browser.targets": ("open_tab", "switch_tab", "close_tab"),
        "browser.tabs": (
            "open_tab", "switch_tab", "close_tab", "navigate", "reload",
            "back", "forward", "wait",
        ),
        "browser.frames": (),
        "browser.page": ("navigate", "reload", "back", "forward", "wait"),
        "browser.text": (),
        "browser.elements": (
            "invoke", "set_text", "set_value", "select_option", "check",
            "uncheck", "toggle", "submit", "scroll_into_view", "wait",
        ),
        "browser.forms": (
            "set_text", "set_value", "select_option", "check", "uncheck",
            "toggle", "submit", "scroll_into_view", "wait",
        ),
        "browser.links": ("invoke", "scroll_into_view", "wait"),
        "browser.storage": (),
        "browser.downloads": ("wait",),
        "browser.navigation_state": ("wait",),
    }
    action_schemas = {
        "open_tab": {
            "type": "object", "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
        "switch_tab": {"type": "object", "properties": {}, "additionalProperties": False},
        "close_tab": {"type": "object", "properties": {}, "additionalProperties": False},
        "navigate": {
            "type": "object", "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
        "reload": {"type": "object", "properties": {}, "additionalProperties": False},
        "back": {"type": "object", "properties": {}, "additionalProperties": False},
        "forward": {"type": "object", "properties": {}, "additionalProperties": False},
        "invoke": {"type": "object", "properties": {}, "additionalProperties": False},
        "set_text": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        "set_value": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        "select_option": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
        "check": {"type": "object", "properties": {}, "additionalProperties": False},
        "uncheck": {"type": "object", "properties": {}, "additionalProperties": False},
        "toggle": {"type": "object", "properties": {}, "additionalProperties": False},
        "submit": {"type": "object", "properties": {}, "additionalProperties": False},
        "scroll_into_view": {
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        "wait": {
            "type": "object",
            "properties": {"ms": {"type": "integer", "minimum": 0, "maximum": 30_000}},
            "additionalProperties": False,
        },
    }

    def __init__(
        self,
        vm_ip: str,
        *,
        port: int = 9222,
        fallback_ports: tuple[int, ...] = (1337,),
        call_timeout_seconds: float = 45.0,
        initial_active_url: str | None = None,
    ) -> None:
        self.vm_ip = vm_ip
        self.ports = (port, *fallback_ports)
        self.call_timeout_seconds = call_timeout_seconds
        self.initial_active_url = initial_active_url
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"semantic-browser-{vm_ip}-{port}",
            daemon=True,
        )
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright = None
        self._browser = None
        self._active = None
        self._native: dict[str, dict[str, Any]] = {}
        self._surface_ids: dict[int, str] = {}
        self._closed = False
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("semantic browser event loop did not start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _call(self, coroutine):
        if self._closed or self._loop is None:
            raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "browser adapter is closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self.call_timeout_seconds)
        except FutureTimeout as error:
            future.cancel()
            raise ProtocolError(
                ErrorCode.TIMEOUT,
                "browser semantic operation timed out",
                retryable=True,
            ) from error

    async def _connect(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except Exception as error:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                "async Playwright is unavailable",
                retryable=True,
            ) from error
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        last_error: Exception | None = None
        for candidate in self.ports:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://{self.vm_ip}:{candidate}", timeout=15_000
                )
                break
            except Exception as error:
                last_error = error
        if self._browser is None:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                f"Chrome CDP is unavailable on configured ports: {type(last_error).__name__}",
                retryable=True,
            )
        return self._browser

    async def _context(self):
        browser = await self._connect()
        if not browser.contexts:
            raise ProtocolError(ErrorCode.ADAPTER_UNAVAILABLE, "Chrome has no browser context")
        return browser.contexts[0]

    async def _reconnect_after_browser_restart(self) -> None:
        """Discard dead CDP objects after a guarded browser relaunch.

        Stopping Playwright only disconnects this client. The guest adapter is
        solely responsible for starting/stopping the actual Chrome process.
        """
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        self._active = None
        self._native.clear()
        self._surface_ids.clear()
        last_error: ProtocolError | None = None
        for _ in range(20):
            try:
                await self._connect()
                return
            except ProtocolError as error:
                last_error = error
                await asyncio.sleep(0.25)
        raise ProtocolError(
            ErrorCode.ADAPTER_UNAVAILABLE,
            "Chrome CDP did not reconnect after guarded relaunch",
            retryable=True,
        ) from last_error

    async def _page(self):
        context = await self._context()
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            raise ProtocolError(ErrorCode.NOT_FOUND, "Chrome has no open page")
        if self._active in pages:
            return self._active
        if self.initial_active_url:
            expected = self.initial_active_url.rstrip("/")
            for page in reversed(pages):
                if page.url.rstrip("/") == expected:
                    self._active = page
                    self.initial_active_url = None
                    return page
        self._active = pages[-1]
        return self._active

    def _remember(self, kind: str, identity: Any, payload: Mapping[str, Any]) -> str:
        native_ref = f"native_{hashlib.sha256(f'{kind}:{identity}'.encode()).hexdigest()[:30]}"
        self._native[native_ref] = {"kind": kind, **dict(payload)}
        return native_ref

    def _surface_id(self, page: Any) -> str:
        """Return an episode-local opaque identity for one browser page.

        The identity deliberately contains neither CDP target IDs nor any
        selector/geometry.  It is stable for the lifetime of the Playwright
        page object and exists so the semantic kernel can prove target-set
        deltas across actions initiated from another application.
        """

        identity = id(page)
        surface_id = self._surface_ids.get(identity)
        if surface_id is None:
            surface_id = f"surface_{secrets.token_urlsafe(18)}"
            self._surface_ids[identity] = surface_id
        return surface_id

    async def _tabs(self) -> list[dict[str, Any]]:
        context = await self._context()
        pages = [page for page in context.pages if not page.is_closed()]
        active = await self._page()
        records = []
        for index, page in enumerate(pages):
            try:
                title = await page.title()
            except Exception:
                title = ""
            ref = self._remember("tab", id(page), {"page": page})
            records.append({
                "ref": ref,
                "kind": "browser.tab",
                "surface_id": self._surface_id(page),
                "index": index,
                "url": _public_url(page.url),
                "title": title[:500],
                "active": page is active,
                "advertised_actions": [
                    "switch_tab", "close_tab", "navigate", "reload", "back", "forward",
                ],
            })
        return records

    async def _target_snapshot(
        self,
        previous_surface_ids: frozenset[str] | None,
        timeout_ms: int,
    ) -> list[dict[str, Any]]:
        """Observe browser surfaces, optionally waiting for a stable set delta."""

        if previous_surface_ids is None or timeout_ms <= 0:
            return await self._tabs()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(max(timeout_ms, 0), 2_000) / 1_000
        changed_ids: frozenset[str] | None = None
        changed_at = 0.0
        latest: list[dict[str, Any]] = []
        while True:
            latest = await self._tabs()
            current_ids = frozenset(
                str(record["surface_id"])
                for record in latest
                if isinstance(record.get("surface_id"), str)
            )
            now = loop.time()
            if current_ids != previous_surface_ids:
                if current_ids != changed_ids:
                    changed_ids = current_ids
                    changed_at = now
                elif now - changed_at >= 0.1:
                    return latest
            if now >= deadline:
                return latest
            await asyncio.sleep(0.05)

    def target_snapshot(
        self,
        *,
        previous_surface_ids: frozenset[str] | None = None,
        timeout_ms: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Kernel-private target observation used for cross-app receipts."""

        return tuple(self._call(self._target_snapshot(previous_surface_ids, timeout_ms)))

    async def _frames(self) -> list[dict[str, Any]]:
        page = await self._page()
        records = []
        for index, frame in enumerate(page.frames):
            ref = self._remember("frame", f"{id(page)}:{index}:{frame.url}", {
                "page": page, "frame": frame,
            })
            records.append({
                "ref": ref, "kind": "browser.frame", "index": index,
                "name": frame.name or "", "url": _public_url(frame.url),
                "main": frame is page.main_frame,
                "advertised_actions": [],
            })
        return records

    async def _ax_records(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        page = await self._page()
        context = await self._context()
        session = await context.new_cdp_session(page)
        try:
            await session.send("Accessibility.enable")
            frame_tree = await session.send("Page.getFrameTree")
            frames: list[dict[str, Any]] = []

            def collect_frame(node: Mapping[str, Any]) -> None:
                frame = node.get("frame")
                if isinstance(frame, Mapping) and frame.get("id"):
                    frames.append(dict(frame))
                for child in node.get("childFrames") or []:
                    if isinstance(child, Mapping):
                        collect_frame(child)

            root_tree = frame_tree.get("frameTree")
            if isinstance(root_tree, Mapping):
                collect_frame(root_tree)
            trees: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
            for frame in frames or [{"id": None, "url": page.url}]:
                parameters = {"frameId": frame["id"]} if frame.get("id") else {}
                try:
                    tree = await session.send("Accessibility.getFullAXTree", parameters)
                except Exception:
                    # A cross-process frame can disappear between the frame-tree
                    # snapshot and its AX query. The enclosing page remains
                    # queryable; its next revision will expose the new state.
                    continue
                trees.append((frame, list(tree.get("nodes") or [])))
        finally:
            await session.detach()
        return _compile_ax_records(page, trees, self._remember)

    async def _text(self) -> list[dict[str, Any]]:
        page = await self._page()
        text = await page.locator("body").inner_text(timeout=10_000)
        ref = self._remember("page_text", id(page), {"page": page})
        return [{
            "ref": ref, "kind": "browser.text", "text": text[:64_000],
            "url": _public_url(page.url), "advertised_actions": [],
        }]

    async def _storage(self) -> list[dict[str, Any]]:
        page = await self._page()
        context = await self._context()
        cookies = await context.cookies([page.url])
        local = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
        ref = self._remember("storage", id(page), {"page": page})
        return [{
            "ref": ref, "kind": "browser.storage",
            "cookies": [
                {
                    key: cookie.get(key)
                    for key in (
                        "name", "domain", "path", "expires", "httpOnly", "secure", "sameSite"
                    )
                }
                for cookie in cookies
            ],
            "local_storage": [
                {
                    "key": str(key)[:500],
                    "value_length": len(str(value)),
                    "value_sha256": hashlib.sha256(str(value).encode()).hexdigest(),
                }
                for key, value in list(local.items())[:1_000]
            ],
            "secret_values_redacted": True,
            "advertised_actions": [],
        }]

    async def _observe(self, resource: str) -> AdapterObservation:
        summary: dict[str, Any] | None = None
        if resource in {"browser.targets", "browser.tabs"}:
            records = await self._tabs()
        elif resource == "browser.frames":
            records = await self._frames()
        elif resource in {"browser.elements", "browser.forms", "browser.links"}:
            records, summary = await self._ax_records()
            if resource == "browser.forms":
                records = [r for r in records if r.get("role") == "form"]
            elif resource == "browser.links":
                records = [r for r in records if r.get("role") == "link"]
        elif resource == "browser.text":
            records = await self._text()
        elif resource == "browser.storage":
            records = await self._storage()
        elif resource == "browser.downloads":
            records = []
        elif resource in {"browser.page", "browser.navigation_state"}:
            page = await self._page()
            title = await page.title()
            ref = self._remember("page", id(page), {"page": page})
            records = [{
                "ref": ref, "kind": "browser.page", "url": _public_url(page.url),
                "title": title[:500], "load_state": "live",
                "advertised_actions": ["navigate", "reload", "back", "forward", "open_tab"],
            }]
        else:
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, resource)
        public_digest = [
            {key: value for key, value in record.items() if key != "ref"}
            for record in records
        ]
        return AdapterObservation(
            items=records,
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={"record_count": len(records), **(summary or {})},
            native_revision=f"browser_{_digest(public_digest)[:20]}",
        )

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        return self._call(self._observe(context.resource))

    async def _resolve_object(self, page, backend_node_id: int):
        context = await self._context()
        session = await context.new_cdp_session(page)
        try:
            resolved = await session.send(
                "DOM.resolveNode", {"backendNodeId": backend_node_id}
            )
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                raise ProtocolError(ErrorCode.STALE_REF, "browser node no longer resolves")
            return session, object_id
        except Exception:
            await session.detach()
            raise

    async def _call_on_node(self, native: Mapping[str, Any], declaration: str, arguments=()):
        page = native["page"]
        session, object_id = await self._resolve_object(page, native["backend_node_id"])
        try:
            result = await session.send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": declaration,
                "arguments": [{"value": value} for value in arguments],
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            })
            if result.get("exceptionDetails"):
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    "browser page action raised after mutation began",
                    side_effect_state=SideEffectState.UNKNOWN,
                )
            return (result.get("result") or {}).get("value")
        finally:
            await session.detach()

    async def _reconcile_text_mutation(
        self, native: Mapping[str, Any], expected: str,
    ) -> str | None:
        """Prove an uncertain text mutation from fresh read-only state.

        Input/change handlers may synchronously replace the DOM node or start
        navigation after the value setter has applied. CDP can then report an
        execution-context exception instead of the function's return value.
        Reclassify only when the requested value is subsequently observed on
        the same backend node or on one unique semantic replacement. Any
        missing, ambiguous, or mismatched state deliberately stays uncertain.
        """

        page = native.get("page")
        identity = native.get("semantic_identity")
        for delay_ms in (50, 150, 300):
            try:
                if page is not None and hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(delay_ms)
                else:
                    await asyncio.sleep(delay_ms / 1_000)
            except Exception:
                # A navigation may transiently invalidate the Playwright page
                # wait itself. The fresh semantic snapshot below is the proof.
                await asyncio.sleep(delay_ms / 1_000)

            try:
                observed = await self._call_on_node(
                    native, "function(){ return String(this.value ?? ''); }",
                )
            except Exception:
                observed = None
            if observed is not None and str(observed) == expected:
                return "same_backend_value"

            if not isinstance(identity, Mapping):
                continue
            role = str(identity.get("role") or "").casefold()
            name = str(identity.get("name") or "")
            description = str(identity.get("description") or "")
            frame_name = str(identity.get("frame_name") or "")
            # Role alone is not enough to claim continuity across navigation.
            if not role or not (name or description):
                continue
            try:
                records, _summary = await self._ax_records()
            except Exception:
                continue
            candidates = [
                record for record in records
                if str(record.get("role") or "").casefold() == role
                and str(record.get("name") or "") == name
                and str(record.get("frame_name") or "") == frame_name
            ]
            if len(candidates) > 1 and description:
                candidates = [
                    record for record in candidates
                    if str(record.get("description") or "") == description
                ]
            if (
                len(candidates) == 1
                and (replacement_value := candidates[0].get("value")) is not None
                and replacement_value != "[redacted]"
                and str(replacement_value) == expected
            ):
                return "unique_replacement_value"
        return None

    async def _semantic_input_invoke(self, native: Mapping[str, Any]) -> None:
        """Invoke one uniquely resolved node through a private hit-tested gesture.

        Some browser capabilities, most importantly file inputs, reject a DOM
        ``element.click()`` because it is not a trusted user activation.  The
        adapter may use geometry privately after semantic target resolution,
        but neither the coordinates nor the backend node identity cross the
        model-facing protocol boundary.
        """
        page = native["page"]
        backend_node_id = int(native["backend_node_id"])
        await page.bring_to_front()
        context = await self._context()
        session = await context.new_cdp_session(page)
        try:
            await session.send(
                "DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_node_id}
            )
            model = await session.send(
                "DOM.getBoxModel", {"backendNodeId": backend_node_id}
            )
            box = model.get("model") or {}
            quad = box.get("border") or box.get("content")
            if not isinstance(quad, list) or len(quad) != 8:
                raise ProtocolError(
                    ErrorCode.UNSUPPORTED,
                    "browser target has no actionable private geometry",
                )
            x = sum(float(quad[index]) for index in (0, 2, 4, 6)) / 4.0
            y = sum(float(quad[index]) for index in (1, 3, 5, 7)) / 4.0
            hit = await session.send(
                "DOM.getNodeForLocation",
                {
                    "x": int(round(x)),
                    "y": int(round(y)),
                    "includeUserAgentShadowDOM": True,
                    "ignorePointerEventsNone": False,
                },
            )
            hit_backend = int(hit.get("backendNodeId") or -1)
            hit_matches = hit_backend == backend_node_id
            if not hit_matches and hit_backend >= 0:
                # Native form controls can expose an implementation node in a
                # user-agent shadow tree at their visual center.  Accept it
                # only when the browser itself proves that climbing composed
                # parents/hosts reaches the exact semantic target object.
                target = await session.send(
                    "DOM.resolveNode", {"backendNodeId": backend_node_id}
                )
                child = await session.send(
                    "DOM.resolveNode", {"backendNodeId": hit_backend}
                )
                target_object = (target.get("object") or {}).get("objectId")
                child_object = (child.get("object") or {}).get("objectId")
                if target_object and child_object:
                    ancestry = await session.send("Runtime.callFunctionOn", {
                        "objectId": target_object,
                        "functionDeclaration": (
                            "function(node){ for(let i=0; node && i<64; i++){ "
                            "if(node===this) return true; const root=node.getRootNode?.(); "
                            "node=(root&&root.host&&root!==node)?root.host:node.parentNode; "
                            "} return false; }"
                        ),
                        "arguments": [{"objectId": child_object}],
                        "returnByValue": True,
                    })
                    hit_matches = (ancestry.get("result") or {}).get("value") is True
            if not hit_matches:
                raise ProtocolError(
                    ErrorCode.STALE_REF,
                    "browser target failed private hit testing",
                    retryable=True,
                )
            common = {
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
                "modifiers": 0,
            }
            await session.send(
                "Input.dispatchMouseEvent", {"type": "mousePressed", **common}
            )
            await session.send(
                "Input.dispatchMouseEvent", {"type": "mouseReleased", **common}
            )
        finally:
            await session.detach()

    async def _is_file_input(self, native: Mapping[str, Any]) -> bool:
        """Classify a resolved browser node without exposing DOM selectors.

        File inputs are the one ordinary page control whose activation must be
        a trusted user gesture in order for Chrome to open its native chooser.
        All other invokable elements should use their native DOM ``click``
        method rather than depending on private geometry and center hit tests.
        """

        result = await self._call_on_node(
            native,
            "function(){ return this instanceof HTMLInputElement && "
            "String(this.type || '').toLowerCase() === 'file'; }",
        )
        return result is True

    async def _act(self, payload: Mapping[str, Any]) -> AdapterActionResult:
        target = payload.get("target") or {}
        native_ref = target.get("ref")
        native = self._native.get(str(native_ref))
        if native is None:
            raise ProtocolError(ErrorCode.STALE_REF, "browser ref no longer resolves", retryable=True)
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        kind = native.get("kind")
        page = native.get("page") or await self._page()
        mutation_reconciliation: str | None = None
        open_tab_evidence: dict[str, Any] = {}
        if action == "open_tab":
            context = await self._context()
            before_pages = [item for item in context.pages if not item.is_closed()]
            before_surface_ids = {
                self._surface_id(item) for item in before_pages
            }
            page = await context.new_page()
            self._active = page
            if arguments.get("url"):
                await page.goto(str(arguments["url"]), wait_until="domcontentloaded")
            await page.bring_to_front()
            after_pages = [item for item in context.pages if not item.is_closed()]
            after_surface_ids = {
                self._surface_id(item) for item in after_pages
            }
            opened_surface_id = self._surface_id(page)
            if (
                not before_surface_ids.issubset(after_surface_ids)
                or opened_surface_id in before_surface_ids
                or opened_surface_id not in after_surface_ids
            ):
                raise ProtocolError(
                    ErrorCode.POSTCONDITION_FAILED,
                    "new tab was created but preservation of existing tabs was not proved",
                    side_effect_state=SideEffectState.APPLIED,
                )
            open_tab_evidence = {
                "opened_surface_id": opened_surface_id,
                "opened_url": _public_url(page.url),
                "tab_count_before": len(before_surface_ids),
                "tab_count_after": len(after_surface_ids),
                "existing_tabs_preserved": True,
            }
        elif action == "switch_tab" and kind == "tab":
            self._active = page
            await page.bring_to_front()
        elif action == "close_tab" and kind == "tab":
            await page.close()
            if self._active is page:
                self._active = None
        elif action == "navigate":
            await page.goto(str(arguments.get("url") or "about:blank"), wait_until="domcontentloaded")
        elif action == "reload":
            await page.reload(wait_until="domcontentloaded")
        elif action == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded")
        elif action == "wait":
            await page.wait_for_timeout(min(30_000, max(0, int(arguments.get("ms", 500)))))
        elif kind != "backend_node":
            raise ProtocolError(ErrorCode.UNSUPPORTED, f"{action} requires a browser element")
        elif action == "invoke":
            if await self._is_file_input(native):
                await self._semantic_input_invoke(native)
                execution_path = "semantic_input"
            else:
                await self._call_on_node(
                    native,
                    "function(){ if (typeof this.click !== 'function') "
                    "throw new Error('not invokable'); this.click(); return true; }",
                )
                execution_path = "native_api"
        elif action in {"set_text", "set_value"}:
            expected = str(arguments.get("value", ""))
            try:
                await self._call_on_node(
                    native,
                    "function(value){ this.focus(); const p = this instanceof HTMLTextAreaElement ? "
                    "HTMLTextAreaElement.prototype : HTMLInputElement.prototype; const s = "
                    "Object.getOwnPropertyDescriptor(p, 'value')?.set; if (!s) throw new Error('not editable'); "
                    "s.call(this, String(value)); this.dispatchEvent(new InputEvent('input',{bubbles:true})); "
                    "this.dispatchEvent(new Event('change',{bubbles:true})); return this.value; }",
                    (expected,),
                )
            except ProtocolError as error:
                if (
                    action != "set_text"
                    or error.side_effect_state is not SideEffectState.UNKNOWN
                ):
                    raise
                mutation_reconciliation = await self._reconcile_text_mutation(
                    native, expected,
                )
                if mutation_reconciliation is None:
                    raise
            except Exception:
                if action != "set_text":
                    raise
                mutation_reconciliation = await self._reconcile_text_mutation(
                    native, expected,
                )
                if mutation_reconciliation is None:
                    raise
        elif action in {"check", "uncheck", "toggle"}:
            wanted = None if action == "toggle" else action == "check"
            await self._call_on_node(
                native,
                "function(wanted){ const current = 'checked' in this ? !!this.checked : "
                "this.getAttribute('aria-checked') === 'true'; if (wanted === null || current !== wanted) "
                "this.click(); return true; }",
                (wanted,),
            )
        elif action == "select_option":
            await self._call_on_node(
                native,
                "function(value){ const option = Array.from(this.options || []).find(o => "
                "o.value === String(value) || o.text.trim() === String(value)); if (!option) "
                "throw new Error('option not found'); this.value = option.value; "
                "this.dispatchEvent(new Event('change',{bubbles:true})); return this.value; }",
                (arguments.get("value"),),
            )
        elif action == "submit":
            await self._call_on_node(
                native,
                "function(){ const form = this instanceof HTMLFormElement ? this : this.form; "
                "if (!form) throw new Error('no form'); form.requestSubmit(); return true; }",
            )
        elif action == "scroll_into_view":
            await self._call_on_node(
                native,
                "function(){ this.scrollIntoView({block:'center',inline:'center'}); return true; }",
            )
        else:
            raise ProtocolError(ErrorCode.UNSUPPORTED, f"unsupported browser action: {action}")
        return AdapterActionResult(
            changed=True,
            result={
                "execution_path": (
                    execution_path if action == "invoke" else "native_api"
                ),
                "action": action,
                **open_tab_evidence,
                **(
                    {
                        "verification": "fresh_semantic_value",
                        "reconciliation": mutation_reconciliation,
                    }
                    if mutation_reconciliation is not None else {}
                ),
            },
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            status=Status.OK,
        )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        try:
            return self._call(self._act(payload))
        except ProtocolError as error:
            if error.code in {
                ErrorCode.TIMEOUT,
                ErrorCode.ADAPTER_UNAVAILABLE,
                ErrorCode.INTERNAL_ERROR,
            } and error.side_effect_state is SideEffectState.NONE:
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    error.message,
                    retryable=False,
                    side_effect_state=SideEffectState.UNKNOWN,
                ) from error
            raise
        except Exception as error:
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                f"browser action transport failed: {type(error).__name__}",
                side_effect_state=SideEffectState.UNKNOWN,
            ) from error

    async def _close_async(self) -> None:
        if self._playwright is not None:
            # Stopping Playwright disconnects CDP without closing Chrome.
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call(self._close_async())
        finally:
            self._closed = True
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
