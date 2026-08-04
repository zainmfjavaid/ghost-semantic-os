"""Semantic Chrome-chrome adapter over privileged internal APIs and CDP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, Callable, Mapping

from .adapters import AdapterActionResult, AdapterContext, AdapterObservation, SemanticAdapter
from .browser_adapter import AsyncBrowserAdapter, _public_url
from .protocol import ErrorCode, ProtocolError, SideEffectState, Status


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class _ChromePreMutationError(ProtocolError):
    """Private marker proving that Chrome mutation execution never started."""


class ChromeSemanticAdapter(SemanticAdapter):
    adapter_id = "chrome.semantic@1"
    resources = frozenset({
        "chrome.profile", "chrome.bookmarks", "chrome.history", "chrome.settings",
        "chrome.privacy", "chrome.extensions", "chrome.downloads", "chrome.print_jobs",
        "chrome.toolbar_state", "chrome.internal_pages",
    })
    capabilities = frozenset({
        "create_bookmark", "update_bookmark", "move_bookmark", "delete_bookmark",
        "set_pref", "delete_history", "clear_history", "enable_extension",
        "disable_extension", "uninstall_extension", "load_unpacked", "save_pdf",
        "create_shortcut",
    })

    def __init__(
        self,
        browser: AsyncBrowserAdapter,
        guest_request: Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]],
    ) -> None:
        self.browser = browser
        self.guest_request = guest_request
        self._native: dict[str, dict[str, Any]] = {}
        self._loaded_extension_paths: dict[str, str] = {}

    def _remember(self, kind: str, identity: Any, record: Mapping[str, Any]) -> str:
        ref = f"chrome_{hashlib.sha256(f'{kind}:{identity}'.encode()).hexdigest()[:28]}"
        self._native[ref] = {"kind": kind, **dict(record)}
        return ref

    async def _internal(
        self,
        url: str,
        expression: str,
        argument: Any = None,
        *,
        mutation: bool = False,
        capability_expression: str | None = None,
        missing_capability: str | None = None,
    ) -> Any:
        context = await self.browser._context()
        page = await context.new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except Exception as error:
                raise _ChromePreMutationError(
                    ErrorCode.ADAPTER_UNAVAILABLE,
                    f"Chrome internal surface is unavailable: {type(error).__name__}",
                    retryable=True,
                    missing_capability=missing_capability,
                ) from error
            if capability_expression is not None:
                try:
                    available = await page.evaluate(capability_expression)
                except Exception as error:
                    raise _ChromePreMutationError(
                        ErrorCode.ADAPTER_UNAVAILABLE,
                        f"Chrome capability probe failed: {type(error).__name__}",
                        retryable=True,
                        missing_capability=missing_capability,
                    ) from error
                if available is not True:
                    raise _ChromePreMutationError(
                        ErrorCode.REPRESENTATION_GAP,
                        "Chrome does not expose the required generic internal capability",
                        missing_capability=missing_capability,
                    )
            try:
                return await page.evaluate(expression, argument)
            except Exception as error:
                if mutation:
                    detail = str(error).replace("\n", " ")[:500]
                    raise ProtocolError(
                        ErrorCode.UNCERTAIN,
                        "Chrome internal action did not establish final state: "
                        f"{type(error).__name__}" + (f" ({detail})" if detail else ""),
                        retryable=False,
                        side_effect_state=SideEffectState.UNKNOWN,
                        missing_capability=missing_capability,
                    ) from error
                raise ProtocolError(
                    ErrorCode.ADAPTER_UNAVAILABLE,
                    f"Chrome internal API is unavailable: {type(error).__name__}",
                    retryable=True,
                    missing_capability=missing_capability,
                ) from error
        except ProtocolError:
            raise
        finally:
            try:
                await page.close()
            except Exception:
                # Closing this private inspection surface is cleanup only.  It
                # must not overwrite the truthful state of the operation above.
                pass

    def _profile_query(self, kind: str) -> list[dict[str, Any]]:
        response = self.guest_request("POST", "/v1/query", {
            "resource": "chrome.private.profile",
            "scope": {},
            "where": {},
            "fields": [],
            "order_by": [],
            "parameters": {"kind": kind},
            "limit": 100,
            "internal_offset": 0,
            "freshness": "live",
        })
        if not response.get("ok"):
            error = response.get("error") if isinstance(response, Mapping) else None
            error = error if isinstance(error, Mapping) else {}
            try:
                code = ErrorCode(str(error.get("code") or "adapter_unavailable"))
            except ValueError:
                code = ErrorCode.INTERNAL_ERROR
            raise ProtocolError(
                code,
                str(error.get("message") or "Chrome profile query failed")[:2_000],
                retryable=bool(error.get("retryable", False)),
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome profile query is invalid")
        records = result.get("records")
        if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome profile records are invalid")
        # Private queries are currently bounded to 100 records.  Follow the
        # guest-only offset until one stable collection is assembled.
        output = [dict(record) for record in records]
        revision = result.get("revision")
        offset = result.get("next_internal_offset")
        while offset is not None:
            if not isinstance(offset, int) or offset <= 0 or len(output) > 5_000:
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome private cursor is invalid")
            page = self.guest_request("POST", "/v1/query", {
                "resource": "chrome.private.profile", "scope": {}, "where": {},
                "fields": [], "order_by": [], "parameters": {"kind": kind},
                "limit": 100, "internal_offset": offset, "freshness": "live",
            })
            page_result = page.get("result") if isinstance(page, Mapping) else None
            if not page.get("ok") or not isinstance(page_result, Mapping):
                raise ProtocolError(
                    ErrorCode.ADAPTER_UNAVAILABLE,
                    "Chrome profile paging failed",
                    retryable=True,
                )
            if page_result.get("revision") != revision:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "Chrome profile changed while being queried",
                    retryable=True,
                )
            page_records = page_result.get("records")
            if not isinstance(page_records, list):
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome profile page is invalid")
            output.extend(dict(item) for item in page_records if isinstance(item, Mapping))
            new_offset = page_result.get("next_internal_offset")
            if new_offset is not None and new_offset <= offset:
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome private cursor repeated")
            offset = new_offset
        return output

    async def _bookmarks(self) -> list[dict[str, Any]]:
        try:
            tree = await self._internal(
                "chrome://bookmarks/",
                "() => new Promise((resolve, reject) => { if (!chrome.bookmarks) "
                "return reject(new Error('bookmarks API unavailable')); chrome.bookmarks.getTree(resolve); })",
            )
        except ProtocolError as error:
            if error.code not in {ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED}:
                raise
            tree = None
        records: list[dict[str, Any]] = []
        ids_to_refs: dict[str, str] = {}
        if tree is None:
            profile_records = self._profile_query("bookmarks")
            for node in profile_records:
                node_id = str(node.get("id") or "")
                ref = self._remember("bookmark", node_id, {"id": node_id})
                ids_to_refs[node_id] = ref
                records.append({
                    "ref": ref,
                    "kind": "chrome.bookmark",
                    "title": str(node.get("title") or "")[:2_000],
                    "url": _public_url(node.get("url")) if node.get("url") else None,
                    "_parent_id": node.get("parent_id"),
                    "folder": bool(node.get("folder", False)),
                    "advertised_actions": [
                        "update_bookmark", "move_bookmark", "delete_bookmark", "create_bookmark",
                    ],
                    "execution_source": "profile_database",
                })
        else:
            def visit(node: Mapping[str, Any], parent: str | None = None) -> None:
                node_id = str(node.get("id") or "")
                ref = self._remember("bookmark", node_id, {"id": node_id})
                ids_to_refs[node_id] = ref
                records.append({
                    "ref": ref, "kind": "chrome.bookmark",
                    "title": str(node.get("title") or "")[:2_000],
                    "url": _public_url(node.get("url")) if node.get("url") else None,
                    "_parent_id": parent,
                    "folder": "url" not in node,
                    "advertised_actions": [
                        "update_bookmark", "move_bookmark", "delete_bookmark", "create_bookmark",
                    ],
                    "execution_source": "chrome_internal_api",
                })
                for child in node.get("children") or []:
                    if isinstance(child, Mapping):
                        visit(child, node_id)
            for root in tree or []:
                if isinstance(root, Mapping):
                    visit(root)
        for record in records:
            parent_id = record.pop("_parent_id", None)
            record["parent_ref"] = ids_to_refs.get(str(parent_id)) if parent_id else None
        return records

    async def _settings(self) -> list[dict[str, Any]]:
        try:
            prefs = await self._internal(
                "chrome://settings/",
                "() => new Promise((resolve, reject) => { if (!chrome.settingsPrivate) "
                "return reject(new Error('settingsPrivate unavailable')); "
                "chrome.settingsPrivate.getAllPrefs(resolve); })",
            )
            source = "chrome_internal_api"
        except ProtocolError as error:
            if error.code not in {ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED}:
                raise
            prefs = self._profile_query("settings")
            source = "profile_database"
        records = []
        for pref in prefs or []:
            if not isinstance(pref, Mapping) or not pref.get("key"):
                continue
            key = str(pref["key"])
            ref = self._remember("preference", key, {"key": key})
            records.append({
                "ref": ref, "kind": "chrome.setting", "key": key,
                "value": pref.get("value"), "type": pref.get("type"),
                "controlled_by": pref.get("controlledBy"),
                "enforcement": pref.get("enforcement"),
                "secret_value_redacted": pref.get("secret_value_redacted", False),
                "execution_source": source,
                "advertised_actions": ["set_pref"],
            })
        return records

    async def _history(self) -> list[dict[str, Any]]:
        try:
            records = await self._internal(
                "chrome://history/",
                "() => new Promise((resolve, reject) => { if (!chrome.history) "
                "return reject(new Error('history API unavailable')); chrome.history.search({text:'',maxResults:1000},resolve); })",
            )
            source = "chrome_internal_api"
        except ProtocolError as error:
            if error.code not in {ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED}:
                raise
            records = self._profile_query("history")
            source = "profile_database"
        output = []
        for entry in records or []:
            if not isinstance(entry, Mapping):
                continue
            native_url = str(entry.get("url") or "")
            url = _public_url(native_url)
            ref = self._remember("history", native_url, {"url": native_url})
            output.append({
                "ref": ref, "kind": "chrome.history_entry", "url": url,
                "title": str(entry.get("title") or "")[:2_000],
                "last_visit_time": entry.get("lastVisitTime", entry.get("last_visit_time")),
                "visit_count": entry.get("visitCount", entry.get("visit_count")),
                "execution_source": source,
                "advertised_actions": ["delete_history", "clear_history"],
            })
        return output

    async def _extensions(self) -> list[dict[str, Any]]:
        try:
            extensions = await self._internal(
                "chrome://extensions/",
                "() => new Promise((resolve, reject) => { if (!chrome.management) "
                "return reject(new Error('management API unavailable')); chrome.management.getAll(resolve); })",
            )
            source = "chrome_internal_api"
        except ProtocolError as error:
            if error.code not in {ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED}:
                raise
            extensions = self._profile_query("extensions")
            source = "profile_database"
        records = []
        for extension in extensions or []:
            if not isinstance(extension, Mapping):
                continue
            extension_id = str(extension.get("id") or "")
            ref = self._remember("extension", extension_id, {"id": extension_id})
            records.append({
                "ref": ref, "kind": "chrome.extension", "id": extension_id,
                "name": str(extension.get("name") or "")[:2_000],
                "version": extension.get("version"), "enabled": extension.get("enabled"),
                "install_type": extension.get("installType", extension.get("install_type")),
                "path": (
                    extension.get("path")
                    or self._loaded_extension_paths.get(extension_id)
                ),
                "execution_source": source,
                "advertised_actions": [
                    "enable_extension", "disable_extension", "uninstall_extension", "load_unpacked",
                ],
            })
        return records

    async def _load_unpacked_via_internal_ui(self, path: str) -> dict[str, Any]:
        """Use Chrome's real developer UI and the semantic native chooser.

        Google Chrome branded builds reject the command-line load-extension
        switch.  The internal Extensions surface remains the authoritative
        generic route and persists exactly the state created by a user pressing
        Load unpacked.  All DOM selectors and chooser-native refs stay private.
        """
        context = await self.browser._context()
        page = await context.new_page()
        chooser_opened = False
        click_task: asyncio.Task[Any] | None = None
        try:
            await page.goto(
                "chrome://extensions/", wait_until="domcontentloaded", timeout=15_000
            )
            await page.bring_to_front()
            developer = page.locator("extensions-toolbar #devMode")
            load_button = page.locator("extensions-toolbar #loadUnpacked")
            await developer.wait_for(state="attached", timeout=10_000)
            enabled = await developer.evaluate("element => !!element.checked")
            if not enabled:
                await developer.click(timeout=10_000)
                await page.wait_for_function(
                    "() => document.querySelector('extensions-manager')"
                    ".shadowRoot.querySelector('extensions-toolbar')"
                    ".shadowRoot.querySelector('#devMode').checked === true",
                    timeout=10_000,
                )
            await load_button.wait_for(state="visible", timeout=10_000)
            # Chrome keeps the high-level click pending while its modal native
            # chooser is open.  Keep that task alive while guest AT-SPI calls
            # run off the browser loop, then join it after the chooser closes.
            click_task = asyncio.create_task(load_button.click(timeout=30_000))
            await asyncio.sleep(0)
            chooser_opened = True

            chooser: Mapping[str, Any] | None = None
            # AT-SPI may publish the native chooser object before its child
            # actions are stable. Allow the ordinary UI-realization interval;
            # the guest route re-queries after every action to prove progress.
            for _ in range(20):
                response = await asyncio.to_thread(self.guest_request, "POST", "/v1/query", {
                    "resource": "os.file_choosers", "scope": {}, "where": {},
                    "fields": [], "order_by": [], "parameters": {},
                    "limit": 100, "internal_offset": 0, "freshness": "live",
                })
                result = response.get("result") if isinstance(response, Mapping) else None
                records = result.get("records") if isinstance(result, Mapping) else None
                if response.get("ok") and isinstance(records, list) and len(records) == 1:
                    chooser = records[0] if isinstance(records[0], Mapping) else None
                    if chooser is not None:
                        break
                await asyncio.sleep(0.25)
            if chooser is None or not isinstance(chooser.get("ref"), str):
                diagnostics = await asyncio.to_thread(
                    self.guest_request, "POST", "/v1/query", {
                        "resource": "os.dialogs", "scope": {}, "where": {},
                        "fields": [], "order_by": [], "parameters": {},
                        "limit": 100, "internal_offset": 0, "freshness": "live",
                    },
                )
                diagnostic_result = (
                    diagnostics.get("result") if isinstance(diagnostics, Mapping) else None
                )
                diagnostic_records = (
                    diagnostic_result.get("records")
                    if isinstance(diagnostic_result, Mapping) else []
                )
                names = [
                    str(item.get("name") or item.get("role") or "")[:200]
                    for item in diagnostic_records or [] if isinstance(item, Mapping)
                ][:10]
                click_state = "pending"
                if click_task.done():
                    try:
                        await click_task
                        click_state = "completed"
                    except Exception as error:
                        click_state = f"failed:{type(error).__name__}"
                raise ProtocolError(
                    ErrorCode.POSTCONDITION_FAILED,
                    "Chrome opened no uniquely represented native extension chooser "
                    f"(click={click_state}, dialogs={names})",
                    side_effect_state=SideEffectState.NONE,
                    missing_capability="os.file_choosers.choose_path",
                )
            selected = await asyncio.to_thread(self.guest_request, "POST", "/v1/act", {
                "target": {"ref": chooser["ref"]},
                "action": "choose_path",
                "arguments": {"path": path},
            })
            if not selected.get("ok"):
                raw = selected.get("error") if isinstance(selected, Mapping) else None
                error = raw if isinstance(raw, Mapping) else {}
                try:
                    code = ErrorCode(str(error.get("code") or "internal_error"))
                except ValueError:
                    code = ErrorCode.INTERNAL_ERROR
                try:
                    side_effect_state = SideEffectState(
                        str(error.get("side_effect_state") or "none")
                    )
                except ValueError:
                    side_effect_state = SideEffectState.UNKNOWN
                raise ProtocolError(
                    code,
                    str(error.get("message") or "semantic extension chooser failed")[:2_000],
                    retryable=bool(error.get("retryable", False)),
                    side_effect_state=side_effect_state,
                    missing_capability="os.file_choosers.choose_path",
                )
            await click_task
            for _ in range(50):
                for record in self._profile_query("extensions"):
                    installed_path = record.get("path")
                    if isinstance(installed_path, str) and installed_path == path:
                        return {
                            "execution_path": "accessibility",
                            "extension_id": record.get("id"),
                            "name": record.get("name"),
                            "path": path,
                            "enabled": record.get("enabled") is True,
                            "guarded_relaunch": False,
                        }
                await asyncio.sleep(0.2)
            raise ProtocolError(
                ErrorCode.POSTCONDITION_FAILED,
                "Chrome chooser closed but the unpacked extension was absent from the registry",
                side_effect_state=SideEffectState.UNKNOWN,
                missing_capability="chrome.extensions.load_unpacked",
            )
        except ProtocolError:
            raise
        except Exception as error:
            raise ProtocolError(
                ErrorCode.UNCERTAIN if chooser_opened else ErrorCode.ADAPTER_UNAVAILABLE,
                f"Chrome load-unpacked UI route failed: {type(error).__name__}",
                retryable=not chooser_opened,
                side_effect_state=(
                    SideEffectState.UNKNOWN if chooser_opened else SideEffectState.NONE
                ),
                missing_capability="chrome.extensions.load_unpacked",
            ) from error
        finally:
            if click_task is not None and not click_task.done():
                click_task.cancel()
                await asyncio.gather(click_task, return_exceptions=True)
            try:
                await page.close()
            except Exception:
                pass

    async def _load_unpacked_via_guarded_relaunch(self, path: str) -> dict[str, Any]:
        """Use the guest's native guarded Chrome relaunch bridge first."""

        context = await self.browser._context()
        restore_urls = [
            page.url for page in context.pages
            if not page.is_closed()
            and page.url.startswith(("http://", "https://", "chrome://", "file://"))
        ][:100]
        response = await asyncio.to_thread(self.guest_request, "POST", "/v1/act", {
            "action": "chrome_load_unpacked",
            "arguments": {"path": path, "restore_urls": restore_urls},
        })
        if not response.get("ok"):
            raw = response.get("error") if isinstance(response, Mapping) else None
            error = raw if isinstance(raw, Mapping) else {}
            try:
                code = ErrorCode(str(error.get("code") or "internal_error"))
            except ValueError:
                code = ErrorCode.INTERNAL_ERROR
            try:
                side_effect_state = SideEffectState(
                    str(error.get("side_effect_state") or "none")
                )
            except ValueError:
                side_effect_state = SideEffectState.UNKNOWN
            raise ProtocolError(
                code,
                str(error.get("message") or "guarded Chrome relaunch failed")[:2_000],
                retryable=bool(error.get("retryable", False)),
                side_effect_state=side_effect_state,
                missing_capability="chrome.extensions.load_unpacked",
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(
                ErrorCode.INTERNAL_ERROR,
                "guarded Chrome relaunch returned no typed result",
                side_effect_state=SideEffectState.UNKNOWN,
            )
        await self.browser._reconnect_after_browser_restart()
        live_extensions = await self._extensions()
        expected_id = str(result.get("extension_id") or "")
        expected_name = str(result.get("name") or "")
        live = next(
            (
                record for record in live_extensions
                if record.get("id") == expected_id
                and record.get("enabled") is True
                and (
                    not expected_name
                    or str(record.get("name") or "") == expected_name
                )
            ),
            None,
        )
        if live is None:
            raise ProtocolError(
                ErrorCode.POSTCONDITION_FAILED,
                "Chrome relaunched but the live extension registry did not verify the unpacked extension",
                side_effect_state=SideEffectState.UNKNOWN,
                missing_capability="chrome.extensions.load_unpacked",
            )
        self._loaded_extension_paths[expected_id] = path
        return {
            **dict(result),
            "extension_id": str(live["id"]),
            "name": str(live.get("name") or expected_name),
            "enabled": True,
            "path": path,
            "live_registry_verified": True,
        }

    async def _downloads(self) -> list[dict[str, Any]]:
        try:
            downloads = await self._internal(
                "chrome://downloads/",
                "() => new Promise((resolve, reject) => { if (!chrome.downloads) "
                "return reject(new Error('downloads API unavailable')); chrome.downloads.search({},resolve); })",
            )
            source = "chrome_internal_api"
        except ProtocolError as error:
            if error.code not in {ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED}:
                raise
            downloads = self._profile_query("downloads")
            source = "profile_database"
        records = []
        for download in downloads or []:
            if not isinstance(download, Mapping):
                continue
            download_id = int(download.get("id") or 0)
            ref = self._remember("download", download_id, {"id": download_id})
            records.append({
                "ref": ref, "kind": "chrome.download", "id": download_id,
                "url": _public_url(download.get("url")),
                "filename": download.get("filename"),
                "state": download.get("state"),
                "bytes_received": download.get("bytesReceived", download.get("bytes_received")),
                "total_bytes": download.get("totalBytes", download.get("total_bytes")),
                "error": download.get("error"),
                "execution_source": source,
                "advertised_actions": [],
            })
        return records

    async def _observe(self, resource: str) -> AdapterObservation:
        if resource == "chrome.bookmarks":
            records = await self._bookmarks()
        elif resource in {"chrome.settings", "chrome.privacy", "chrome.toolbar_state"}:
            records = await self._settings()
            if resource == "chrome.privacy":
                records = [record for record in records if any(
                    token in record.get("key", "").casefold()
                    for token in ("privacy", "tracking", "cookie", "history", "safe_browsing")
                )]
            elif resource == "chrome.toolbar_state":
                records = [record for record in records if any(
                    token in record.get("key", "").casefold()
                    for token in ("bookmark", "toolbar", "home_button")
                )]
        elif resource == "chrome.history":
            records = await self._history()
        elif resource == "chrome.extensions":
            records = await self._extensions()
        elif resource == "chrome.downloads":
            records = await self._downloads()
        elif resource == "chrome.profile":
            records = self._profile_query("profile")
            for record in records:
                ref = self._remember("profile", record.get("profile_hash"), {})
                record.update({
                    "ref": ref,
                    "advertised_actions": [],
                    "execution_source": "profile_database",
                })
        elif resource in {"chrome.print_jobs", "chrome.internal_pages"}:
            page = await self.browser._page()
            ref = self._remember("page", id(page), {"page": page})
            records = [{
                "ref": ref,
                "kind": resource,
                "url": _public_url(page.url),
                "profile": "active",
                "advertised_actions": ["save_pdf", "create_shortcut"],
            }]
        else:
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, resource)
        public = [{key: value for key, value in record.items() if key != "ref"} for record in records]
        return AdapterObservation(
            items=records,
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={"record_count": len(records)},
            native_revision=f"chrome_{_digest(public)[:20]}",
        )

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        return self.browser._call(self._observe(context.resource))

    async def _act(self, payload: Mapping[str, Any]) -> AdapterActionResult:
        target = payload.get("target") or {}
        native = self._native.get(str(target.get("ref")))
        if native is None:
            raise ProtocolError(ErrorCode.STALE_REF, "Chrome ref no longer resolves", retryable=True)
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        delta: dict[str, Any]
        if action == "create_bookmark":
            create_arguments = dict(arguments)
            if native.get("kind") == "bookmark" and native.get("id"):
                create_arguments.setdefault("parentId", native["id"])
            delta = await self._internal(
                "chrome://bookmarks/",
                "args => new Promise((resolve, reject) => chrome.bookmarks.create(args, value => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(value); }))",
                create_arguments,
                mutation=True,
                capability_expression="() => !!(chrome.bookmarks && chrome.bookmarks.create)",
                missing_capability="chrome.bookmarks.create",
            )
        elif action in {"update_bookmark", "move_bookmark"}:
            bookmark_id = native.get("id")
            method = "update" if action == "update_bookmark" else "move"
            delta = await self._internal(
                "chrome://bookmarks/",
                "args => new Promise((resolve, reject) => "
                f"chrome.bookmarks.{method}(args.id, args.changes, value => {{ "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(value); }))",
                {"id": bookmark_id, "changes": dict(arguments)},
                mutation=True,
                capability_expression=f"() => !!(chrome.bookmarks && chrome.bookmarks.{method})",
                missing_capability=f"chrome.bookmarks.{method}",
            )
        elif action == "delete_bookmark":
            await self._internal(
                "chrome://bookmarks/",
                "id => new Promise((resolve, reject) => chrome.bookmarks.removeTree(id, () => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }))",
                native.get("id"),
                mutation=True,
                capability_expression="() => !!(chrome.bookmarks && chrome.bookmarks.removeTree)",
                missing_capability="chrome.bookmarks.removeTree",
            )
            delta = {"deleted": native.get("id")}
        elif action == "set_pref":
            key = str(native.get("key") or arguments.get("key") or "")
            if not key:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "set_pref requires a preference key")
            value = arguments.get("value")
            await self._internal(
                "chrome://settings/",
                "args => new Promise((resolve, reject) => { if (!chrome.settingsPrivate) "
                "return reject(new Error('settingsPrivate unavailable')); "
                "chrome.settingsPrivate.setPref(args.key,args.value,'user_controlled',() => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }); })",
                {"key": key, "value": value},
                mutation=True,
                capability_expression=(
                    "() => !!(chrome.settingsPrivate && chrome.settingsPrivate.setPref)"
                ),
                missing_capability="chrome.settingsPrivate.setPref",
            )
            delta = {"key": key, "value": value}
        elif action == "delete_history":
            await self._internal(
                "chrome://history/",
                "url => new Promise((resolve, reject) => chrome.history.deleteUrl({url}, () => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }))",
                native.get("url"),
                mutation=True,
                capability_expression="() => !!(chrome.history && chrome.history.deleteUrl)",
                missing_capability="chrome.history.deleteUrl",
            )
            delta = {"deleted_url": native.get("url")}
        elif action == "clear_history":
            start = float(arguments.get("start_time", 0))
            end = float(arguments.get("end_time", 8.64e15))
            await self._internal(
                "chrome://history/",
                "range => new Promise((resolve, reject) => chrome.history.deleteRange(range, () => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }))",
                {"startTime": start, "endTime": end},
                mutation=True,
                capability_expression="() => !!(chrome.history && chrome.history.deleteRange)",
                missing_capability="chrome.history.deleteRange",
            )
            delta = {"cleared": True, "start_time": start, "end_time": end}
        elif action in {"enable_extension", "disable_extension"}:
            extension_id = native.get("id")
            enabled = action == "enable_extension"
            await self._internal(
                "chrome://extensions/",
                "args => new Promise((resolve, reject) => "
                "chrome.management.setEnabled(args.id,args.enabled,() => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }))",
                {"id": extension_id, "enabled": enabled},
                mutation=True,
                capability_expression=(
                    "() => !!(chrome.management && chrome.management.setEnabled)"
                ),
                missing_capability="chrome.management.setEnabled",
            )
            delta = {"id": extension_id, "enabled": enabled}
        elif action == "uninstall_extension":
            extension_id = native.get("id")
            await self._internal(
                "chrome://extensions/",
                "id => new Promise((resolve, reject) => "
                "chrome.management.uninstall(id,{showConfirmDialog:false},() => { "
                "const error = chrome.runtime && chrome.runtime.lastError; if (error) reject(new Error(error.message)); "
                "else resolve(true); }))",
                extension_id,
                mutation=True,
                capability_expression=(
                    "() => !!(chrome.management && chrome.management.uninstall)"
                ),
                missing_capability="chrome.management.uninstall",
            )
            self._loaded_extension_paths.pop(str(extension_id), None)
            delta = {"id": extension_id, "uninstalled": True}
        elif action == "load_unpacked":
            path = arguments.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "load_unpacked requires an absolute guest directory path",
                )
            try:
                delta = await self._load_unpacked_via_guarded_relaunch(path)
            except ProtocolError as error:
                # Accessibility is a fallback only when the native route
                # proved that no mutation began. Never replay after a relaunch
                # timeout or another uncertain/partially applied result.
                if (
                    error.code not in {
                        ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.UNSUPPORTED,
                    }
                    or error.side_effect_state is not SideEffectState.NONE
                ):
                    raise
                delta = await self._load_unpacked_via_internal_ui(path)
            if not delta.get("extension_id") or delta.get("enabled") is not True:
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    "Chrome did not verify the unpacked extension registry",
                    side_effect_state=SideEffectState.UNKNOWN,
                    missing_capability="chrome.extensions.load_unpacked",
                )
        elif action == "save_pdf":
            path = arguments.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "save_pdf requires an absolute guest path")
            page = native.get("page") or await self.browser._page()
            context = await self.browser._context()
            session = await context.new_cdp_session(page)
            try:
                printed = await session.send("Page.printToPDF", {
                    "printBackground": True, "preferCSSPageSize": True,
                    "transferMode": "ReturnAsBase64",
                })
            finally:
                await session.detach()
            encoded = printed.get("data")
            if not isinstance(encoded, str):
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Chrome returned no PDF data")
            # Decode privately, validate the signature, then use the guest's
            # bounded atomic blob staging route. A print result can exceed the
            # daemon's 1 MiB request cap; no image/PDF bytes enter model context.
            raw_pdf = base64.b64decode(encoded, validate=True)
            if not raw_pdf.startswith(b"%PDF-"):
                raise ProtocolError(ErrorCode.POSTCONDITION_FAILED, "Chrome output is not a PDF")
            transfer_id = hashlib.sha256(
                f"{path}:{hashlib.sha256(raw_pdf).hexdigest()}".encode()
            ).hexdigest()
            written: Mapping[str, Any] = {}
            offset = 0
            try:
                for start in range(0, len(raw_pdf), 512 * 1024):
                    chunk = raw_pdf[start:start + 512 * 1024]
                    final = start + len(chunk) == len(raw_pdf)
                    chunk_arguments: dict[str, Any] = {
                        "transfer_id": transfer_id,
                        "offset": offset,
                        "base64": base64.b64encode(chunk).decode("ascii"),
                        "final": final,
                    }
                    if start == 0:
                        chunk_arguments["path"] = path
                        if isinstance(arguments.get("expected_hash"), str):
                            chunk_arguments["expected_hash"] = arguments["expected_hash"]
                    written = self.guest_request("POST", "/v1/act", {
                        "action": "stage_base64_chunk", "arguments": chunk_arguments,
                    })
                    if not written.get("ok"):
                        raw_error = written.get("error") if isinstance(written, Mapping) else None
                        raw_error = raw_error if isinstance(raw_error, Mapping) else {}
                        try:
                            error_code = ErrorCode(str(raw_error.get("code") or "artifact_conflict"))
                        except ValueError:
                            error_code = ErrorCode.INTERNAL_ERROR
                        try:
                            side_effect_state = SideEffectState(
                                str(raw_error.get("side_effect_state") or "none")
                            )
                        except ValueError:
                            side_effect_state = SideEffectState.UNKNOWN
                        raise ProtocolError(
                            error_code,
                            str(raw_error.get("message") or "guest rejected a staged printed-PDF chunk")[:2_000],
                            retryable=bool(raw_error.get("retryable", False)),
                            side_effect_state=side_effect_state,
                        )
                    offset += len(chunk)
            except Exception:
                self.guest_request("POST", "/v1/act", {
                    "action": "abort_blob_transfer",
                    "arguments": {"transfer_id": transfer_id},
                })
                raise
            delta = dict(written.get("result") or {})
        elif action == "create_shortcut":
            name = arguments.get("name")
            native_page = native.get("page")
            url = arguments.get("url") or (native_page.url if native_page else None)
            created = self.guest_request("POST", "/v1/act", {
                "action": "create_desktop_entry",
                "arguments": {
                    "name": name, "url": url, "profile": arguments.get("profile", ""),
                    **({"expected_hash": arguments["expected_hash"]}
                       if isinstance(arguments.get("expected_hash"), str) else {}),
                },
            })
            if not created.get("ok"):
                raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "guest rejected desktop entry")
            delta = dict(created.get("result") or {})
        else:
            raise ProtocolError(ErrorCode.UNSUPPORTED, f"unsupported Chrome action: {action}")
        return AdapterActionResult(
            changed=True,
            result={"execution_path": "native_api", **(delta or {})},
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            status=Status.OK,
        )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        try:
            return self.browser._call(self._act(payload))
        except ProtocolError as error:
            if isinstance(error, _ChromePreMutationError):
                raise
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
                    missing_capability=error.missing_capability,
                ) from error
            raise
        except Exception as error:
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                f"Chrome action transport failed: {type(error).__name__}",
                retryable=False,
                side_effect_state=SideEffectState.UNKNOWN,
            ) from error

    def close(self) -> None:
        # The paired browser adapter owns the shared event loop and connection.
        return
