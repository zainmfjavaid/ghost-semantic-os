#!/usr/bin/env python3
"""Model-free semantic-v1 canaries against one real nested OSWorld desktop."""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import requests


class Canary:
    def __init__(
        self, base_url: str, task: Path, suite: str, *, source_url: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.task = task.resolve()
        self.suite = suite
        self.source_url = source_url
        self.episode_id: str | None = None
        self.observations: dict[str, dict[str, Any]] = {}

    def create(self) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/episodes",
            json={
                "task_path": str(self.task),
                "runtime": "semantic-v1",
                "require_screenshot": False,
                "initial_observation": False,
                "max_tool_calls": 100,
            },
            timeout=900,
        )
        response.raise_for_status()
        created = response.json()
        self.episode_id = str(created["episode_id"])
        identity = created.get("environment_identity") or {}
        if identity.get("outer_provider") != "gcp":
            raise RuntimeError(f"wrong outer environment identity: {identity}")
        if identity.get("guest_platform") != "linux":
            raise RuntimeError(f"wrong nested guest platform: {identity}")
        if not created.get("semantic_guest_bundle_hash"):
            raise RuntimeError("versioned semantic guest handshake is missing")
        if created.get("screenshots_captured") != 0:
            raise RuntimeError(f"reset captured a screenshot: {created}")
        return created

    def semantic(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.episode_id
        request = {
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": self.episode_id,
            "operation": operation,
            "payload": payload,
        }
        response = requests.post(
            f"{self.base_url}/episodes/{self.episode_id}/semantic",
            json=request,
            timeout=330,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") not in {"ok", "partial"}:
            raise RuntimeError(
                f"semantic {operation} failed: {json.dumps(result, sort_keys=True)}"
            )
        return result

    def query(
        self, resource: str, *, parameters: dict[str, Any] | None = None,
        where: dict[str, Any] | None = None, scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.semantic("query", {
            "resource": resource,
            "scope": scope or {},
            "where": where or {},
            "fields": [],
            "order_by": [],
            "parameters": parameters or {},
            "limit": 100,
            "freshness": "live",
        })
        self.observations[resource] = response
        return response

    @staticmethod
    def records(response: dict[str, Any]) -> list[dict[str, Any]]:
        records = (response.get("result") or {}).get("records")
        if not isinstance(records, list):
            raise RuntimeError(f"response has no record list: {response}")
        return records

    def act(
        self, ref: str, action: str, arguments: dict[str, Any], *, confirm: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": {"ref": ref},
            "action": action,
            "arguments": arguments,
            "preconditions": [],
            "postconditions": [],
            "timeout_ms": 30_000,
            "confirm": confirm,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return self.semantic("act", payload)

    def core(self) -> dict[str, Any]:
        capabilities = self.records(self.query("system.capabilities"))
        if len(capabilities) < 8:
            raise RuntimeError(f"too few semantic capabilities: {len(capabilities)}")
        seen_adapter = False
        for capability in capabilities:
            if capability.get("capability_type") == "adapter":
                seen_adapter = True
            elif capability.get("capability_type") == "resource":
                if seen_adapter:
                    raise RuntimeError("resource capability appeared after an adapter card")
            else:
                raise RuntimeError(f"unknown capability record: {capability}")
        discovery_bytes: dict[str, dict[str, int]] = {}
        for resource in (
            "browser.elements", "filesystem.file", "research.documents",
            "spreadsheet.cells",
        ):
            summary = self.query(
                "system.capabilities",
                where={"op": "eq", "field": "resource", "value": resource},
            )
            summary_records = self.records(summary)
            if len(summary_records) != 1:
                raise RuntimeError(
                    f"resource capability is not uniquely discoverable ({resource}): "
                    f"{summary_records}"
                )
            detail = self.query(
                "system.capability", scope={"ref": str(summary_records[0]["ref"])}
            )
            detail_records = self.records(detail)
            if len(detail_records) != 1 or detail_records[0].get("resource") != resource:
                raise RuntimeError(f"wrong resource capability detail ({resource}): {detail}")
            if (detail.get("result") or {}).get("overflow_handle") is not None:
                raise RuntimeError(f"resource capability overflowed to a handle: {resource}")
            discovery_bytes[resource] = {
                "summary": len(json.dumps(summary, separators=(",", ":"))),
                "detail": len(json.dumps(detail, separators=(",", ":"))),
            }
            if discovery_bytes[resource]["summary"] > 4_000:
                raise RuntimeError(f"resource capability summary is oversized: {resource}")
            if discovery_bytes[resource]["detail"] > 8_000:
                raise RuntimeError(f"resource capability detail is oversized: {resource}")
        health = self.records(self.query("system.health"))
        unavailable = [
            item for item in health
            if item.get("status") == "unavailable"
            and item.get("adapter_id") in {
                "universal-atspi@1", "guest-os@1", "guest-filesystem@1",
            }
        ]
        if unavailable:
            raise RuntimeError(f"required core adapter unavailable: {unavailable}")
        self.query("system.surfaces")
        self.query("system.pending_state")
        keyword_run = self.semantic("run", {
            "code": (
                "rows = computer.query(resource='system.pending_state', limit=30)\n"
                "emit(len(rows['records']))"
            ),
        })
        if (keyword_run.get("result") or {}).get("failed_operation") is not None:
            raise RuntimeError(f"keyword-style computer.run failed: {keyword_run}")
        assert self.episode_id
        invalid_completion = requests.post(
            f"{self.base_url}/episodes/{self.episode_id}/semantic/complete",
            json={
                "summary": "negative completion-contract canary",
                "infeasible": False,
                "claims": [{
                    "claim": "missing receipt must fail as typed data",
                    "verification_id": "ver_intentionally_missing",
                }],
                "evidence_ids": [],
            },
            timeout=30,
        )
        invalid_completion.raise_for_status()
        completion_result = invalid_completion.json()
        if (
            completion_result.get("accepted") is not False
            or (completion_result.get("error") or {}).get("code")
            != "precondition_failed"
        ):
            raise RuntimeError(
                "missing completion receipt did not return typed precondition_failed: "
                f"{completion_result}"
            )
        return {
            "capability_records": len(capabilities),
            "capability_adapters": sum(
                item.get("capability_type") == "adapter" for item in capabilities
            ),
            "capability_discovery_bytes": discovery_bytes,
            "health_records": len(health),
            "invalid_completion_typed": True,
        }

    def browser(self) -> dict[str, Any]:
        tabs = self.records(self.query("browser.tabs"))
        if not tabs:
            raise RuntimeError("browser canary has no semantic tab")
        active = next((item for item in tabs if item.get("active")), tabs[-1])
        acted = self.act(
            str(active["ref"]), "navigate", {"url": "https://example.com/"},
            confirm=False,
        )
        if (acted.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"browser navigation was not applied: {acted}")
        text_records = self.records(self.query("browser.text"))
        if not any("Example Domain" in str(item.get("text")) for item in text_records):
            raise RuntimeError(f"browser text missing navigated state: {text_records}")
        for resource in (
            "chrome.profile", "chrome.settings", "chrome.bookmarks",
            "chrome.history", "chrome.extensions", "chrome.downloads",
        ):
            try:
                self.query(resource)
            except Exception as error:
                raise RuntimeError(f"Chrome resource failed ({resource}): {error}") from error
        verified = self.semantic("verify", {
            "mode": "all",
            "assertions": [{
                "claim_id": "example-domain-visible",
                "query": {
                    "resource": "browser.text", "scope": {}, "where": {},
                    "fields": [], "order_by": [], "parameters": {},
                    "limit": 30, "freshness": "live",
                },
                "assert": {"op": "contains", "field": "text", "value": "Example Domain"},
            }],
            "freshness": "live",
        })
        if (verified.get("result") or {}).get("verdict") != "pass":
            raise RuntimeError(f"browser verification failed: {verified}")
        return {"tabs": len(tabs), "chrome_resources": 6}

    def chrome_actions(self) -> dict[str, Any]:
        self.browser()
        bookmarks = self.records(self.query("chrome.bookmarks"))
        folder = next(
            (
                item for item in bookmarks
                if item.get("folder")
                and str(item.get("title") or "").casefold() == "bookmarks bar"
            ),
            next((item for item in bookmarks if item.get("folder") and item.get("parent_ref")), None),
        )
        if folder is None:
            raise RuntimeError("Chrome exposes no bookmark folder target")
        bookmark_title = "Ghost Semantic Canary"
        bookmark_url = "https://example.com/?ghost-semantic-canary=1"
        created = self.act(
            str(folder["ref"]), "create_bookmark",
            {"title": bookmark_title, "url": bookmark_url}, confirm=True,
        )
        if (created.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"bookmark action was not applied: {created}")
        bookmark_matches = self.records(self.query(
            "chrome.bookmarks",
            where={"op": "eq", "field": "url", "value": bookmark_url},
        ))
        if len(bookmark_matches) != 1 or bookmark_matches[0].get("title") != bookmark_title:
            raise RuntimeError(f"bookmark persistent state did not match: {bookmark_matches}")

        settings = self.records(self.query(
            "chrome.settings",
            where={"op": "eq", "field": "key", "value": "browser.show_bookmark_bar"},
        ))
        if len(settings) != 1 or not isinstance(settings[0].get("value"), bool):
            toolbar = self.records(self.query("chrome.toolbar_state"))
            settings = [item for item in toolbar if isinstance(item.get("value"), bool)]
        if not settings:
            raise RuntimeError(f"bookmark-bar setting is not semantically available: {settings}")
        setting = settings[0]
        setting_key = str(setting["key"])
        expected_setting = not bool(setting["value"])
        changed = self.act(
            str(setting["ref"]), "set_pref", {"value": expected_setting},
            confirm=True,
        )
        if (changed.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"setting action was not applied: {changed}")
        current = self.records(self.query(
            "chrome.settings",
            where={"op": "all", "filters": [
                {"field": "key", "op": "eq", "value": setting_key},
                {"field": "value", "op": "eq", "value": expected_setting},
            ]},
        ))
        if len(current) != 1:
            raise RuntimeError(f"setting state did not persist: {current}")

        jobs = self.records(self.query("chrome.print_jobs"))
        if len(jobs) != 1:
            raise RuntimeError(f"Chrome print surface is ambiguous: {jobs}")
        pdf_path = "/home/user/Desktop/semantic-canary.pdf"
        printed = self.act(str(jobs[0]["ref"]), "save_pdf", {"path": pdf_path}, confirm=True)
        delta = (printed.get("result") or {}).get("delta") or {}
        if (printed.get("result") or {}).get("status") != "applied" or int(delta.get("size", 0)) < 5:
            raise RuntimeError(f"PDF action did not produce a validated artifact: {printed}")
        structure = self.records(self.query("artifact.structure", scope={"path": pdf_path}))
        if not structure:
            raise RuntimeError("printed PDF has no parseable artifact structure")
        return {
            "bookmark_mutation": True,
            "setting_mutation": True,
            "pdf_bytes": int(delta["size"]),
            "pdf_parseable": True,
        }

    def extension(self) -> dict[str, Any]:
        extension_path = "/home/user/Desktop/helloExtension"
        try:
            self.query("filesystem.metadata", scope={"path": extension_path})
        except RuntimeError:
            archive_path = "/home/user/Desktop/helloExtension.zip"
            archive = self.records(self.query(
                "artifact.structure", scope={"path": archive_path},
            ))
            if len(archive) != 1:
                raise RuntimeError(f"extension setup exposed no source archive: {archive}")
            extracted = self.act(
                str(archive[0]["ref"]), "extract_archive",
                {
                    "source": archive_path,
                    "destination": "/home/user/Desktop",
                    "expected_hash": archive[0].get("sha256"),
                },
                confirm=True,
            )
            if (extracted.get("result") or {}).get("status") != "applied":
                raise RuntimeError(f"extension archive was not extracted: {extracted}")
        profiles = self.records(self.query("chrome.profile"))
        if len(profiles) != 1:
            raise RuntimeError(f"active Chrome profile is ambiguous: {profiles}")
        loaded = self.act(
            str(profiles[0]["ref"]), "load_unpacked", {"path": extension_path},
            confirm=True,
        )
        delta = (loaded.get("result") or {}).get("delta") or {}
        if (loaded.get("result") or {}).get("status") != "applied" or not delta.get("extension_id"):
            raise RuntimeError(f"unpacked extension was not verified: {loaded}")
        records = self.records(self.query(
            "chrome.extensions",
            where={"op": "all", "filters": [
                {"field": "path", "op": "eq", "value": extension_path},
                {"field": "enabled", "op": "is_true"},
            ]},
        ))
        if len(records) != 1:
            raise RuntimeError(f"extension registry did not persist loaded path: {records}")
        return {"extension_id": delta["extension_id"], "extension_enabled": True}

    def dialog(self) -> dict[str, Any]:
        desktop = self.records(self.query(
            "filesystem.metadata", scope={"path": "/home/user/Desktop"},
        ))
        if not desktop:
            raise RuntimeError("Desktop has no filesystem capability target")
        html_path = "/home/user/Desktop/semantic-file-chooser.html"
        written = self.act(
            str(desktop[0]["ref"]), "write_text",
            {
                "path": html_path,
                "content": "<!doctype html><title>Semantic chooser</title><input type=file aria-label='Choose semantic file'>",
            },
            confirm=True,
        )
        if (written.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"dialog fixture was not written: {written}")
        tabs = self.records(self.query("browser.tabs"))
        active = next((item for item in tabs if item.get("active")), tabs[-1])
        self.act(str(active["ref"]), "navigate", {"url": f"file://{html_path}"}, confirm=False)
        controls = self.records(self.query(
            "browser.elements",
            where={"op": "contains", "field": "name", "value": "semantic file"},
        ))
        if len(controls) != 1:
            raise RuntimeError(f"file input was not uniquely represented: {controls}")
        self.act(str(controls[0]["ref"]), "invoke", {}, confirm=False)
        choosers: list[dict[str, Any]] = []
        for _ in range(20):
            choosers = self.records(self.query("os.file_choosers"))
            if choosers:
                break
            import time
            time.sleep(0.25)
        if len(choosers) != 1:
            dialogs = self.records(self.query("os.dialogs"))
            windows = self.records(self.query("os.windows"))
            applications = self.records(self.query("os.applications"))
            raise RuntimeError(
                f"native file chooser was not represented: choosers={choosers} "
                f"dialogs={dialogs} windows={windows} applications={applications}"
            )
        dismissed = self.act(str(choosers[0]["ref"]), "dismiss", {}, confirm=False)
        if (dismissed.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"native file chooser was not dismissed: {dismissed}")
        if self.records(self.query("os.file_choosers")):
            raise RuntimeError("native file chooser remained after dismiss")
        return {"file_chooser_visible": True, "file_chooser_dismissed": True}

    def packages(self) -> dict[str, Any]:
        package: dict[str, Any] | None = None
        for candidate in ("sl", "ed", "tree"):
            records = self.records(self.query(
                "os.packages", parameters={"name": candidate},
            ))
            if len(records) == 1 and records[0].get("installed") is False:
                package = records[0]
                break
        if package is None:
            raise RuntimeError("package canary found no known uninstalled bounded package")
        installed = self.act(
            str(package["ref"]), "install_package",
            {"name": str(package["name"])}, confirm=True,
        )
        install_result = installed.get("result") or {}
        install_delta = install_result.get("delta") or {}
        if (
            install_result.get("status") != "applied"
            or install_delta.get("installed") is not True
            or not install_delta.get("version")
        ):
            raise RuntimeError(f"OS package installation was not proven: {installed}")
        package_after = self.records(self.query(
            "os.packages", parameters={"name": str(package["name"])},
        ))
        if (
            len(package_after) != 1
            or package_after[0].get("installed") is not True
            or package_after[0].get("version") != install_delta.get("version")
        ):
            raise RuntimeError(f"package registry postcondition failed: {package_after}")

        identifier = "org.ghost.semantic.canary"
        description = f"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006"
 xmlns:d="http://openoffice.org/extensions/description/2006"
 xmlns:xlink="http://www.w3.org/1999/xlink">
  <identifier value="{identifier}"/>
  <version value="1.0.0"/>
  <display-name><name lang="en">Ghost Semantic Canary</name></display-name>
</description>
""".encode()
        manifest = b"""<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">
  <manifest:file-entry manifest:full-path="description.xml" manifest:media-type="application/vnd.sun.star.package-bundle-description"/>
</manifest:manifest>
"""
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("description.xml", description)
            archive.writestr("META-INF/manifest.xml", manifest)
        desktop = self.records(self.query(
            "filesystem.metadata", scope={"path": "/home/user/Desktop"},
        ))
        if len(desktop) != 1:
            raise RuntimeError(f"Desktop filesystem target is ambiguous: {desktop}")
        oxt_path = "/home/user/Desktop/semantic-canary.oxt"
        written = self.act(
            str(desktop[0]["ref"]), "write_base64_atomic",
            {"path": oxt_path, "base64": base64.b64encode(archive_bytes.getvalue()).decode()},
            confirm=True,
        )
        if (written.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"OXT fixture write was not applied: {written}")
        extensions = self.records(self.query(
            "libreoffice.extensions", parameters={"identifier": identifier},
        ))
        if len(extensions) != 1 or extensions[0].get("installed") is not False:
            raise RuntimeError(f"extension precondition was not represented: {extensions}")
        added = self.act(
            str(extensions[0]["ref"]), "install_extension", {"path": oxt_path},
            confirm=True,
        )
        extension_result = added.get("result") or {}
        extension_delta = extension_result.get("delta") or {}
        if (
            extension_result.get("status") != "applied"
            or extension_delta.get("identifier") != identifier
            or extension_delta.get("installed") is not True
            or extension_delta.get("enabled") is not None
            or extension_delta.get("registration_state") != "not_applicable"
        ):
            raise RuntimeError(f"LibreOffice extension installation was not proven: {added}")
        extension_after = self.records(self.query(
            "libreoffice.extensions", parameters={"identifier": identifier},
        ))
        if (
            len(extension_after) != 1
            or extension_after[0].get("installed") is not True
            or extension_after[0].get("enabled") is not None
            or extension_after[0].get("registration_state") != "not_applicable"
        ):
            raise RuntimeError(f"extension registry postcondition failed: {extension_after}")
        return {
            "package": package["name"], "package_registry_verified": True,
            "extension_identifier": identifier,
            "extension_registry_verified": True,
            "extension_registration_state": extension_delta.get("registration_state"),
            "libreoffice_restart_required": bool(
                extension_delta.get("libreoffice_restart_required")
            ),
        }

    def extension_source(self) -> dict[str, Any]:
        """Exercise the bounded public-URL-to-guest OXT transport end to end."""
        extensions = self.records(self.query("libreoffice.extensions"))
        registries = [
            record for record in extensions
            if "install_extension" in record.get("advertised_actions", [])
        ]
        if len(registries) != 1:
            raise RuntimeError(f"extension registry target is ambiguous: {registries}")
        source_url = self.source_url
        if not source_url:
            raise RuntimeError("extension-source requires --source-url")
        added = self.act(
            str(registries[0]["ref"]), "install_extension",
            {"source_url": source_url}, confirm=True,
            idempotency_key="semantic-canary-public-oxt",
        )
        result = added.get("result") or {}
        delta = result.get("delta") or {}
        source = delta.get("source_artifact") or {}
        if (
            result.get("status") != "applied"
            or delta.get("installed") is not True
            or not delta.get("identifier")
            or source.get("requested_url") != source_url
            or source.get("staged_artifact_removed") is not True
            or not source.get("content_hash")
            or int(source.get("size") or 0) <= 384 * 1024
        ):
            raise RuntimeError(f"public OXT installation was not proven: {added}")
        return {
            "extension_identifier": delta["identifier"],
            "source_size": source["size"],
            "source_hash": source["content_hash"],
            "staged_artifact_removed": True,
        }

    def cross_app(self) -> dict[str, Any]:
        def unique_ui(
            *, role: str, name: str, contains: bool = False, polls: int = 30,
        ) -> dict[str, Any]:
            candidates: list[dict[str, Any]] = []
            name_filter = {
                "field": "name", "op": "contains" if contains else "eq", "value": name,
            }
            where = {"op": "all", "filters": [
                {"field": "role", "op": "eq", "value": role}, name_filter,
            ]}
            for _ in range(polls):
                candidates = self.records(self.query("ui.elements", where=where))
                if len(candidates) == 1:
                    return candidates[0]
                time.sleep(0.25)
            raise RuntimeError(
                f"UI target did not resolve uniquely: role={role!r} name={name!r} "
                f"candidates={candidates}"
            )

        bills = unique_ui(role="tree item", name="Bills")
        bills_actions = bills.get("advertised_actions") or []
        if "activate" not in bills_actions:
            raise RuntimeError(f"Bills folder has no direct activate action: {bills}")
        self.act(
            str(bills["ref"]), "invoke", {"advertised_action": "activate"},
            confirm=False,
        )
        invoice = unique_ui(
            role="tree item", name="Amazon Web Services Invoice Available", contains=True,
        )
        invoice_actions = invoice.get("advertised_actions") or []
        if "activate" not in invoice_actions:
            raise RuntimeError(f"AWS invoice has no direct activate action: {invoice}")
        self.act(
            str(invoice["ref"]), "invoke", {"advertised_action": "activate"},
            confirm=False,
        )
        link = unique_ui(role="link", name="Billing & Cost Management Page")
        link_actions = link.get("advertised_actions") or []
        if "jump" not in link_actions:
            raise RuntimeError(f"mail link has no direct jump action: {link}")
        tabs_before = self.records(self.query("browser.tabs"))
        before_surfaces = {str(item["surface_id"]) for item in tabs_before}
        idempotency_key = f"cross-app-link-{uuid.uuid4()}"
        applied = self.act(
            str(link["ref"]), "invoke", {"advertised_action": "jump"},
            confirm=False, idempotency_key=idempotency_key,
        )
        result = applied.get("result") or {}
        effect = (result.get("delta") or {}).get("browser_target_effect") or {}
        if (
            result.get("status") != "applied"
            or effect.get("creation_verdict") != "one_created"
            or not effect.get("created_surface_ref")
            or "amazon.com" not in str(effect.get("target_uri") or "")
        ):
            raise RuntimeError(f"cross-app receipt did not prove one linked target: {applied}")
        tabs_after: list[dict[str, Any]] = []
        for _ in range(30):
            tabs_after = self.records(self.query("browser.tabs"))
            if len(tabs_after) == len(tabs_before) + 1:
                break
            time.sleep(0.25)
        new_tabs = [
            item for item in tabs_after
            if str(item["surface_id"]) not in before_surfaces
        ]
        if len(new_tabs) != 1 or "amazon.com" not in str(new_tabs[0].get("url") or ""):
            raise RuntimeError(
                f"cross-app browser state did not match the receipt: before={tabs_before} "
                f"after={tabs_after}"
            )
        replayed = self.act(
            str(link["ref"]), "invoke", {"advertised_action": "jump"},
            confirm=False, idempotency_key=idempotency_key,
        )
        replay_result = replayed.get("result") or {}
        if replay_result.get("receipt_id") != result.get("receipt_id"):
            raise RuntimeError(f"idempotent replay did not return the original receipt: {replayed}")
        tabs_replayed = self.records(self.query("browser.tabs"))
        if len(tabs_replayed) != len(tabs_after):
            raise RuntimeError("idempotent cross-app replay created a duplicate browser target")
        return {
            "tabs_before": len(tabs_before), "tabs_after": len(tabs_after),
            "browser_target_created": True, "target_uri_grounded": True,
            "receipt_replayed_without_duplicate": True,
        }

    def office(self) -> dict[str, Any]:
        sessions = self.records(self.query("document.sessions"))
        if not sessions:
            raise RuntimeError("LibreOffice canary has no live UNO document")
        state = self.records(self.query("document.state"))
        writer = self.records(self.query("writer.paragraphs"))
        if not writer:
            raise RuntimeError("Writer canary exposed no paragraphs")
        first = writer[0]
        original = str(first.get("text") or "")
        marker = "SEMANTIC_CANARY "
        replacement = [marker + original, "", "SEMANTIC_CANARY SECOND PARAGRAPH"]
        acted = self.act(
            str(first["ref"]), "replace_with_paragraphs", {"paragraphs": replacement},
            confirm=False,
        )
        if (acted.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"UNO structural mutation was not applied: {acted}")
        paragraph_evidence = ((acted.get("result") or {}).get("delta") or {}).get(
            "paragraph_evidence"
        )
        if not isinstance(paragraph_evidence, dict) or not paragraph_evidence.get("matched"):
            raise RuntimeError(f"UNO structural receipt lacked paragraph evidence: {acted}")
        observed = [str(item.get("text") or "") for item in self.records(
            self.query("writer.paragraphs")
        )]
        if not any(
            observed[index:index + len(replacement)] == replacement
            for index in range(max(0, len(observed) - len(replacement) + 1))
        ):
            raise RuntimeError(f"Writer did not expose real paragraph objects: {observed[:5]}")
        verified = self.semantic("verify", {
            "mode": "all",
            "assertions": [{
                "claim_id": "writer-live-edit",
                "query": {
                    "resource": "writer.paragraphs", "scope": {},
                    "where": {"op": "contains", "field": "text", "value": marker},
                    "fields": [], "order_by": [], "parameters": {},
                    "limit": 100, "freshness": "live",
                },
                "assert": {"op": "exists"},
            }],
            "freshness": "live",
        })
        if (verified.get("result") or {}).get("verdict") != "pass":
            raise RuntimeError(f"UNO verification failed: {verified}")
        url = str(state[0].get("url") or "") if state else ""
        if url.startswith("file://"):
            path = url.removeprefix("file://")
            saved = self.act(str(state[0]["ref"]), "save", {}, confirm=True)
            if (saved.get("result") or {}).get("status") != "applied":
                raise RuntimeError(f"Writer save did not apply: {saved}")
            file_records = self.records(self.query(
                "filesystem.file", scope={"path": path}
            ))
            sha256 = file_records[0].get("sha256") if file_records else None
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise RuntimeError(f"filesystem.file did not expose expected_hash input: {file_records}")
            structure = self.records(self.query(
                "artifact.structure", scope={"path": path}
            ))
            if len(structure) != 1 or structure[0].get("parseable") is not True:
                raise RuntimeError(f"saved Writer artifact was not parseable: {structure}")
            synchronization = self.records(self.query(
                "artifact.sync", scope={"path": path}
            ))
            if (
                len(synchronization) != 1
                or synchronization[0].get("live_app_matches_disk") is not True
            ):
                raise RuntimeError(
                    f"Writer live model did not match saved artifact: {synchronization}"
                )
        return {
            "documents": len(sessions), "state_records": len(state),
            "paragraphs": len(writer), "structural_paragraphs": len(replacement),
            "saved_parseable": bool(url.startswith("file://")),
            "live_disk_match": bool(url.startswith("file://")),
        }

    def calc_artifact(self) -> dict[str, Any]:
        sessions = self.records(self.query("document.sessions"))
        if not sessions:
            raise RuntimeError("Calc canary has no live UNO document")
        sheets = self.records(self.query("spreadsheet.sheets"))
        if not sheets:
            raise RuntimeError("Calc canary exposed no sheets")
        sheet_name = str(sheets[0].get("name") or "")
        if not sheet_name:
            raise RuntimeError(f"Calc sheet has no semantic name: {sheets[0]}")
        range_name = "Z100:AA101"
        ranges = self.records(self.query(
            "spreadsheet.ranges",
            parameters={"sheet": sheet_name, "range": range_name},
        ))
        if len(ranges) != 1:
            raise RuntimeError(f"Calc range is not uniquely represented: {ranges}")
        expected = [
            ["SEMANTIC_CANARY_A", "SEMANTIC_CANARY_B"],
            ["one", "two"],
        ]
        acted = self.act(
            str(ranges[0]["ref"]), "set_range_values", {"values": expected},
            confirm=False,
        )
        if (acted.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"Calc range mutation was not applied: {acted}")
        evidence = ((acted.get("result") or {}).get("delta") or {}).get("range_evidence")
        if not isinstance(evidence, dict) or evidence.get("matched") is not True:
            raise RuntimeError(f"Calc range receipt lacked exact evidence: {acted}")
        observed = self.records(self.query(
            "spreadsheet.ranges",
            parameters={"sheet": sheet_name, "range": range_name},
        ))
        if len(observed) != 1 or observed[0].get("data") != expected:
            raise RuntimeError(f"Calc did not retain exact range data: {observed}")
        state = self.records(self.query("document.state"))
        if len(state) != 1:
            raise RuntimeError(f"Calc state is ambiguous: {state}")
        saved = self.act(str(state[0]["ref"]), "save", {}, confirm=True)
        if (saved.get("result") or {}).get("status") != "applied":
            raise RuntimeError(f"Calc save did not apply: {saved}")
        url = str(state[0].get("url") or "")
        if not url.startswith("file://"):
            raise RuntimeError(f"Calc document has no persistent file URL: {state[0]}")
        path = url.removeprefix("file://")
        structure = self.records(self.query("artifact.structure", scope={"path": path}))
        if len(structure) != 1 or structure[0].get("parseable") is not True:
            raise RuntimeError(f"saved Calc artifact was not parseable: {structure}")
        synchronization = self.records(self.query("artifact.sync", scope={"path": path}))
        if (
            len(synchronization) != 1
            or synchronization[0].get("live_app_matches_disk") is not True
        ):
            raise RuntimeError(f"Calc live model did not match disk: {synchronization}")
        return {
            "sheet": sheet_name, "range": range_name,
            "range_postcheck": True, "saved_parseable": True,
            "live_disk_match": True,
        }

    def research(self) -> dict[str, Any]:
        searched = self.query(
            "research.search",
            parameters={"queries": ["Example Domain IANA reserved domain"]},
        )
        results = self.records(searched)
        if not results:
            raise RuntimeError("public research search returned no semantic results")
        search_handle = results[0].get("collection_handle")
        if not isinstance(search_handle, str):
            raise RuntimeError(f"research search returned no collection handle: {searched}")
        if any(result.get("collection_handle") != search_handle for result in results):
            raise RuntimeError("research search records did not share one collection handle")
        fetched = self.query(
            "research.documents",
            parameters={
                "collection_handle": search_handle,
                "result_offset": 0,
                "result_limit": min(3, len(results)),
            },
        )
        fetched_sources = self.records(fetched)
        documents_handle = (
            fetched_sources[0].get("collection_handle") if fetched_sources else None
        )
        if not isinstance(documents_handle, str):
            raise RuntimeError(f"research fetch returned no combined handle: {fetched}")
        if any(
            source.get("collection_handle") != documents_handle
            for source in fetched_sources
        ):
            raise RuntimeError("fetched sources did not share one collection handle")
        sources = self.records(self.query(
            "research.sources", parameters={"collection_handle": documents_handle},
        ))
        chunks = self.records(self.query(
            "research.documents", parameters={"collection_handle": documents_handle},
        ))
        if not sources or not chunks:
            raise RuntimeError(
                f"combined research handle was not re-queryable: sources={sources} chunks={chunks}"
            )
        required = {
            "url", "title", "http_status", "content_hash", "fetched_at",
            "redirect_chain", "source_excerpt",
        }
        missing = [
            sorted(required - set(source)) for source in sources
            if not required.issubset(source)
        ]
        if missing:
            raise RuntimeError(f"research sources lost provenance fields: {missing}")
        if any(chunk.get("collection_handle") != documents_handle for chunk in chunks):
            raise RuntimeError("research chunks did not retain their combined collection handle")
        return {
            "search_results": len(results),
            "fetched_sources": len(sources),
            "document_chunks": len(chunks),
            "combined_handle": True,
            "provenance_complete": True,
        }

    def strict_state(self) -> dict[str, Any]:
        assert self.episode_id
        response = requests.get(
            f"{self.base_url}/episodes/{self.episode_id}/semantic/state", timeout=30,
        )
        response.raise_for_status()
        state = response.json()
        counters = {
            key: state.get(key)
            for key in (
                "screenshots_captured", "image_parts_created", "image_parts_in_session",
                "image_parts_sent", "pixels_sent_to_policy_model", "visual_sidecar_calls",
            )
        }
        if any(value != 0 for value in counters.values()):
            raise RuntimeError(f"strict image policy counter failed: {counters}")
        assert self.episode_id
        legacy_obs = requests.get(
            f"{self.base_url}/episodes/{self.episode_id}/obs", timeout=30,
        )
        if legacy_obs.status_code != 409:
            raise RuntimeError(f"legacy obs did not fail closed: {legacy_obs.status_code}")
        legacy_step = requests.post(
            f"{self.base_url}/episodes/{self.episode_id}/step",
            json={"command": "import pyautogui"}, timeout=30,
        )
        if legacy_step.status_code != 409:
            raise RuntimeError(f"legacy step did not fail closed: {legacy_step.status_code}")
        return state

    def close(self) -> None:
        if self.episode_id is None:
            return
        response = requests.delete(
            f"{self.base_url}/episodes/{self.episode_id}", timeout=180,
        )
        response.raise_for_status()

    def run(self) -> dict[str, Any]:
        created = self.create()
        try:
            detail = self.core()
            if self.suite == "browser":
                detail.update(self.browser())
            elif self.suite == "chrome-actions":
                detail.update(self.chrome_actions())
            elif self.suite == "extension":
                detail.update(self.extension())
            elif self.suite == "dialog":
                detail.update(self.dialog())
            elif self.suite == "office":
                detail.update(self.office())
            elif self.suite == "calc-artifact":
                detail.update(self.calc_artifact())
            elif self.suite == "research":
                detail.update(self.research())
            elif self.suite == "packages":
                detail.update(self.packages())
            elif self.suite == "extension-source":
                detail.update(self.extension_source())
            elif self.suite == "cross-app":
                detail.update(self.cross_app())
            state = self.strict_state()
            return {
                "ok": True,
                "suite": self.suite,
                "episode_id": self.episode_id,
                "guest_bundle_hash": created["semantic_guest_bundle_hash"],
                "semantic_operations": state.get("semantic_operations"),
                **detail,
            }
        finally:
            self.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-url", default="http://127.0.0.1:8079")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--source-url")
    parser.add_argument(
        "--suite",
        choices=(
            "core", "browser", "chrome-actions", "extension", "dialog", "office",
            "calc-artifact", "research", "packages", "cross-app",
            "extension-source",
        ),
        required=True,
    )
    args = parser.parse_args()
    if not args.task.is_file():
        raise SystemExit(f"task is absent: {args.task}")
    print(json.dumps(Canary(
        args.env_url, args.task, args.suite, source_url=args.source_url,
    ).run(), sort_keys=True))


if __name__ == "__main__":
    main()
