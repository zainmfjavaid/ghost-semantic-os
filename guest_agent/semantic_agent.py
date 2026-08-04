#!/usr/bin/env python3
"""Versioned, task-agnostic semantic agent for the Ghost OSWorld guest.

The daemon is intentionally ignorant of OSWorld task JSON and evaluators. It
exposes generic live accessibility, OS, filesystem, and artifact state to the
outer semantic kernel. Native object paths and coordinates never cross this
boundary; short-lived random capabilities refer to in-process objects.
"""
from __future__ import annotations

import hashlib
import csv
import io
import json
import mimetypes
import os
import platform
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import struct
import tempfile
import threading
import time
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlparse
from urllib.request import urlopen
from xml.etree import ElementTree as ET

AGENT_VERSION = "1.0.0-alpha.1"
MAX_BODY_BYTES = 1_048_576
MAX_RECORDS = 5_000
MAX_TEXT = 2_000
TOKEN = os.environ.get("GHOST_SEMANTIC_TOKEN", "")
PORT = int(os.environ.get("GHOST_SEMANTIC_PORT", "8765"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AgentError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        side_effect_state: str | None = None,
        missing_capability: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.side_effect_state = (
            side_effect_state
            if side_effect_state is not None
            else "unknown" if code == "uncertain" else "none"
        )
        if self.side_effect_state not in {"none", "applied", "unknown"}:
            raise ValueError("invalid side-effect state")
        self.missing_capability = missing_capability


def _private_member(value: Any, names: Sequence[str]) -> Any:
    """Read one private native property without allowing probe errors outward."""

    for name in names:
        try:
            member = getattr(value, name)
            return member() if callable(member) else member
        except Exception:
            continue
    return None


def _accessible_process_id(accessible: Any) -> int | None:
    value = _private_member(
        accessible,
        ("get_process_id", "getProcessId", "process_id", "processId"),
    )
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _private_atspi_identity(accessible: Any) -> str | None:
    """Return a stable private identity for an AT-SPI accessible when possible.

    libatspi's durable identity is the application D-Bus name plus object path.
    Bindings do not expose those fields uniformly, so fall back to an explicit
    accessible id or a process-qualified structural ancestry. Nothing returned
    here crosses the guest boundary; AgentState hashes it before table lookup.
    """

    looks_accessible = any(
        hasattr(accessible, name)
        for name in (
            "getRoleName", "get_role_name", "getState", "get_state_set",
            "queryComponent", "get_component_iface",
        )
    )
    if not looks_accessible:
        return None

    transport = _private_member(accessible, ("parent",))
    application = _private_member(
        accessible, ("getApplication", "get_application", "application"),
    )
    if application is None and transport is not None:
        application = _private_member(transport, ("app", "application"))
    path = _private_member(
        accessible, ("object_path", "get_object_path", "_object_path", "path", "_path"),
    )
    if path is None and transport is not None:
        path = _private_member(transport, ("path", "object_path", "_path"))
    bus_name = None
    for candidate in (accessible, application, transport):
        if candidate is None:
            continue
        bus_name = _private_member(
            candidate, ("bus_name", "get_bus_name", "_bus_name", "busName"),
        )
        if bus_name:
            break
    if path and (bus_name or _accessible_process_id(accessible)):
        owner = str(bus_name) if bus_name else f"pid:{_accessible_process_id(accessible)}"
        return f"dbus:{owner}:{path}"

    pid = _accessible_process_id(accessible)
    accessible_id = _private_member(
        accessible, ("get_accessible_id", "getAccessibleId", "accessible_id"),
    )
    if pid and accessible_id:
        return f"accessible-id:{pid}:{accessible_id}"

    segments: list[tuple[str, str, int | None]] = []
    current = accessible
    seen: set[int] = set()
    has_stable_position = False
    for _depth in range(48):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        role = _private_member(current, ("getRoleName", "get_role_name", "role"))
        name = _private_member(current, ("getName", "get_name", "name"))
        index_value = _private_member(
            current, ("getIndexInParent", "get_index_in_parent", "index_in_parent"),
        )
        try:
            index = int(index_value)
        except (TypeError, ValueError):
            index = None
        if index is not None and index >= 0:
            has_stable_position = True
        segments.append((
            " ".join(str(role or "unknown").casefold().split()),
            " ".join(str(name or "").casefold().split()),
            index,
        ))
        current = _private_member(
            current,
            ("getParent", "get_parent", "accessible_parent"),
        )
    if segments and (pid is not None or has_stable_position or len(segments) > 1):
        return "structural:" + json.dumps(
            {"pid": pid, "path": list(reversed(segments))},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return None


class AgentState:
    def __init__(self) -> None:
        self.started_at = _now()
        self.refs: dict[str, Any] = {}
        self.keys: dict[str, str] = {}
        self.revision_counter = 0
        self.last_digest = ""
        self.modified_documents: list[dict[str, Any]] = []
        self.blob_staging: dict[str, dict[str, Any]] = {}
        # Private transport paging must be a view over one immutable snapshot.
        # Rebuilding a live AT-SPI tree or UNO range for every 100-record page
        # both multiplied query latency and made ordinary UI churn look like a
        # revision conflict between pages.  These snapshots never cross the
        # guest boundary and are discarded as soon as the final page is read.
        self.private_snapshots: dict[str, dict[str, Any]] = {}
        self.private_snapshot_lock = threading.RLock()
        injected_bundle_hash = os.environ.get("GHOST_SEMANTIC_BUNDLE_HASH", "")
        if re.fullmatch(r"[0-9a-f]{64}", injected_bundle_hash):
            self.bundle_hash = injected_bundle_hash
        else:
            try:
                self.bundle_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            except OSError:
                self.bundle_hash = "unavailable"

    def clear_private_snapshots(self) -> None:
        with self.private_snapshot_lock:
            self.private_snapshots.clear()

    def ref_for(self, native: Any, fingerprint: dict[str, Any]) -> str:
        # Native AT-SPI object identity stays private. The semantic fingerprint
        # is used only to keep capabilities stable within this daemon process.
        # pyatspi may allocate a fresh Python proxy on every tree walk, so
        # ``id(native)`` is not a stable identity. Its string/repr contains the
        # private D-Bus identity; hash it here and never expose the source text.
        private_identity = _private_atspi_identity(native)
        native_identity = hashlib.sha256(
            (
                private_identity
                or f"fallback:{type(native).__name__}:{native!s}:{native!r}"
            ).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        # Capability identity must not change merely because a value, checked
        # state, document modified flag, or text property changes. The outer
        # kernel compares the current semantic fingerprint before every action
        # and rejects action-relevant drift; this table only binds a capability
        # to the native object identity for the lifetime of this daemon.
        key = native_identity
        ref = self.keys.get(key)
        if ref is None:
            ref = f"entity_{secrets.token_urlsafe(18)}"
            self.keys[key] = ref
        self.refs[ref] = native
        return ref

    def revision(self, records: list[dict[str, Any]]) -> str:
        digest = _json_hash(records)
        if digest != self.last_digest:
            self.revision_counter += 1
            self.last_digest = digest
        return f"revision_{self.revision_counter}_{digest[:12]}"


STATE = AgentState()
UNO_LOCK = threading.RLock()
UNO_REFS: set[str] = set()


def _atspi_module():
    try:
        import pyatspi  # type: ignore

        return pyatspi
    except Exception as error:
        raise AgentError(
            "adapter_unavailable", f"AT-SPI unavailable: {type(error).__name__}", retryable=True,
        ) from error


def _uno_connection():
    try:
        import uno  # type: ignore

        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local
        )
        context = resolver.resolve(
            "uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext"
        )
        desktop = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context
        )
        return uno, context, desktop
    except Exception as error:
        raise AgentError(
            "adapter_unavailable",
            f"LibreOffice UNO unavailable: {type(error).__name__}",
            retryable=True,
        ) from error


def _uno_documents() -> list[Any]:
    _uno, _context, desktop = _uno_connection()
    documents = []
    enumeration = desktop.getComponents().createEnumeration()
    while enumeration.hasMoreElements() and len(documents) < 32:
        component = enumeration.nextElement()
        try:
            if hasattr(component, "getURL") and hasattr(component, "isModified"):
                documents.append(component)
        except Exception:
            continue
    return documents


def _uno_kind(document: Any) -> str:
    services = (
        ("com.sun.star.sheet.SpreadsheetDocument", "calc"),
        ("com.sun.star.presentation.PresentationDocument", "impress"),
        ("com.sun.star.text.TextDocument", "writer"),
    )
    for service, name in services:
        try:
            if document.supportsService(service):
                return name
        except Exception:
            continue
    return "unknown"


def _uno_document_record(document: Any) -> dict[str, Any]:
    kind = _uno_kind(document)
    url = _text(document.getURL(), 4_096)
    title = ""
    try:
        title = _text(document.getTitle(), 2_000)
    except Exception:
        title = Path(url.removeprefix("file://")).name if url else "Untitled"
    fingerprint = {
        "kind": kind, "url": url, "title": title,
        "modified": bool(document.isModified()),
    }
    ref = _uno_ref(document, f"document.{kind}", {"url": url, "title": title})
    return {
        "ref": ref, "kind": f"document.{kind}", "application": "libreoffice",
        "document_type": kind, "title": title, "url": url,
        "modified": fingerprint["modified"],
        "advertised_actions": [
            "activate", "save", "save_as", "export", "undo", "redo", "reload",
        ],
        "source": "libreoffice.uno", "freshness": "live",
    }


def _uno_active_document(arguments: dict[str, Any] | None = None) -> Any:
    arguments = arguments or {}
    requested = arguments.get("document_ref")
    if isinstance(requested, str):
        return _resolve(requested)
    documents = _uno_documents()
    if not documents:
        raise AgentError("not_found", "no open LibreOffice document", retryable=True)
    try:
        _uno, _context, desktop = _uno_connection()
        current = desktop.getCurrentComponent()
        for document in documents:
            if document is current or _uno_property(document, "URL") == _uno_property(current, "URL"):
                return document
    except Exception:
        pass
    for document in documents:
        try:
            frame = document.getCurrentController().getFrame()
            window = frame.getContainerWindow()
            if bool(_uno_property(window, "Active", False)):
                return document
        except Exception:
            continue
    return documents[-1]


def _uno_ref(native: Any, kind: str, stable: dict[str, Any]) -> str:
    ref = STATE.ref_for(native, {"kind": kind, **stable})
    UNO_REFS.add(ref)
    return ref


def _uno_property(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        try:
            return obj.getPropertyValue(name)
        except Exception:
            return default


def _uno_set_property(obj: Any, name: str, value: Any) -> None:
    try:
        setattr(obj, name, value)
        return
    except Exception:
        pass
    try:
        obj.setPropertyValue(name, value)
    except Exception as error:
        raise AgentError(
            "unsupported", f"UNO property is not writable: {name}"
        ) from error


def _uno_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_uno_json(item) for item in value[:1_000]]
    for fields in (
        ("X", "Y"), ("Width", "Height"),
        ("Sheet", "Column", "Row"),
        ("Sheet", "StartColumn", "StartRow", "EndColumn", "EndRow"),
    ):
        if all(hasattr(value, field) for field in fields):
            return {field.casefold(): _uno_json(getattr(value, field)) for field in fields}
    return _text(value)


def _uno_file_url(path: str) -> str:
    if not path.startswith("/"):
        raise AgentError("invalid_request", "LibreOffice path must be absolute")
    uno, _context, _desktop = _uno_connection()
    return str(uno.systemPathToFileUrl(path))


def _uno_property_values(values: dict[str, Any]) -> tuple[Any, ...]:
    uno, _context, _desktop = _uno_connection()
    result = []
    for name, value in values.items():
        prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
        prop.Name = name
        prop.Value = value
        result.append(prop)
    return tuple(result)


def _uno_doc_for_object(native: Any) -> Any:
    for document in _uno_documents():
        if native is document:
            return document
        kind = _uno_kind(document)
        try:
            if kind == "calc":
                sheets = document.getSheets()
                for name in sheets.getElementNames():
                    sheet = sheets.getByName(name)
                    if native is sheet:
                        return document
            elif kind == "writer":
                if native is document.getText():
                    return document
            elif kind == "impress":
                pages = document.getDrawPages()
                for index in range(pages.getCount()):
                    page = pages.getByIndex(index)
                    if native is page:
                        return document
        except Exception:
            continue
    # Most UNO child proxies expose a model, document, or spreadsheet parent.
    for name in ("Model", "Document", "Spreadsheet"):
        candidate = _uno_property(native, name)
        if candidate is not None and candidate is not native:
            try:
                if _uno_kind(candidate) != "unknown":
                    return candidate
            except Exception:
                pass
    return _uno_active_document()


def _uno_document_records() -> list[dict[str, Any]]:
    records = [_uno_document_record(document) for document in _uno_documents()]
    STATE.modified_documents = [
        {"ref": record["ref"], "title": record["title"], "url": record["url"]}
        for record in records if record.get("modified")
    ]
    return records


def _writer_paragraph_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        enumeration = document.getText().createEnumeration()
    except Exception as error:
        raise AgentError("adapter_unavailable", "Writer text enumeration unavailable") from error
    index = 0
    while enumeration.hasMoreElements() and len(records) < MAX_RECORDS:
        paragraph = enumeration.nextElement()
        try:
            if not paragraph.supportsService("com.sun.star.text.Paragraph"):
                continue
        except Exception:
            continue
        style = _text(_uno_property(paragraph, "ParaStyleName", ""), 256)
        text = _text(_uno_property(paragraph, "String", ""), MAX_TEXT)
        ref = _uno_ref(paragraph, "writer.paragraph", {"index": index})
        records.append({
            "ref": ref, "kind": "writer.paragraph", "index": index,
            "text": text, "style": style,
            "alignment": _uno_json(_uno_property(paragraph, "ParaAdjust")),
            "advertised_actions": [
                "replace_text", "replace_with_paragraphs", "insert_paragraphs",
                "insert_text", "delete_text", "set_paragraph_properties",
            ],
            "source": "libreoffice.uno", "freshness": "live",
        })
        index += 1
    return records


def _writer_run_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    paragraphs = _writer_paragraph_records(document)
    for paragraph_record in paragraphs:
        paragraph = _resolve(paragraph_record["ref"])
        try:
            portions = paragraph.createEnumeration()
        except Exception:
            continue
        run_index = 0
        while portions.hasMoreElements() and len(records) < MAX_RECORDS:
            run = portions.nextElement()
            portion_type = _text(_uno_property(run, "TextPortionType", "Text"), 128)
            text = _text(_uno_property(run, "String", ""), MAX_TEXT)
            ref = _uno_ref(run, "writer.run", {
                "paragraph": paragraph_record["index"], "run": run_index,
            })
            records.append({
                "ref": ref, "kind": "writer.run",
                "paragraph_index": paragraph_record["index"], "run_index": run_index,
                "portion_type": portion_type, "text": text,
                "character_style": _text(_uno_property(run, "CharStyleName", ""), 256),
                "font_name": _text(_uno_property(run, "CharFontName", ""), 256),
                "font_size": _uno_json(_uno_property(run, "CharHeight")),
                "bold": _uno_json(_uno_property(run, "CharWeight")),
                "italic": _uno_json(_uno_property(run, "CharPosture")),
                "color": _uno_json(_uno_property(run, "CharColor")),
                "advertised_actions": ["replace_text", "delete_text", "set_character_properties"],
                "source": "libreoffice.uno", "freshness": "live",
            })
            run_index += 1
    return records


def _writer_table_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        tables = document.getTextTables()
        names = tables.getElementNames()
    except Exception:
        return records
    for name in names[:1_000]:
        table = tables.getByName(name)
        ref = _uno_ref(table, "writer.table", {"name": name})
        cell_names = list(table.getCellNames())[:5_000]
        cells = []
        for cell_name in cell_names:
            cell = table.getCellByName(cell_name)
            cells.append({"name": cell_name, "text": _text(_uno_property(cell, "String", ""))})
        records.append({
            "ref": ref, "kind": "writer.table", "name": name,
            "cells": cells, "cell_count": len(cell_names),
            "advertised_actions": ["set_table_cell"],
            "source": "libreoffice.uno", "freshness": "live",
        })
    return records


def _writer_style_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        families = document.getStyleFamilies()
        for family_name in families.getElementNames():
            family = families.getByName(family_name)
            for style_name in family.getElementNames():
                style = family.getByName(style_name)
                ref = _uno_ref(style, "writer.style", {
                    "family": family_name, "name": style_name,
                })
                records.append({
                    "ref": ref, "kind": "writer.style", "family": family_name,
                    "name": style_name, "user_defined": bool(_uno_property(style, "IsUserDefined", False)),
                    "in_use": bool(_uno_property(style, "IsInUse", False)),
                    "advertised_actions": ["set_style_properties"],
                    "source": "libreoffice.uno", "freshness": "live",
                })
                if len(records) >= MAX_RECORDS:
                    return records
    except Exception:
        pass
    return records


def _calc_sheet_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sheets = document.getSheets()
    names = list(sheets.getElementNames())
    active = None
    try:
        active = document.getCurrentController().getActiveSheet()
    except Exception:
        pass
    for index, name in enumerate(names):
        sheet = sheets.getByName(name)
        ref = _uno_ref(sheet, "spreadsheet.sheet", {"name": name})
        records.append({
            "ref": ref, "kind": "spreadsheet.sheet", "name": name, "index": index,
            "active": sheet is active,
            "visible": bool(_uno_property(sheet, "IsVisible", True)),
            "advertised_actions": [
                "rename_sheet", "reorder_sheet", "add_sheet", "delete_sheet",
                "insert_rows", "delete_rows", "insert_columns", "delete_columns",
            ],
            "source": "libreoffice.uno", "freshness": "live",
        })
    return records


def _calc_range(document: Any, parameters: dict[str, Any]) -> tuple[Any, str, str]:
    sheets = document.getSheets()
    requested_sheet = parameters.get("sheet")
    sheet_name = str(requested_sheet) if requested_sheet is not None else ""
    requested_range = parameters.get("range") or parameters.get("address")
    if not requested_range:
        try:
            selected = document.getCurrentController().getSelection()
            selected_address = selected.getRangeAddress()
            names = list(sheets.getElementNames())
            if 0 <= int(selected_address.Sheet) < len(names):
                sheet_name = sheet_name or str(names[int(selected_address.Sheet)])
            start = _calc_a1(int(selected_address.StartColumn), int(selected_address.StartRow))
            end = _calc_a1(int(selected_address.EndColumn), int(selected_address.EndRow))
            requested_range = start if start == end else f"{start}:{end}"
        except Exception:
            requested_range = "A1"
    if not sheet_name:
        try:
            active = document.getCurrentController().getActiveSheet()
            sheet_name = next(
                name for name in sheets.getElementNames() if sheets.getByName(name) is active
            )
        except Exception:
            names = sheets.getElementNames()
            if not names:
                raise AgentError("not_found", "spreadsheet has no sheets")
            sheet_name = names[0]
    if not sheets.hasByName(sheet_name):
        raise AgentError("not_found", f"sheet does not exist: {sheet_name}")
    range_name = str(requested_range or "A1")
    if len(range_name) > 128:
        raise AgentError("invalid_request", "range address is too long")
    try:
        return sheets.getByName(sheet_name).getCellRangeByName(range_name), sheet_name, range_name
    except Exception as error:
        raise AgentError("invalid_request", f"invalid spreadsheet range: {range_name}") from error


def _calc_a1(column: int, row: int) -> str:
    label = ""
    value = column + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return f"{label}{row + 1}"


def _calc_cell_records(document: Any, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    cell_range, sheet_name, requested = _calc_range(document, parameters)
    address = cell_range.getRangeAddress()
    count = (address.EndRow - address.StartRow + 1) * (address.EndColumn - address.StartColumn + 1)
    if count > MAX_RECORDS:
        raise AgentError("budget_exhausted", "spreadsheet range exceeds 5000 cells")
    records = []
    sheet = document.getSheets().getByName(sheet_name)
    for row in range(address.StartRow, address.EndRow + 1):
        for column in range(address.StartColumn, address.EndColumn + 1):
            cell = sheet.getCellByPosition(column, row)
            cell_address = cell.getCellAddress()
            ref = _uno_ref(cell, "spreadsheet.cell", {
                "sheet": sheet_name, "column": column, "row": row,
            })
            records.append({
                "ref": ref, "kind": "spreadsheet.cell", "sheet": sheet_name,
                "column": column, "row": row,
                "address": _text(
                    _uno_property(cell, "AbsoluteName", _calc_a1(column, row)), 256
                ).split(".")[-1].replace("$", ""),
                "value": _uno_json(_uno_property(cell, "Value", 0.0)),
                "display": _text(_uno_property(cell, "String", ""), MAX_TEXT),
                "formula": _text(_uno_property(cell, "Formula", ""), MAX_TEXT),
                "type": _uno_json(_uno_property(cell, "Type")),
                "number_format": _uno_json(_uno_property(cell, "NumberFormat")),
                "style": _text(_uno_property(cell, "CellStyle", ""), 256),
                "background_color": _uno_json(_uno_property(cell, "CellBackColor")),
                "advertised_actions": [
                    "set_value", "set_formula", "set_text", "set_cell_properties",
                ],
                "source": "libreoffice.uno", "freshness": "live",
            })
    return records


def _calc_range_record(document: Any, parameters: dict[str, Any]) -> dict[str, Any]:
    cell_range, sheet_name, requested = _calc_range(document, parameters)
    address = cell_range.getRangeAddress()
    ref = _uno_ref(cell_range, "spreadsheet.range", {
        "sheet": sheet_name, "range": requested,
    })
    return {
        "ref": ref, "kind": "spreadsheet.range", "sheet": sheet_name,
        "range": requested, "address": _uno_json(address),
        "data": _uno_json(cell_range.getDataArray()),
        "formulas": _uno_json(cell_range.getFormulaArray()),
        "advertised_actions": ["set_range_values", "set_range_formulas", "fill", "set_range_properties"],
        "source": "libreoffice.uno", "freshness": "live",
    }


def _canonical_formula(value: Any) -> str | None:
    text = _text(value, MAX_TEXT).strip()
    if not text:
        return None
    return text if text.startswith("=") else f"={text}"


def _canonical_number(value: Any) -> Any:
    """Normalize semantically equal UNO/OOXML numeric representations."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _calc_cell_content_type(cell: Any) -> str:
    try:
        native = cell.getType()
    except Exception:
        native = _uno_property(cell, "Type", "")
    value = getattr(native, "value", native)
    normalized = _text(value, 128).casefold().rsplit(".", 1)[-1]
    return normalized if normalized in {"empty", "value", "text", "formula"} else "unknown"


def _canonical_calc_live(document: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    sheets = document.getSheets()
    total_cells = 0
    for sheet_name in sheets.getElementNames():
        sheet = sheets.getByName(sheet_name)
        try:
            cursor = sheet.createCursor()
            cursor.gotoEndOfUsedArea(True)
            address = cursor.getRangeAddress()
        except Exception as error:
            raise AgentError(
                "representation_gap", "Calc used-area model is unavailable"
            ) from error
        count = (address.EndRow + 1) * (address.EndColumn + 1)
        total_cells += count
        if total_cells > MAX_RECORDS:
            raise AgentError(
                "budget_exhausted", "live spreadsheet structure exceeds 5000 cells"
            )
        cells = []
        for row in range(address.EndRow + 1):
            for column in range(address.EndColumn + 1):
                cell = sheet.getCellByPosition(column, row)
                formula_text = _text(_uno_property(cell, "Formula", ""), MAX_TEXT)
                display = _text(_uno_property(cell, "String", ""), MAX_TEXT)
                numeric = _uno_property(cell, "Value", 0.0)
                content_type = _calc_cell_content_type(cell)
                if content_type == "formula" or formula_text.startswith("="):
                    canonical = {"formula": _canonical_formula(formula_text)}
                elif content_type == "value":
                    canonical = {"value": _canonical_number(numeric)}
                elif content_type == "text":
                    canonical = {"value": display}
                elif content_type == "empty":
                    continue
                elif display:
                    canonical = {"value": display}
                elif isinstance(numeric, (int, float)) and not isinstance(numeric, bool) and numeric != 0:
                    canonical = {"value": _canonical_number(numeric)}
                else:
                    continue
                cells.append({"address": _calc_a1(column, row), **canonical})
        result.append({"name": str(sheet_name), "cells": cells})
    return result


def _canonical_calc_disk(disk: dict[str, Any]) -> list[dict[str, Any]]:
    sheets = []
    for sheet in disk.get("sheets", []):
        cells = []
        for cell in sheet.get("cells", []):
            address = _text(cell.get("address"), 128).replace("$", "")
            formula = cell.get("formula")
            if formula not in {None, ""}:
                cells.append({"address": address, "formula": _canonical_formula(formula)})
            elif cell.get("value") not in {None, ""}:
                cells.append({
                    "address": address,
                    "value": _canonical_number(cell.get("value")),
                })
        sheets.append({"name": str(sheet.get("name") or ""), "cells": cells})
    return sheets


def _canonical_writer_live(document: Any) -> dict[str, Any]:
    paragraphs = [
        str(record.get("text") or "")
        for record in _writer_paragraph_records(document)
    ]
    tables = []
    for table in _writer_table_records(document):
        tables.append({
            "cells": [str(cell.get("text") or "") for cell in table.get("cells", [])]
        })
    return {"paragraphs": paragraphs, "tables": tables}


def _canonical_writer_disk(disk: dict[str, Any]) -> dict[str, Any]:
    raw_paragraphs = disk.get("body_paragraphs", disk.get("paragraphs", []))
    paragraphs = [
        str(record.get("text") or "") if isinstance(record, dict) else str(record or "")
        for record in raw_paragraphs
    ]
    tables = []
    for table in disk.get("tables", []):
        cells = [
            str(cell or "")
            for row in table.get("rows", [])
            for cell in row
        ]
        tables.append({"cells": cells})
    return {"paragraphs": paragraphs, "tables": tables}


def _first_structural_difference(left: Any, right: Any, path: str = "$") -> dict[str, Any] | None:
    if left == right:
        return None
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}"
            if key not in left:
                return {"path": child_path, "live": "<missing>", "disk": _text(right[key], 300)}
            if key not in right:
                return {"path": child_path, "live": _text(left[key], 300), "disk": "<missing>"}
            difference = _first_structural_difference(left[key], right[key], child_path)
            if difference is not None:
                return difference
        return None
    if isinstance(left, list) and isinstance(right, list):
        common = min(len(left), len(right))
        for index in range(common):
            difference = _first_structural_difference(
                left[index], right[index], f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return {
            "path": f"{path}.length", "live": len(left), "disk": len(right),
        }
    return {"path": path, "live": _text(left, 300), "disk": _text(right, 300)}


def _structural_comparison(live: Any, disk: Any) -> dict[str, Any]:
    difference = _first_structural_difference(live, disk)
    return {
        "matched": difference is None,
        "live_sha256": _json_hash(live),
        "disk_sha256": _json_hash(disk),
        "first_difference": difference,
    }


def _calc_chart_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sheets = document.getSheets()
    for sheet_name in sheets.getElementNames():
        charts = sheets.getByName(sheet_name).getCharts()
        for name in charts.getElementNames():
            chart = charts.getByName(name)
            ref = _uno_ref(chart, "spreadsheet.chart", {"sheet": sheet_name, "name": name})
            records.append({
                "ref": ref, "kind": "spreadsheet.chart", "sheet": sheet_name, "name": name,
                "ranges": _uno_json(chart.getRanges()),
                "embedded": bool(_uno_property(chart, "HasMainTitle", False)),
                "advertised_actions": ["delete_chart"],
                "source": "libreoffice.uno", "freshness": "live",
            })
    return records


def _impress_slide_records(document: Any, include_shapes: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pages = document.getDrawPages()
    for index in range(pages.getCount()):
        page = pages.getByIndex(index)
        ref = _uno_ref(page, "presentation.slide", {"index": index})
        slide = {
            "ref": ref, "kind": "presentation.slide", "index": index,
            "name": _text(_uno_property(page, "Name", f"Slide {index + 1}"), 256),
            "layout": _uno_json(_uno_property(page, "Layout")),
            "shape_count": page.getCount(),
            "advertised_actions": [
                "create_slide", "delete_slide", "set_slide_properties", "add_text_shape",
            ],
            "source": "libreoffice.uno", "freshness": "live",
        }
        if not include_shapes:
            records.append(slide)
            continue
        for shape_index in range(page.getCount()):
            shape = page.getByIndex(shape_index)
            shape_ref = _uno_ref(shape, "presentation.shape", {
                "slide": index, "shape": shape_index,
            })
            native_name = _text(_uno_property(shape, "Name", ""), 220)
            object_label = f"Slide {index + 1} — Object {shape_index + 1}"
            if native_name:
                object_label += f" — {native_name}"
            records.append({
                "ref": shape_ref, "kind": "presentation.shape", "slide_index": index,
                "shape_index": shape_index,
                # Impress-generated names such as ``Google Shape;84;p13`` do
                # not tell a policy model which slide owns the object.  Keep
                # the native name for identity/debugging, but prefix every
                # model-facing label with stable one-based slide/object
                # context so a compact query result remains self-contained.
                "name": object_label,
                "native_name": native_name,
                "shape_type": _text(shape.getShapeType(), 256),
                "text": _text(_uno_property(shape, "String", ""), MAX_TEXT),
                "position": _uno_json(shape.getPosition()), "size": _uno_json(shape.getSize()),
                "advertised_actions": ["replace_text", "set_shape_properties", "delete_shape"],
                "source": "libreoffice.uno", "freshness": "live",
            })
    return records


def _impress_note_records(document: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pages = document.getDrawPages()
    for slide_index in range(pages.getCount()):
        page = pages.getByIndex(slide_index)
        try:
            notes_page = page.getNotesPage()
        except Exception as error:
            raise AgentError(
                "representation_gap", "Impress notes model is unavailable"
            ) from error
        texts = []
        for shape_index in range(notes_page.getCount()):
            shape = notes_page.getByIndex(shape_index)
            text = _text(_uno_property(shape, "String", ""), MAX_TEXT)
            if text:
                texts.append(text)
        ref = _uno_ref(notes_page, "presentation.notes", {"slide": slide_index})
        records.append({
            "ref": ref,
            "kind": "presentation.notes",
            "slide_index": slide_index,
            "text": _text("\n".join(texts), MAX_TEXT),
            "advertised_actions": ["add_text_shape"],
            "source": "libreoffice.uno",
            "freshness": "live",
        })
    return records


def _impress_style_records(document: Any) -> list[dict[str, Any]]:
    records = _writer_style_records(document)
    return [
        {
            **record,
            "kind": "presentation.style",
        }
        for record in records
    ]


def _build_uno_snapshot(
    resource: str, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    with UNO_LOCK:
        documents = _uno_document_records()
        if resource == "document.sessions":
            records = documents
        else:
            parameters = payload.get("parameters") or {}
            document = _uno_active_document(parameters)
            kind = _uno_kind(document)
            if resource in {"document.state", "document.metadata", "document.save_state", "document.selection", "document.exports"}:
                record = _uno_document_record(document)
                record.update({
                    "selection": _text(_uno_property(document.getCurrentSelection(), "String", ""), MAX_TEXT),
                    "has_location": bool(record.get("url")),
                    "read_only": bool(_uno_property(document, "IsReadOnly", False)),
                })
                records = [record]
            elif resource in {"writer.paragraphs", "writer.headings", "writer.lists", "writer.sections"} and kind == "writer":
                records = _writer_paragraph_records(document)
                if resource == "writer.headings":
                    records = [record for record in records if record.get("style", "").casefold().startswith("heading")]
                elif resource == "writer.lists":
                    records = [record for record in records if _uno_property(_resolve(record["ref"]), "NumberingStyleName", "")]
            elif resource == "writer.runs" and kind == "writer":
                records = _writer_run_records(document)
            elif resource == "writer.tables" and kind == "writer":
                records = _writer_table_records(document)
            elif resource == "writer.styles" and kind == "writer":
                records = _writer_style_records(document)
            elif resource in {"writer.page_config", "writer.headers_footers"} and kind == "writer":
                records = _writer_style_records(document)
                records = [record for record in records if record.get("family") == "PageStyles"]
            elif resource in {"writer.hyperlinks", "writer.bookmarks", "writer.comments"} and kind == "writer":
                supplier = {
                    "writer.bookmarks": "getBookmarks", "writer.comments": "getTextFields",
                    "writer.hyperlinks": "getTextFields",
                }[resource]
                collection = getattr(document, supplier)()
                names = list(collection.getElementNames()) if hasattr(collection, "getElementNames") else []
                records = [{
                    "ref": _uno_ref(collection.getByName(name), resource[:-1], {"name": name}),
                    "kind": resource[:-1], "name": name, "source": "libreoffice.uno", "freshness": "live",
                } for name in names[:MAX_RECORDS]]
            elif resource == "spreadsheet.sheets" and kind == "calc":
                records = _calc_sheet_records(document)
            elif resource in {"spreadsheet.cells", "spreadsheet.formulas", "spreadsheet.styles", "spreadsheet.rows", "spreadsheet.columns", "spreadsheet.selection"} and kind == "calc":
                records = _calc_cell_records(document, parameters)
            elif resource == "spreadsheet.ranges" and kind == "calc":
                records = [_calc_range_record(document, parameters)]
            elif resource == "spreadsheet.named_ranges" and kind == "calc":
                names = document.getNamedRanges()
                records = [{
                    "ref": _uno_ref(names.getByName(name), "spreadsheet.named_range", {"name": name}),
                    "kind": "spreadsheet.named_range", "name": name,
                    "content": _text(names.getByName(name).getContent(), 2_000),
                    "source": "libreoffice.uno", "freshness": "live",
                } for name in names.getElementNames()]
            elif resource == "spreadsheet.charts" and kind == "calc":
                records = _calc_chart_records(document)
            elif resource in {"spreadsheet.filters", "spreadsheet.frozen_panes"} and kind == "calc":
                controller = document.getCurrentController()
                records = [{
                    "ref": _uno_ref(controller, resource[:-1], {"resource": resource}),
                    "kind": resource[:-1],
                    "frozen_rows": int(_uno_property(controller, "FirstVisibleRow", 0)),
                    "frozen_columns": int(_uno_property(controller, "FirstVisibleColumn", 0)),
                    "source": "libreoffice.uno", "freshness": "live",
                }]
            elif resource == "presentation.slides" and kind == "impress":
                records = _impress_slide_records(document)
            elif resource == "presentation.shapes" and kind == "impress":
                records = _impress_slide_records(document, include_shapes=True)
            elif resource == "presentation.notes" and kind == "impress":
                records = _impress_note_records(document)
            elif resource == "presentation.styles" and kind == "impress":
                records = _impress_style_records(document)
            else:
                raise AgentError(
                    "unsupported", f"resource {resource} is not available for active {kind} document"
                )
        revision = f"uno_{_json_hash(records)[:16]}"
        return records, revision


def _query_uno(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _private_snapshot_page(
        resource,
        payload,
        lambda: _build_uno_snapshot(resource, payload),
    )


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) and name.startswith("get") else value
    except Exception:
        return default


def _role_name(accessible: Any) -> str:
    try:
        return _text(accessible.getRoleName(), 128)
    except Exception:
        return "unknown"


def _states(accessible: Any, pyatspi: Any) -> dict[str, bool]:
    result: dict[str, bool] = {}
    try:
        state_set = accessible.getState()
    except Exception:
        return result
    constants = {
        "active": "STATE_ACTIVE",
        "busy": "STATE_BUSY",
        "checked": "STATE_CHECKED",
        "editable": "STATE_EDITABLE",
        "enabled": "STATE_ENABLED",
        "expanded": "STATE_EXPANDED",
        "focusable": "STATE_FOCUSABLE",
        "focused": "STATE_FOCUSED",
        "invalid": "STATE_INVALID_ENTRY",
        "multiselectable": "STATE_MULTISELECTABLE",
        "pressed": "STATE_PRESSED",
        "read_only": "STATE_READ_ONLY",
        "required": "STATE_REQUIRED",
        "selected": "STATE_SELECTED",
        "showing": "STATE_SHOWING",
        "visible": "STATE_VISIBLE",
        "modal": "STATE_MODAL",
    }
    for public, constant in constants.items():
        flag = getattr(pyatspi, constant, None)
        if flag is None:
            continue
        try:
            result[public] = bool(state_set.contains(flag))
        except Exception:
            pass
    return result


def _actions(accessible: Any) -> list[str]:
    try:
        interface = accessible.queryAction()
        return [_text(interface.getName(index), 128) for index in range(interface.nActions)]
    except Exception:
        return []


def _executable_text_actions(
    accessible: Any, state: dict[str, bool],
) -> list[str]:
    """Advertise text mutation only after proving EditableText exists.

    AT-SPI's EDITABLE state is descriptive, not an interface guarantee. Chrome
    toolbar entries are a common counterexample: they can report EDITABLE while
    rejecting ``queryEditableText``. The facade must never turn that state bit
    into a model-visible typing capability which the guest cannot execute.
    """

    if state.get("editable") is not True or state.get("read_only") is True:
        return []
    try:
        accessible.queryEditableText()
    except Exception:
        return []
    return ["set_text", "insert_text", "replace_text"]


_TEXT_MUTATION_ACTION_NAMES = frozenset({
    "set text", "insert text", "replace text",
})


def _record_actions(accessible: Any, state: dict[str, bool]) -> list[str]:
    """Return only advertised actions executable through the public contract."""

    executable_text = _executable_text_actions(accessible, state)
    actions = [
        action for action in _actions(accessible)
        if " ".join(re.sub(r"[-_.]+", " ", action.casefold()).split())
        not in _TEXT_MUTATION_ACTION_NAMES
    ]
    for semantic_action in executable_text:
        if semantic_action not in actions:
            actions.append(semantic_action)
    return actions


def _value(accessible: Any) -> Any:
    try:
        interface = accessible.queryValue()
        return {
            "current": interface.currentValue,
            "minimum": interface.minimumValue,
            "maximum": interface.maximumValue,
            "increment": interface.minimumIncrement,
        }
    except Exception:
        return None


def _accessible_text(accessible: Any) -> str:
    try:
        interface = accessible.queryText()
        return _text(interface.getText(0, min(interface.characterCount, MAX_TEXT)), MAX_TEXT)
    except Exception:
        return ""


def _hyperlink_uri(accessible: Any) -> str:
    """Return one exact public web URI proved by AT-SPI Hyperlink.

    A link's accessible name is not its target.  In particular, Thunderbird
    exposes names such as ``Billing & Cost Management Page`` while keeping the
    actual URI behind the Hyperlink interface.  Only a unique HTTP(S) anchor
    is exported: guessing among anchors or treating visible text as a URL
    would make a later browser action dishonest.
    """

    try:
        interface = accessible.queryHyperlink()
        anchor_count = int(_safe_attr(interface, "nAnchors", 0) or 0)
    except Exception:
        return ""
    candidates: list[str] = []
    for index in range(max(0, anchor_count)):
        try:
            uri = _text(interface.getURI(index), 4_096).strip()
        except Exception:
            continue
        parsed = urlparse(uri)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        if uri not in candidates:
            candidates.append(uri)
    return candidates[0] if len(candidates) == 1 else ""


def _walk_accessibility(
    *, max_depth: int = 32, roots: Sequence[Any] | None = None,
    max_records: int = MAX_RECORDS, lightweight: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    pyatspi = _atspi_module()
    desktop = pyatspi.Registry.getDesktop(0)
    records: list[dict[str, Any]] = []

    def visit(node: Any, parent_ref: str | None, depth: int) -> None:
        if len(records) >= max_records or depth > max_depth:
            return
        role = _role_name(node)
        name = _text(_safe_attr(node, "name", ""))
        description = _text(_safe_attr(node, "description", ""))
        state = _states(node, pyatspi)
        # Shallow surface discovery needs only identity/title/state. Querying
        # Text, Value, Action, and Hyperlink on even a few Calc descendants can
        # block the AT-SPI bridge until the guest transport times out. Full UI
        # element walks still collect every executable interface below.
        text = "" if lightweight else _accessible_text(node)
        actions = [] if lightweight else _record_actions(node, state)
        value = None if lightweight else _value(node)
        url = "" if lightweight else (
            _hyperlink_uri(node)
            if role.casefold() in {"link", "hyperlink"}
            else ""
        )
        fingerprint = {
            "role": role,
            "name": name,
            "description": description,
            "text": text,
            "state": state,
            "actions": actions,
            "value": value,
        }
        if url:
            fingerprint["url"] = url
        ref = STATE.ref_for(node, fingerprint)
        child_count = int(_safe_attr(node, "childCount", 0) or 0)
        record = {
            "ref": ref,
            "kind": "ui.element",
            "role": role,
            "name": name,
            "description": description,
            "text": text,
            "value": value,
            "state": state,
            "advertised_actions": actions,
            "parent_ref": parent_ref,
            "child_refs": [],
            "child_count": child_count,
            "source": "atspi",
            "freshness": "live",
        }
        if url:
            record["url"] = url
        records.append(record)
        for index in range(max(0, child_count)):
            try:
                visit(node[index], ref, depth + 1)
            except Exception:
                continue

    if roots is None:
        roots = []
        for app_index in range(int(_safe_attr(desktop, "childCount", 0) or 0)):
            try:
                roots.append(desktop[app_index])
            except Exception:
                continue
    for root in roots:
        try:
            visit(root, None, 0)
        except Exception:
            continue
    by_ref = {record["ref"]: record for record in records}
    for record in records:
        parent_ref = record.get("parent_ref")
        if parent_ref in by_ref:
            by_ref[parent_ref]["child_refs"].append(record["ref"])
    return records, STATE.revision(records)


def _where_match(record: dict[str, Any], where: Any) -> bool:
    if not where:
        return True
    if not isinstance(where, dict):
        raise AgentError("invalid_request", "where must be an object")
    if "all" in where:
        return all(_where_match(record, item) for item in where["all"])
    if "any" in where:
        return any(_where_match(record, item) for item in where["any"])
    if "not" in where:
        return not _where_match(record, where["not"])
    field = where.get("field")
    if not isinstance(field, str):
        raise AgentError("invalid_request", "filter leaf requires field")
    value: Any = record
    for part in field.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    for operation, expected in where.items():
        if operation == "field":
            continue
        if operation == "eq" and value != expected:
            return False
        if operation == "ne" and value == expected:
            return False
        if operation == "contains" and str(expected).casefold() not in str(value).casefold():
            return False
        if operation == "starts_with" and not str(value).startswith(str(expected)):
            return False
        if operation == "ends_with" and not str(value).endswith(str(expected)):
            return False
        if operation == "in" and value not in expected:
            return False
        if operation == "has" and not (
            isinstance(value, dict) and expected in value
            or isinstance(value, list) and expected in value
        ):
            return False
        if operation == "is_true" and bool(value) is not True:
            return False
        if operation == "is_false" and bool(value) is not False:
            return False
    return True


def _arguments_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: tuple[str, ...] = (),
    additional_properties: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    """Build the bounded argument contracts published to the semantic kernel."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def _uno_property_bag(names: set[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {
                "description": f"LibreOffice {name} value; type depends on the UNO property",
            }
            for name in sorted(names)
        },
        "additionalProperties": False,
        "minProperties": 1,
    }


_STRING = {"type": "string"}
_NUMBER = {"type": "number"}
_INTEGER = {"type": "integer"}
_BOOLEAN = {"type": "boolean"}
_PATH = {"type": "string", "description": "Absolute guest path"}
_WRITER_PARAGRAPH_PROPERTY_NAMES = {
    "ParaStyleName", "ParaAdjust", "ParaLeftMargin", "ParaRightMargin",
    "ParaTopMargin", "ParaBottomMargin", "ParaFirstLineIndent", "ParaLineSpacing",
    "NumberingStyleName", "BreakType",
}
_WRITER_CHARACTER_PROPERTY_NAMES = {
    "CharStyleName", "CharFontName", "CharHeight", "CharWeight", "CharPosture",
    "CharColor", "CharBackColor", "CharUnderline", "CharStrikeout",
}
_CALC_PROPERTY_NAMES = {
    "CellStyle", "NumberFormat", "CellBackColor", "CharColor", "CharFontName",
    "CharHeight", "CharWeight", "CharPosture", "HoriJustify", "VertJustify",
    "IsTextWrapped", "RotateAngle",
}
_DRAW_PROPERTY_NAMES = {
    "Name", "FillColor", "FillStyle", "LineColor", "LineStyle", "LineWidth",
    "CharColor", "CharFontName", "CharHeight", "CharWeight", "CharPosture",
    "ParaAdjust", "Visible", "Printable",
}
_TEXT_REPLACEMENT_ARGUMENTS = {
    "text": {"type": "string", "description": "Replacement text (preferred)"},
    "value": {"type": "string", "description": "Deprecated alias for text"},
    "font_size": {"type": "number", "description": "Font size in points"},
    "font_color": {
        "description": "Text color as #RRGGBB or an integer RGB value",
    },
    "font_name": _STRING,
    "paragraph_alignment": {
        "type": "string", "enum": ["left", "right", "center", "justify"],
    },
}
_DRAW_UPDATE_ARGUMENTS = {
    "properties": {
        **_uno_property_bag(_DRAW_PROPERTY_NAMES),
        "description": "Whitelisted raw UNO draw properties",
    },
    "position": _arguments_schema({"x": _INTEGER, "y": _INTEGER}),
    "size": _arguments_schema({"width": _INTEGER, "height": _INTEGER}),
    "font_size": {"type": "number", "description": "Font size in points"},
    "font_color": {"description": "Text color as #RRGGBB or integer RGB"},
    "paragraph_alignment": {
        "type": "string", "enum": ["left", "right", "center", "justify"],
    },
}


_LIBREOFFICE_ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "activate": _arguments_schema(),
    "save": _arguments_schema(),
    "save_as": _arguments_schema({"path": _PATH}, required=("path",)),
    "export": _arguments_schema(
        {"path": _PATH, "filter_name": _STRING}, required=("path",)
    ),
    "undo": _arguments_schema(),
    "redo": _arguments_schema(),
    "reload": _arguments_schema({"discard_changes": _BOOLEAN}),
    "insert_text": _arguments_schema(
        {
            "value": _STRING,
            "position": {"type": "string", "enum": ["start", "end"]},
            "offset": _INTEGER,
        },
        required=("value",),
    ),
    "replace_with_paragraphs": _arguments_schema(
        {
            "paragraphs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2_000,
                "items": {"type": "string"},
                "description": "Exact paragraph texts; empty strings create real blank paragraphs",
            },
        },
        required=("paragraphs",),
        description="Replace one Writer paragraph with an exact sequence of paragraph objects",
    ),
    "insert_paragraphs": _arguments_schema(
        {
            "paragraphs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2_000,
                "items": {"type": "string"},
                "description": "Exact paragraph texts; empty strings create real blank paragraphs",
            },
            "position": {
                "type": "string",
                "enum": ["before", "after", "end"],
            },
        },
        required=("paragraphs", "position"),
        description="Insert real Writer paragraphs relative to a current paragraph ref",
    ),
    "install_extension": _arguments_schema(
        {"path": _PATH}, required=("path",),
        description="Install one validated local LibreOffice .oxt package",
    ),
    "replace_text": _arguments_schema(
        _TEXT_REPLACEMENT_ARGUMENTS,
        description="Supply exactly one of text or value; optional formatting is applied and verified",
    ),
    "set_text": _arguments_schema(
        _TEXT_REPLACEMENT_ARGUMENTS,
        description="Supply exactly one of text or value; optional formatting is applied and verified",
    ),
    "delete_text": _arguments_schema(),
    "set_paragraph_properties": _arguments_schema({
        "properties": {
            **_uno_property_bag(_WRITER_PARAGRAPH_PROPERTY_NAMES),
            "description": "Whitelisted raw UNO paragraph properties",
        },
        "alignment": {
            "type": "string", "enum": ["left", "right", "center", "justify"],
        },
    }),
    "set_character_properties": _arguments_schema(
        {"properties": _uno_property_bag(
            _WRITER_CHARACTER_PROPERTY_NAMES | _WRITER_PARAGRAPH_PROPERTY_NAMES
        )},
        required=("properties",),
    ),
    "set_style_properties": _arguments_schema(
        {"properties": _uno_property_bag(
            _WRITER_CHARACTER_PROPERTY_NAMES | _WRITER_PARAGRAPH_PROPERTY_NAMES
        )},
        required=("properties",),
    ),
    "set_table_cell": _arguments_schema({
        "cell": _STRING,
        "text": {"type": "string", "description": "Replacement cell text (preferred)"},
        "value": {"type": "string", "description": "Deprecated alias for text"},
        "properties": _uno_property_bag(
            _WRITER_CHARACTER_PROPERTY_NAMES
            | _WRITER_PARAGRAPH_PROPERTY_NAMES
            | {"BackColor", "BackTransparent", "VertOrient"}
        ),
        "font_size": {"type": "number", "description": "Font size in points"},
        "font_color": {"description": "Text color as #RRGGBB or integer RGB"},
        "character_color": {"description": "Alias for font_color"},
        "background_color": {"description": "Cell background as #RRGGBB or integer RGB"},
        "paragraph_alignment": {
            "type": "string", "enum": ["left", "right", "center", "justify"],
        },
    }, required=("cell",)),
    "set_value": _arguments_schema({"value": _NUMBER}, required=("value",)),
    "set_formula": _arguments_schema({"formula": _STRING}, required=("formula",)),
    "set_cell_properties": _arguments_schema(
        {"properties": _uno_property_bag(_CALC_PROPERTY_NAMES)}, required=("properties",),
    ),
    "set_range_properties": _arguments_schema(
        {"properties": _uno_property_bag(_CALC_PROPERTY_NAMES)}, required=("properties",),
    ),
    "set_range_values": _arguments_schema(
        {"values": {"type": "array", "items": {"type": "array"}}}, required=("values",)
    ),
    "set_range_formulas": _arguments_schema(
        {"formulas": {"type": "array", "items": {"type": "array"}}}, required=("formulas",)
    ),
    "fill": _arguments_schema({
        "direction": {"type": "string", "enum": ["down", "right"]},
        "source_count": _INTEGER,
    }),
    "rename_sheet": _arguments_schema({"name": _STRING}, required=("name",)),
    "reorder_sheet": _arguments_schema({"index": _INTEGER}, required=("index",)),
    "insert_rows": _arguments_schema({"index": _INTEGER, "count": _INTEGER}),
    "insert_columns": _arguments_schema({"index": _INTEGER, "count": _INTEGER}),
    "delete_rows": _arguments_schema({"index": _INTEGER, "count": _INTEGER}),
    "delete_columns": _arguments_schema({"index": _INTEGER, "count": _INTEGER}),
    "add_sheet": _arguments_schema({"name": _STRING, "index": _INTEGER}, required=("name",)),
    "delete_sheet": _arguments_schema({"name": _STRING}),
    "freeze": _arguments_schema({"columns": _INTEGER, "rows": _INTEGER}),
    "unfreeze": _arguments_schema(),
    "create_chart": _arguments_schema({
        "sheet": _STRING, "range": _STRING, "name": _STRING,
        "x": _INTEGER, "y": _INTEGER, "width": _INTEGER, "height": _INTEGER,
        "column_header": _BOOLEAN, "row_header": _BOOLEAN,
    }),
    "delete_chart": _arguments_schema({"sheet": _STRING, "name": _STRING}, required=("sheet",)),
    "create_slide": _arguments_schema({"index": _INTEGER}),
    "delete_slide": _arguments_schema(),
    "set_slide_properties": _arguments_schema(_DRAW_UPDATE_ARGUMENTS),
    "add_text_shape": _arguments_schema({
        "text": _STRING, "x": _INTEGER, "y": _INTEGER,
        "width": _INTEGER, "height": _INTEGER,
    }),
    "set_shape_properties": _arguments_schema(_DRAW_UPDATE_ARGUMENTS),
    "delete_shape": _arguments_schema(),
}

_DIRECT_ATSPI_ACTION = _arguments_schema({"advertised_action": _STRING})
_ATSPI_ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "invoke": _DIRECT_ATSPI_ACTION,
    "focus": _arguments_schema(),
    "set_text": _arguments_schema({"value": _STRING}),
    "insert_text": _arguments_schema({"value": _STRING, "position": _INTEGER}),
    "replace_text": _arguments_schema({"value": _STRING}),
    "set_value": _arguments_schema({"value": _NUMBER}, required=("value",)),
    "select": _arguments_schema({"index": _INTEGER}),
    "clear_selection": _arguments_schema(),
    "toggle": _DIRECT_ATSPI_ACTION,
    "check": _DIRECT_ATSPI_ACTION,
    "uncheck": _DIRECT_ATSPI_ACTION,
    "expand": _DIRECT_ATSPI_ACTION,
    "collapse": _DIRECT_ATSPI_ACTION,
    "scroll_into_view": _arguments_schema(),
    "dismiss": _DIRECT_ATSPI_ACTION,
    "activate_window": _DIRECT_ATSPI_ACTION,
    "close_window": _DIRECT_ATSPI_ACTION,
    "choose_path": _arguments_schema({"path": _PATH}, required=("path",)),
}

_FILESYSTEM_ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "create_directory": _arguments_schema(
        {"path": _PATH, "parents": _BOOLEAN}, required=("path",)
    ),
    "copy": _arguments_schema(
        {"source": _PATH, "destination": _PATH}, required=("source", "destination")
    ),
    "move": _arguments_schema(
        {"source": _PATH, "destination": _PATH}, required=("source", "destination")
    ),
    "rename": _arguments_schema(
        {"source": _PATH, "destination": _PATH}, required=("source", "destination")
    ),
    "write_text": _arguments_schema(
        {"path": _PATH, "content": _STRING, "expected_hash": _STRING},
        required=("path",),
    ),
    "write_base64_atomic": _arguments_schema(
        {"path": _PATH, "base64": _STRING, "expected_hash": _STRING},
        required=("path", "base64"),
    ),
    "extract_archive": _arguments_schema(
        {"source": _PATH, "destination": _PATH, "expected_hash": _STRING},
        required=("source", "destination"),
    ),
    "create_desktop_entry": _arguments_schema(
        {
            "name": _STRING, "url": _STRING, "profile": _STRING,
            "expected_hash": _STRING,
        },
        required=("name", "url"),
    ),
}

_OS_ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "launch": _arguments_schema({"desktop_id": _STRING}, required=("desktop_id",)),
    "set_setting": _arguments_schema(
        {"schema": _STRING, "key": _STRING, "value": _STRING},
        required=("schema", "key", "value"),
    ),
    "write_clipboard": _arguments_schema({"text": _STRING}, required=("text",)),
    "set_audio_volume": _arguments_schema({"percent": _NUMBER}, required=("percent",)),
    "set_audio_muted": _arguments_schema({"muted": _BOOLEAN}, required=("muted",)),
    "set_wallpaper": _arguments_schema({"path": _PATH}, required=("path",)),
    "install_package": _arguments_schema(
        {"name": _STRING}, required=("name",),
        description="Install one explicitly named operating-system package",
    ),
}


CAPABILITIES: list[dict[str, Any]] = [
    {
        "adapter_id": "universal-atspi@1",
        "resources": [
            "ui.elements", "ui.surfaces", "os.applications", "os.windows",
            "os.dialogs", "os.file_choosers",
        ],
        "actions": [
            "invoke", "focus", "set_text", "insert_text", "replace_text", "set_value",
            "select", "clear_selection", "toggle", "check", "uncheck", "expand", "collapse",
            "scroll_into_view", "dismiss", "activate_window", "close_window", "choose_path",
        ],
        "resource_actions": {
            "ui.elements": [
                "invoke", "focus", "set_text", "insert_text", "replace_text",
                "set_value", "select", "clear_selection", "toggle", "check",
                "uncheck", "expand", "collapse", "scroll_into_view", "dismiss",
            ],
            "ui.surfaces": ["activate_window", "dismiss", "close_window"],
            "os.applications": ["activate_window"],
            "os.windows": ["activate_window", "close_window"],
            "os.dialogs": ["dismiss"],
            "os.file_choosers": ["choose_path", "dismiss"],
        },
        "execution_paths": ["accessibility"],
        "resource_schemas": {
            "ui.elements": {
                "type": "object",
                "properties": {
                    "active_surface_only": {"type": "boolean"},
                    "max_records": {
                        "type": "integer", "minimum": 1, "maximum": MAX_RECORDS,
                    },
                },
                "additionalProperties": False,
            },
            "ui.surfaces": {"type": "object", "additionalProperties": False},
        },
        "action_schemas": _ATSPI_ACTION_SCHEMAS,
    },
    {
        "adapter_id": "guest-filesystem@1",
        "resources": [
            "filesystem.entries", "filesystem.file", "filesystem.metadata",
            "artifact.structure", "artifact.owners", "artifact.sync",
            "artifact.exports", "artifact.downloads",
        ],
        "actions": [
            "create_directory", "copy", "move", "rename", "write_text",
            "write_base64_atomic", "extract_archive", "create_desktop_entry",
        ],
        "resource_actions": {
            "filesystem.entries": [
                "create_directory", "copy", "move", "rename", "extract_archive",
                "create_desktop_entry",
            ],
            "filesystem.file": [
                "copy", "move", "rename", "write_text", "write_base64_atomic",
            ],
            "filesystem.metadata": ["copy", "move", "rename"],
            "artifact.structure": ["extract_archive"],
            "artifact.owners": [],
            "artifact.sync": [],
            "artifact.exports": [],
            "artifact.downloads": [],
        },
        "execution_paths": ["native_api"],
        "resource_schemas": {
            "filesystem.file": {"parameters": {"offset": "optional integer", "length": "optional integer"}},
        },
        "action_schemas": _FILESYSTEM_ACTION_SCHEMAS,
        "risk_classes": {"extract_archive": "persistent"},
    },
    {
        "adapter_id": "guest-os@1",
        "resources": [
            "os.processes", "os.settings", "os.clipboard", "os.notifications",
            "os.desktop_entries", "os.network_state", "os.audio_state",
            "os.display_state", "os.power_state", "os.session_state", "os.packages",
        ],
        "actions": [
            "launch", "set_setting", "write_clipboard",
            "set_audio_volume", "set_audio_muted", "set_wallpaper", "install_package",
        ],
        "resource_actions": {
            "os.processes": ["launch"],
            "os.settings": ["set_setting", "set_wallpaper"],
            "os.clipboard": ["write_clipboard"],
            "os.notifications": [],
            "os.desktop_entries": ["launch"],
            "os.network_state": [],
            "os.audio_state": ["set_audio_volume", "set_audio_muted"],
            "os.display_state": [],
            "os.power_state": [],
            "os.session_state": [],
            "os.packages": ["install_package"],
        },
        "execution_paths": ["native_api"],
        "resource_schemas": {
            "os.settings": {"parameters": {"schema": "optional string", "key": "optional string"}},
            "os.packages": {"parameters": {"name": "optional validated package name"}},
        },
        "action_schemas": _OS_ACTION_SCHEMAS,
        "risk_classes": {
            "set_setting": "persistent", "write_clipboard": "persistent",
            "set_wallpaper": "persistent", "install_package": "persistent",
            "launch": "reversible",
        },
        "idempotent_actions": [
            "set_setting", "write_clipboard", "set_audio_volume", "set_audio_muted", "set_wallpaper"
        ],
    },
    {
        "adapter_id": "libreoffice.uno@1",
        "supported_versions": ["7.x"],
        "accepts_entity_target": True,
        "resources": [
            "libreoffice.extensions",
            "document.sessions", "document.state", "document.selection",
            "document.metadata", "document.save_state", "document.exports",
            "writer.paragraphs", "writer.runs", "writer.headings", "writer.tables",
            "writer.lists", "writer.sections", "writer.headers_footers",
            "writer.hyperlinks", "writer.bookmarks", "writer.comments",
            "writer.styles", "writer.page_config",
            "spreadsheet.sheets", "spreadsheet.cells", "spreadsheet.ranges",
            "spreadsheet.formulas", "spreadsheet.styles", "spreadsheet.rows",
            "spreadsheet.columns", "spreadsheet.named_ranges", "spreadsheet.filters",
            "spreadsheet.frozen_panes", "spreadsheet.charts", "spreadsheet.selection",
            "presentation.slides", "presentation.shapes", "presentation.notes",
            "presentation.styles",
        ],
        "actions": [
            "install_extension",
            "activate", "save", "save_as", "export", "undo", "redo", "reload",
            "insert_text", "replace_text", "replace_with_paragraphs", "insert_paragraphs",
            "delete_text",
            "set_paragraph_properties",
            "set_character_properties", "set_table_cell", "set_style_properties",
            "set_value", "set_formula", "set_text", "set_cell_properties",
            "set_range_values", "set_range_formulas", "fill", "set_range_properties",
            "rename_sheet", "reorder_sheet", "insert_rows", "insert_columns",
            "delete_rows", "delete_columns", "add_sheet", "delete_sheet",
            "freeze", "unfreeze", "create_chart", "delete_chart",
            "create_slide", "delete_slide", "set_slide_properties", "add_text_shape",
            "set_shape_properties", "delete_shape",
        ],
        "resource_actions": {
            "libreoffice.extensions": ["install_extension"],
            "document.sessions": ["activate"],
            "document.state": ["save", "save_as", "export", "undo", "redo", "reload"],
            "document.selection": [],
            "document.metadata": [],
            "document.save_state": ["save", "save_as"],
            "document.exports": ["export"],
            "writer.paragraphs": [
                "insert_text", "replace_text", "replace_with_paragraphs",
                "insert_paragraphs", "delete_text", "set_paragraph_properties",
            ],
            "writer.runs": [
                "insert_text", "replace_text", "delete_text", "set_character_properties",
            ],
            "writer.headings": ["set_paragraph_properties"],
            "writer.tables": ["set_table_cell"],
            "writer.lists": ["set_paragraph_properties"],
            "writer.sections": [],
            "writer.headers_footers": ["insert_text", "replace_text", "delete_text"],
            "writer.hyperlinks": [],
            "writer.bookmarks": [],
            "writer.comments": [],
            "writer.styles": ["set_style_properties"],
            "writer.page_config": ["set_style_properties"],
            "spreadsheet.sheets": [
                "rename_sheet", "reorder_sheet", "add_sheet", "delete_sheet",
                "insert_rows", "delete_rows", "insert_columns", "delete_columns",
            ],
            "spreadsheet.cells": [
                "set_value", "set_formula", "set_text", "set_cell_properties",
            ],
            "spreadsheet.ranges": [
                "set_range_values", "set_range_formulas", "fill", "set_range_properties",
            ],
            "spreadsheet.formulas": ["set_formula", "set_range_formulas"],
            "spreadsheet.styles": ["set_cell_properties", "set_range_properties"],
            "spreadsheet.rows": ["insert_rows", "delete_rows"],
            "spreadsheet.columns": ["insert_columns", "delete_columns"],
            "spreadsheet.named_ranges": [],
            "spreadsheet.filters": [],
            "spreadsheet.frozen_panes": ["freeze", "unfreeze"],
            "spreadsheet.charts": ["create_chart", "delete_chart"],
            "spreadsheet.selection": [],
            "presentation.slides": [
                "create_slide", "delete_slide", "set_slide_properties", "add_text_shape",
            ],
            "presentation.shapes": [
                "replace_text", "set_shape_properties", "delete_shape",
            ],
            "presentation.notes": ["add_text_shape"],
            "presentation.styles": ["set_style_properties"],
        },
        "execution_paths": ["native_api"],
        "resource_schemas": {
            "libreoffice.extensions": {
                "parameters": {"identifier": "optional validated extension identifier"}
            },
            "spreadsheet.cells": {
                "parameters": {"sheet": "optional string", "range": "A1 address/range"}
            },
            "spreadsheet.ranges": {
                "parameters": {"sheet": "optional string", "range": "required A1 range"}
            },
        },
        "action_schemas": _LIBREOFFICE_ACTION_SCHEMAS,
        "risk_classes": {
            "install_extension": "persistent",
            "save": "persistent", "save_as": "persistent", "export": "persistent",
            "insert_paragraphs": "reversible",
            "set_value": "reversible", "set_formula": "reversible",
        },
        "idempotent_actions": [
            "save", "save_as", "set_value", "set_formula", "set_text",
            "set_cell_properties", "set_range_values", "set_range_formulas",
        ],
        "known_representation_gaps": [
            {
                "capability": "presentation.canvas_visual_similarity",
                "reason": "strict semantic runtime exposes object structure, not rendered visual judgment",
            }
        ],
    },
]


def _is_file_chooser_record(
    record: dict[str, Any], *, require_visible: bool
) -> bool:
    state = record.get("state") or {}
    if require_visible and not bool(state.get("visible", False)):
        return False
    role = str(record.get("role") or "").casefold()
    if role in {"file chooser", "file chooser dialog"}:
        return True
    name = " ".join(str(record.get("name") or "").casefold().split())
    return (
        role == "dialog"
        and name.startswith(("open", "save", "select", "choose"))
        and any(value in name for value in ("file", "folder", "directory"))
    )


_FILE_CHOOSER_MODES = frozenset({"open", "select", "choose", "save"})


def _file_chooser_mode(record: dict[str, Any]) -> str | None:
    """Classify a chooser only from its own visible top-level identity.

    AT-SPI does not expose the GtkFileChooser action enum, but native chooser
    titles conventionally state the operation (for example ``Open File`` or
    ``Save As``).  Treat that title as authoritative UI state while remaining
    deliberately conservative: conflicting operation words or a title whose
    first word is not an operation leave the mode unknown.
    """

    candidates = (record.get("name"), record.get("title"))
    detected: set[str] = set()
    for candidate in candidates:
        normalized = " ".join(_text(candidate, 500).casefold().split())
        if not normalized:
            continue
        words = re.findall(r"[a-z]+", normalized)
        operation_words = {word for word in words if word in _FILE_CHOOSER_MODES}
        if len(operation_words) > 1:
            return None
        if words and words[0] in operation_words:
            detected.add(words[0])
    if len(detected) == 1:
        return next(iter(detected))
    return None


_TOP_LEVEL_SURFACE_ROLES = frozenset({
    "frame", "window", "dialog", "alert", "file chooser", "file chooser dialog",
})


def _private_wm_command(argv: list[str]) -> tuple[int, str]:
    """Run one fixed window-manager command and keep all native data private."""

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env={
                key: value for key, value in os.environ.items()
                if key in {
                    "PATH", "HOME", "DISPLAY", "XAUTHORITY",
                    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "LANG",
                }
            },
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return 127, ""
    return int(completed.returncode), completed.stdout[:16 * 1024]


def _wm_activate_accessible(target: Any) -> bool:
    """Activate the unique native window corresponding to an accessible.

    This is the server-private semantic-input fallback for window surfaces.
    Candidate identity is established from the accessible's exact title and
    process id; ambiguity is never resolved by choosing the first match.
    """

    title = _text(_safe_attr(target, "name", ""), 2_000).strip()
    pid = _accessible_process_id(target)
    searches: list[list[str]] = []
    application_hint = ""
    if title:
        searches.append([
            "xdotool", "search", "--onlyvisible", "--name",
            f"^{re.escape(title)}$",
        ])
        title_parts = re.split(r"\s+[\-–—]\s+", title)
        application_hint = title_parts[-1].strip() if len(title_parts) > 1 else ""
        if application_hint and application_hint.casefold() != title.casefold():
            searches.append([
                "xdotool", "search", "--onlyvisible", "--name",
                re.escape(application_hint),
            ])
    if pid is not None:
        searches.append([
            "xdotool", "search", "--onlyvisible", "--pid", str(pid),
        ])
    for command in searches:
        status, output = _private_wm_command(command)
        if status not in {0, 1}:
            continue
        candidates = {
            line.strip() for line in output.splitlines()
            if line.strip().isdigit() and int(line.strip()) > 0
        }
        # Search routes are ordered strongest-first: an exact visible title,
        # then a unique process-owned visible window.  A later multi-process
        # Chrome search must not invalidate an already unique exact-title
        # identity.
        if len(candidates) == 1:
            window_id = next(iter(candidates))
            activate_status, _activate_output = _private_wm_command([
                "xdotool", "windowactivate", "--sync", window_id,
            ])
            return activate_status == 0
        if len(candidates) > 1:
            continue
    # GNOME exposes some application windows to wmctrl but not to xdotool's
    # search index.  Resolve an exact visible title (or a unique app-title
    # suffix) from wmctrl's inventory, then activate by the private XID.
    list_status, list_output = _private_wm_command(["wmctrl", "-lp"])
    if list_status == 0:
        windows: list[tuple[str, str]] = []
        for line in list_output.splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
                windows.append((fields[0], fields[4]))
        normalized_title = _normalized_window_title(title)
        exact = [
            window_id for window_id, window_title in windows
            if normalized_title
            and _normalized_window_title(window_title) == normalized_title
        ]
        candidates = exact
        if not candidates and application_hint:
            normalized_hint = _normalized_window_title(application_hint)
            candidates = [
                window_id for window_id, window_title in windows
                if normalized_hint in _normalized_window_title(window_title)
            ]
        if len(candidates) == 1:
            activate_status, _activate_output = _private_wm_command([
                "wmctrl", "-i", "-a", candidates[0],
            ])
            return activate_status == 0
    return False


def _xprop_string(output: str, property_name: str) -> str:
    for line in output.splitlines():
        if not line.startswith(property_name):
            continue
        raw = line.partition("=")[2].strip()
        quoted = re.findall(r'"((?:\\.|[^"\\])*)"', raw)
        value = quoted[-1] if quoted else raw
        return value.replace(r'\"', '"')
    return ""


def _wm_active_window() -> dict[str, Any] | None:
    """Return private EWMH identity for the active window, never geometry."""

    status, root = _private_wm_command(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    xid_match = re.search(r"\b0x[0-9a-fA-F]+\b", root) if status == 0 else None
    if xid_match is not None and int(xid_match.group(0), 16) != 0:
        xid = xid_match.group(0)
        detail_status, detail = _private_wm_command([
            "xprop", "-id", xid, "_NET_WM_PID", "_NET_WM_NAME", "WM_NAME", "WM_CLASS",
        ])
        if detail_status == 0:
            pid_match = re.search(r"^_NET_WM_PID[^=]*=\s*(\d+)", detail, re.MULTILINE)
            title = _xprop_string(detail, "_NET_WM_NAME") or _xprop_string(detail, "WM_NAME")
            class_name = _xprop_string(detail, "WM_CLASS")
            pid = int(pid_match.group(1)) if pid_match else None
            if pid or title:
                return {"pid": pid, "title": title, "class_name": class_name}

    status, xid_text = _private_wm_command(["xdotool", "getactivewindow"])
    if status != 0 or not xid_text.strip().isdigit():
        return None
    xid = xid_text.strip()
    pid_status, pid_text = _private_wm_command(["xdotool", "getwindowpid", xid])
    title_status, title = _private_wm_command(["xdotool", "getwindowname", xid])
    class_status, class_name = _private_wm_command([
        "xdotool", "getwindowclassname", xid,
    ])
    pid = int(pid_text.strip()) if pid_status == 0 and pid_text.strip().isdigit() else None
    title = title.strip() if title_status == 0 else ""
    class_name = class_name.strip() if class_status == 0 else ""
    return {"pid": pid, "title": title, "class_name": class_name} if pid or title else None


def _normalized_window_title(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_app_identity(value: Any) -> str:
    """Normalize a private WM class/app name for exact identity matching.

    EWMH ``WM_CLASS`` commonly spells the same application differently from
    AT-SPI (for example ``google-chrome`` versus ``Google Chrome``).  Removing
    punctuation lets us use that authoritative identity without exposing it
    or resorting to fuzzy title guessing.
    """

    return "".join(
        character
        for character in _normalized_window_title(value)
        if character.isalnum()
    )


def _surface_context_names(
    record: dict[str, Any], by_ref: dict[str, dict[str, Any]],
) -> list[str]:
    names: list[str] = []
    current: dict[str, Any] | None = record
    seen: set[str] = set()
    while current is not None:
        name = _normalized_window_title(current.get("name") or current.get("text"))
        if name:
            names.append(name)
        parent_ref = current.get("parent_ref")
        if not isinstance(parent_ref, str) or parent_ref in seen:
            break
        seen.add(parent_ref)
        current = by_ref.get(parent_ref)
    return names


def _is_shell_surface(
    record: dict[str, Any], by_ref: dict[str, dict[str, Any]],
) -> bool:
    return any(
        name == "gnome shell" or "gnome-shell" in name
        for name in _surface_context_names(record, by_ref)
    )


def _wm_surface_match(
    candidates: list[dict[str, Any]], wm: dict[str, Any], *,
    by_ref: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    try:
        wm_pid = int(wm.get("pid")) if wm.get("pid") is not None else None
    except (TypeError, ValueError):
        wm_pid = None
    wm_title = _normalized_window_title(wm.get("title"))
    wm_class = _normalized_app_identity(wm.get("class_name"))
    by_ref = by_ref or {
        str(record["ref"]): record
        for record in candidates
        if isinstance(record.get("ref"), str)
    }
    identities: list[tuple[dict[str, Any], int | None, str]] = []
    for record in candidates:
        ref = record.get("ref")
        node = STATE.refs.get(ref) if isinstance(ref, str) else None
        identities.append((
            record,
            _accessible_process_id(node) if node is not None else None,
            _normalized_window_title(record.get("name") or record.get("text")),
        ))

    if wm_pid is not None:
        pid_matches = [item for item in identities if item[1] == wm_pid]
        if wm_title:
            exact = [item[0] for item in pid_matches if item[2] == wm_title]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                return None
        if len(pid_matches) == 1:
            return pid_matches[0][0]
        if len(pid_matches) > 1:
            return None
    if wm_title:
        title_matches = [item[0] for item in identities if item[2] == wm_title]
        if len(title_matches) == 1:
            return title_matches[0]
    if wm_class:
        class_matches = [
            record
            for record in candidates
            if any(
                _normalized_app_identity(name) == wm_class
                for name in _surface_context_names(record, by_ref)
            )
        ]
        if len(class_matches) == 1:
            return class_matches[0]
    return None


def _reconcile_surface_activity(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    """Prefer a unique WM match while keeping its native identity private."""

    by_ref = {
        str(record["ref"]): record
        for record in records if isinstance(record.get("ref"), str)
    }
    candidates = [
        record for record in records
        if str(record.get("role") or "").casefold() in _TOP_LEVEL_SURFACE_ROLES
        and (record.get("state") or {}).get("visible") is not False
        and (
            (record.get("state") or {}).get("showing") is not False
            or (record.get("state") or {}).get("modal") is True
        )
    ]
    real_candidates = [
        record for record in candidates if not _is_shell_surface(record, by_ref)
    ]
    wm = _wm_active_window()
    matched = (
        _wm_surface_match(candidates, wm, by_ref=by_ref)
        if wm is not None else None
    )
    changed = False

    if wm is not None:
        # The WM is authoritative even when its native window cannot be mapped
        # uniquely. Clear stale AT-SPI activity instead of guessing a target.
        for candidate in candidates:
            state = dict(candidate.get("state") or {})
            active = candidate is matched
            if state.get("active") is not active or state.get("focused") is not active:
                state["active"] = active
                state["focused"] = active
                candidate["state"] = state
                changed = True
    else:
        modal_active = [
            record for record in real_candidates
            if (record.get("state") or {}).get("modal") is True
            and (
                (record.get("state") or {}).get("active") is True
                or (record.get("state") or {}).get("focused") is True
            )
        ]
        active_real = [
            record for record in real_candidates
            if (record.get("state") or {}).get("active") is True
            or (record.get("state") or {}).get("focused") is True
        ]
        if len(modal_active) == 1:
            matched = modal_active[0]
        elif len(active_real) == 1:
            matched = active_real[0]
        elif len(real_candidates) == 1:
            matched = real_candidates[0]

    matched_is_shell = matched is not None and _is_shell_surface(matched, by_ref)
    if (
        (real_candidates and wm is None)
        or (matched is not None and not matched_is_shell)
    ):
        shell_refs = {
            str(record["ref"])
            for record in records
            if isinstance(record.get("ref"), str)
            and _is_shell_surface(record, by_ref)
        }
        if shell_refs:
            records = [record for record in records if record.get("ref") not in shell_refs]
            changed = True
            for record in records:
                child_refs = record.get("child_refs")
                if isinstance(child_refs, list):
                    retained = [ref for ref in child_refs if ref not in shell_refs]
                    if retained != child_refs:
                        record["child_refs"] = retained

    return records, matched, changed


def _authoritative_active_surface_ref() -> tuple[bool, str | None]:
    """Return whether WM authority exists and its uniquely matched surface.

    AT-SPI ACTIVE/FOCUSED flags can remain true on a background window after
    the window manager has activated another application.  Surface reads
    already reconcile that disagreement through EWMH; actions must use the
    same authority or they can incorrectly reject a requested switch as
    ``no_effect``.  A present but unmappable WM identity is deliberately
    distinct from an unavailable WM probe: callers must not trust stale AT-SPI
    activity in the former case.
    """

    wm = _wm_active_window()
    if wm is None:
        return False, None
    try:
        records, _revision = _walk_accessibility(max_depth=2, lightweight=True)
    except Exception:
        return True, None
    by_ref = {
        str(record["ref"]): record
        for record in records if isinstance(record.get("ref"), str)
    }
    candidates = [
        record for record in records
        if str(record.get("role") or "").casefold() in _TOP_LEVEL_SURFACE_ROLES
        and (record.get("state") or {}).get("visible") is not False
        and (
            (record.get("state") or {}).get("showing") is not False
            or (record.get("state") or {}).get("modal") is True
        )
    ]
    matched = _wm_surface_match(candidates, wm, by_ref=by_ref)
    matched_ref = matched.get("ref") if matched is not None else None
    return True, str(matched_ref) if isinstance(matched_ref, str) else None


def _surface_filter(resource: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if resource == "ui.elements":
        return records
    if resource == "ui.surfaces":
        return [
            record for record in records
            if record.get("role", "").casefold()
            in {"application", *_TOP_LEVEL_SURFACE_ROLES}
        ]
    if resource == "os.file_choosers":
        choosers: list[dict[str, Any]] = []
        for record in records:
            if not _is_file_chooser_record(record, require_visible=True):
                continue
            chooser = dict(record)
            chooser.pop("mode", None)
            mode = _file_chooser_mode(chooser)
            if mode is not None:
                chooser["mode"] = mode
            choosers.append(chooser)
        return choosers
    roles = {
        "os.applications": {"application"},
        "os.windows": {"frame", "window"},
        "os.dialogs": {"dialog", "alert"},
    }.get(resource)
    if roles is None:
        return records
    return [record for record in records if record.get("role", "").casefold() in roles]


def _query_accessibility(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    def build() -> tuple[list[dict[str, Any]], str]:
        # Applications, top-level windows and dialogs live at the shallow
        # AT-SPI surface hierarchy. Walking every control below a 2,000-node
        # office or mail window made even `system.surfaces` exceed the guest's
        # 30-second transport deadline despite returning only a dozen records.
        # Full-depth traversal remains available for `ui.elements`.
        parameters = payload.get("parameters") or {}
        active_surface_only = (
            resource == "ui.elements"
            and isinstance(parameters, dict)
            and parameters.get("active_surface_only") is True
        )
        requested_max_records = parameters.get("max_records", MAX_RECORDS)
        if (
            isinstance(requested_max_records, bool)
            or not isinstance(requested_max_records, int)
            or not 1 <= requested_max_records <= MAX_RECORDS
        ):
            raise AgentError(
                "invalid_request", f"max_records must be an integer from 1 to {MAX_RECORDS}",
            )
        surface_resource = resource != "ui.elements" or active_surface_only
        records, revision = _walk_accessibility(
            max_depth=2 if surface_resource else 32,
            lightweight=surface_resource,
        )
        preferred_surface: dict[str, Any] | None = None
        if resource == "ui.surfaces" or active_surface_only:
            records, preferred_surface, reconciled = _reconcile_surface_activity(records)
            if reconciled:
                revision = STATE.revision(records)
        if active_surface_only:
            if preferred_surface is None:
                # Preserve the complete shallow state rather than guessing one
                # of several unfocused windows as the active interaction root.
                return [], revision
            target = _resolve(preferred_surface["ref"])
            active_walk_arguments: dict[str, Any] = {
                "max_depth": 32,
                "roots": [target],
            }
            if "max_records" in parameters:
                active_walk_arguments["max_records"] = requested_max_records
            records, revision = _walk_accessibility(**active_walk_arguments)
        if resource == "system.surfaces":
            surfaces = _surface_filter("os.applications", records)
            surfaces.extend(_surface_filter("os.windows", records))
            surfaces.extend(_surface_filter("os.dialogs", records))
            return surfaces, revision
        filtered = _surface_filter(resource, records)
        return [
            record
            for record in filtered
            if _where_match(record, payload.get("where"))
        ], revision

    return _private_snapshot_page(resource, payload, build)


def _private_page(
    records: list[dict[str, Any]], payload: dict[str, Any], revision: str
) -> dict[str, Any]:
    """Page guest-internal snapshots without exposing this cursor to Qwen.

    The outer kernel fetches these pages, interns opaque public refs, and then
    applies the canonical filter/order/projection/cursor contract itself.
    """
    raw_offset = payload.get("internal_offset", 0)
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
        raise AgentError("invalid_request", "internal_offset is invalid")
    limit = min(max(int(payload.get("limit", 100)), 1), 100)
    end = min(raw_offset + limit, len(records))
    return {
        "records": records[raw_offset:end], "revision": revision,
        "truncated": end < len(records), "total": len(records),
        "next_internal_offset": end if end < len(records) else None,
    }


_PRIVATE_SNAPSHOT_TTL_SECONDS = 30.0
_PRIVATE_SNAPSHOT_LIMIT = 8


def _private_snapshot_key(resource: str, payload: dict[str, Any]) -> str:
    """Identify one adapter query independently of its private page offset."""

    identity = {
        "resource": resource,
        "scope": payload.get("scope") or {},
        "parameters": payload.get("parameters") or {},
        # GuestProxyAdapter clears these today, but retaining them in the key
        # keeps this helper correct for direct clients and future pushdown.
        "where": payload.get("where") or {},
        "fields": payload.get("fields") or [],
        "order_by": payload.get("order_by") or [],
        "freshness": payload.get("freshness") or "live",
    }
    return _json_hash(identity)


def _private_snapshot_page(
    resource: str,
    payload: dict[str, Any],
    build: Callable[[], tuple[list[dict[str, Any]], str]],
) -> dict[str, Any]:
    """Page one immutable private snapshot, building it exactly once.

    The outer kernel requests successive private pages by increasing
    ``internal_offset``.  A continuation therefore has to reuse the exact
    records and revision produced for offset zero; querying the live app again
    would make one logical observation span multiple app states.
    """

    raw_offset = payload.get("internal_offset", 0)
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
        raise AgentError("invalid_request", "internal_offset is invalid")
    key = _private_snapshot_key(resource, payload)
    now = time.monotonic()
    with STATE.private_snapshot_lock:
        expired = [
            snapshot_key
            for snapshot_key, snapshot in STATE.private_snapshots.items()
            if now - float(snapshot["created_at"]) > _PRIVATE_SNAPSHOT_TTL_SECONDS
        ]
        for snapshot_key in expired:
            STATE.private_snapshots.pop(snapshot_key, None)

        if raw_offset == 0:
            records, revision = build()
            if not isinstance(records, list) or not isinstance(revision, str):
                raise AgentError("internal_error", "private snapshot builder returned invalid state")
            snapshot = {
                "records": records,
                "revision": revision,
                "created_at": time.monotonic(),
            }
            # One policy loop is sequential, but bound retained abandoned
            # snapshots in case a client times out before requesting page two.
            if len(STATE.private_snapshots) >= _PRIVATE_SNAPSHOT_LIMIT:
                oldest = min(
                    STATE.private_snapshots,
                    key=lambda snapshot_key: float(
                        STATE.private_snapshots[snapshot_key]["created_at"]
                    ),
                )
                STATE.private_snapshots.pop(oldest, None)
            STATE.private_snapshots[key] = snapshot
        else:
            snapshot = STATE.private_snapshots.get(key)
            if snapshot is None:
                raise AgentError(
                    "revision_conflict",
                    "private query continuation is missing or expired; restart at offset zero",
                    retryable=True,
                )
            records = snapshot["records"]
            revision = snapshot["revision"]

        page = _private_page(records, payload, revision)
        if page["next_internal_offset"] is None:
            STATE.private_snapshots.pop(key, None)
        return page


def _safe_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise AgentError("invalid_request", "path must be an absolute guest path")
    return Path(raw)


def _file_record(path: Path, *, include_hash: bool = False) -> dict[str, Any]:
    stat = path.lstat()
    record = {
        "ref": f"artifact_{hashlib.sha256(str(path).encode()).hexdigest()[:24]}",
        "kind": "filesystem.entry",
        "name": path.name,
        "path": str(path),
        "is_file": path.is_file(),
        "is_directory": path.is_dir(),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "mime_type": mimetypes.guess_type(str(path))[0],
        "source": "filesystem",
        "freshness": "live",
    }
    # Optimistic-concurrency writes require an expected_hash.  Returning the
    # current hash with the read makes that contract satisfiable without the
    # model inventing a value or using an out-of-band process route.
    if include_hash and path.is_file():
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return record


_ARTIFACT_MAX_BYTES = 100 * 1024 * 1024
_LIBREOFFICE_EXTENSION_MAX_BYTES = 300 * 1024 * 1024
_ARTIFACT_MAX_MEMBERS = 10_000
_OOXML_NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
}


def _safe_xml(data: bytes) -> ET.Element:
    if len(data) > _ARTIFACT_MAX_BYTES:
        raise AgentError("budget_exhausted", "artifact XML exceeds parse limit")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise AgentError("policy_violation", "artifact XML DTD/entities are forbidden")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise AgentError("invalid_request", "artifact XML is not parseable") from error


class _ArtifactHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root_tag: str | None = None
        self.element_count = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, _attrs: Any) -> None:
        if self.root_tag is None:
            self.root_tag = tag
        self.element_count += 1

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if sum(len(value) for value in self.text) < MAX_TEXT:
            stripped = data.strip()
            if stripped:
                self.text.append(stripped)


def _parse_html(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentError("invalid_request", "HTML artifact is not UTF-8") from error
    parser = _ArtifactHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as error:
        raise AgentError("invalid_request", "HTML artifact is not parseable") from error
    if parser.root_tag is None:
        raise AgentError("invalid_request", "HTML artifact has no elements")
    return {
        "root_tag": parser.root_tag,
        "text_excerpt": _text(" ".join(parser.text)),
        "element_count": parser.element_count,
    }


def _safe_archive(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise AgentError("invalid_request", "artifact package is not a valid ZIP") from error
    members = archive.infolist()
    if len(members) > _ARTIFACT_MAX_MEMBERS or sum(item.file_size for item in members) > _ARTIFACT_MAX_BYTES:
        archive.close()
        raise AgentError("budget_exhausted", "artifact package exceeds parse limits")
    for item in members:
        parts = Path(item.filename).parts
        if item.filename.startswith("/") or ".." in parts:
            archive.close()
            raise AgentError("policy_violation", "artifact package contains unsafe member path")
    return archive


def _archive_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return _safe_xml(archive.read(name))
    except KeyError as error:
        raise AgentError("invalid_request", f"artifact package is missing {name}") from error


def _parse_docx(path: Path) -> dict[str, Any]:
    with _safe_archive(path) as archive:
        root = _archive_xml(archive, "word/document.xml")
        paragraphs = []
        for index, paragraph in enumerate(root.findall(".//w:p", _OOXML_NAMESPACES)):
            runs = []
            # Displayed hyperlink/field text is nested below wrappers such as
            # w:hyperlink. Direct-child w:r traversal silently dropped that
            # text and made a freshly saved Writer document disagree with its
            # live UNO paragraph model.
            for run_index, run in enumerate(paragraph.findall(".//w:r", _OOXML_NAMESPACES)):
                properties = run.find("w:rPr", _OOXML_NAMESPACES)
                text = "".join(node.text or "" for node in run.findall(".//w:t", _OOXML_NAMESPACES))
                runs.append({
                    "index": run_index, "text": _text(text),
                    "bold": properties is not None and properties.find("w:b", _OOXML_NAMESPACES) is not None,
                    "italic": properties is not None and properties.find("w:i", _OOXML_NAMESPACES) is not None,
                })
            style = paragraph.find("w:pPr/w:pStyle", _OOXML_NAMESPACES)
            style_value = style.attrib.get(f"{{{_OOXML_NAMESPACES['w']}}}val") if style is not None else None
            paragraphs.append({
                "index": index, "text": _text("".join(run["text"] for run in runs)),
                "style": style_value, "runs": runs[:200],
            })
            if len(paragraphs) >= 3_000:
                break
        body = root.find("w:body", _OOXML_NAMESPACES)
        body_paragraphs = []
        if body is not None:
            for index, paragraph in enumerate(body.findall("w:p", _OOXML_NAMESPACES)):
                style = paragraph.find("w:pPr/w:pStyle", _OOXML_NAMESPACES)
                style_value = (
                    style.attrib.get(f"{{{_OOXML_NAMESPACES['w']}}}val")
                    if style is not None else None
                )
                body_paragraphs.append({
                    "index": index,
                    "text": _text("".join(
                        node.text or ""
                        for node in paragraph.findall(".//w:t", _OOXML_NAMESPACES)
                    )),
                    "style": style_value,
                })
                if len(body_paragraphs) >= 3_000:
                    break
        tables = []
        for table_index, table in enumerate(root.findall(".//w:tbl", _OOXML_NAMESPACES)):
            rows = []
            for row in table.findall("w:tr", _OOXML_NAMESPACES):
                cells = []
                for cell in row.findall("w:tc", _OOXML_NAMESPACES):
                    paragraph_text = [
                        "".join(
                            node.text or ""
                            for node in paragraph.findall(".//w:t", _OOXML_NAMESPACES)
                        )
                        for paragraph in cell.findall(".//w:p", _OOXML_NAMESPACES)
                    ]
                    cells.append(_text("\n".join(paragraph_text)))
                rows.append(cells)
            tables.append({"index": table_index, "rows": rows[:1_000]})
            if len(tables) >= 200:
                break
        return {
            "format": "docx", "paragraphs": paragraphs, "paragraph_count": len(paragraphs),
            "body_paragraphs": body_paragraphs,
            "headings": [item for item in paragraphs if str(item.get("style") or "").casefold().startswith("heading")],
            "tables": tables,
        }


def _parse_xlsx(path: Path) -> dict[str, Any]:
    with _safe_archive(path) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = _archive_xml(archive, "xl/sharedStrings.xml")
            shared = [
                "".join(node.text or "" for node in item.findall(".//x:t", _OOXML_NAMESPACES))
                for item in shared_root.findall("x:si", _OOXML_NAMESPACES)
            ]
        workbook = _archive_xml(archive, "xl/workbook.xml")
        declared_names = [
            node.attrib.get("name", "") for node in workbook.findall(".//x:sheets/x:sheet", _OOXML_NAMESPACES)
        ]
        worksheet_names = sorted(
            (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        sheets = []
        for sheet_index, member in enumerate(worksheet_names):
            root = _archive_xml(archive, member)
            cells = []
            for cell in root.findall(".//x:c", _OOXML_NAMESPACES):
                cell_type = cell.attrib.get("t")
                raw = cell.findtext("x:v", default=None, namespaces=_OOXML_NAMESPACES)
                formula = cell.findtext("x:f", default=None, namespaces=_OOXML_NAMESPACES)
                inline = "".join(cell.itertext()) if cell_type == "inlineStr" else None
                value: Any = inline if inline is not None else raw
                if cell_type == "s" and raw is not None:
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = raw
                elif cell_type == "b" and raw is not None:
                    value = raw == "1"
                elif cell_type in {None, "n"} and raw is not None:
                    try:
                        value = float(raw) if any(char in raw for char in ".eE") else int(raw)
                    except ValueError:
                        value = raw
                cells.append({
                    "address": cell.attrib.get("r"), "value": value, "formula": formula,
                    "style_id": int(cell.attrib.get("s", "0")), "data_type": cell_type or "number",
                })
                if len(cells) >= 5_000:
                    break
            sheets.append({
                "index": sheet_index, "name": declared_names[sheet_index] if sheet_index < len(declared_names) else f"Sheet{sheet_index + 1}",
                "cells": cells, "cell_count": len(cells),
            })
        return {"format": "xlsx", "sheets": sheets, "sheet_count": len(sheets)}


def _parse_pptx(path: Path) -> dict[str, Any]:
    with _safe_archive(path) as archive:
        members = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        slides = []
        for index, member in enumerate(members):
            root = _archive_xml(archive, member)
            text_nodes = [node.text or "" for node in root.findall(".//a:t", _OOXML_NAMESPACES)]
            shapes = []
            for shape_index, shape in enumerate(root.findall(".//p:sp", {
                **_OOXML_NAMESPACES,
                "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            })):
                shape_text = " ".join(node.text or "" for node in shape.findall(".//a:t", _OOXML_NAMESPACES))
                shapes.append({"index": shape_index, "text": _text(shape_text)})
            slides.append({
                "index": index, "text": _text(" ".join(text_nodes)), "shapes": shapes[:1_000],
                "shape_count": len(shapes),
            })
        return {"format": "pptx", "slides": slides, "slide_count": len(slides)}


def _parse_odf(path: Path) -> dict[str, Any]:
    with _safe_archive(path) as archive:
        root = _archive_xml(archive, "content.xml")
        paragraphs = [
            _text("".join(node.itertext()))
            for node in root.findall(".//text:p", _OOXML_NAMESPACES)[:3_000]
        ]
        headings = [
            _text("".join(node.itertext()))
            for node in root.findall(".//text:h", _OOXML_NAMESPACES)[:1_000]
        ]
        office_text = root.find(".//office:text", _OOXML_NAMESPACES)
        body_paragraphs = []
        if office_text is not None:
            body_paragraphs = [
                _text("".join(node.itertext()))
                for node in list(office_text)
                if _local_xml_name(str(node.tag)) in {"p", "h"}
            ][:3_000]
        tables = []
        for node in root.findall(".//table:table", _OOXML_NAMESPACES)[:200]:
            rows = []
            for row in node.findall("table:table-row", _OOXML_NAMESPACES)[:1_000]:
                rows.append([
                    _text("".join(cell.itertext()))
                    for cell in row.findall("table:table-cell", _OOXML_NAMESPACES)
                ])
            tables.append({"name": node.attrib.get(f"{{{_OOXML_NAMESPACES['table']}}}name", ""), "rows": rows})
        pages = [
            {"index": index, "name": node.attrib.get(f"{{{_OOXML_NAMESPACES['draw']}}}name", ""), "text": _text(" ".join(node.itertext()))}
            for index, node in enumerate(root.findall(".//draw:page", _OOXML_NAMESPACES)[:1_000])
        ]
        return {
            "format": path.suffix.casefold().lstrip("."), "paragraphs": paragraphs,
            "body_paragraphs": body_paragraphs,
            "headings": headings, "tables": tables, "pages": pages,
        }


def _parse_pdf(path: Path) -> dict[str, Any]:
    if path.stat().st_size < 5 or path.read_bytes()[:5] != b"%PDF-":
        raise AgentError("invalid_request", "PDF header is invalid")
    info = _bounded_command(["pdfinfo", str(path)])
    if info["exit_code"] != 0:
        raise AgentError("invalid_request", _text(info["stderr"]) or "PDF is not parseable")
    text_result = _bounded_command(["pdftotext", "-layout", str(path), "-"])
    metadata = {}
    for line in info["stdout"].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip().casefold().replace(" ", "_")] = value.strip()
    return {
        "format": "pdf", "metadata": metadata, "page_count": int(metadata.get("pages", "0") or 0),
        "text_excerpt": _text(text_result["stdout"]), "parseable": True,
    }


def _image_dimensions(path: Path) -> dict[str, Any] | None:
    data = path.read_bytes()[:64 * 1024]
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"format": "png", "width": width, "height": height}
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return {"format": "gif", "width": width, "height": height}
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[offset + 5:offset + 7], "big")
                width = int.from_bytes(data[offset + 7:offset + 9], "big")
                return {"format": "jpeg", "width": width, "height": height}
            if length < 2:
                break
            offset += 2 + length
    return None


def _parse_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AgentError("invalid_request", "artifact.structure requires a regular file")
    suffix = path.suffix.casefold()
    if suffix == ".oxt":
        # LibreOffice dictionaries and grammar extensions legitimately exceed
        # the general document parse limit. They receive a separate bounded
        # path and are parsed only for exact package metadata; their contents
        # are never expanded into a model-visible collection.
        structure = {"format": "oxt", **_oxt_metadata(path), "parseable": True}
    elif path.stat().st_size > _ARTIFACT_MAX_BYTES:
        raise AgentError("budget_exhausted", "artifact exceeds parse limit")
    elif suffix == ".docx":
        structure = _parse_docx(path)
    elif suffix == ".xlsx":
        structure = _parse_xlsx(path)
    elif suffix == ".pptx":
        structure = _parse_pptx(path)
    elif suffix in {".odt", ".ods", ".odp"}:
        structure = _parse_odf(path)
    elif suffix == ".pdf":
        structure = _parse_pdf(path)
    elif suffix == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AgentError("invalid_request", "JSON artifact is not parseable") from error
        structure = {"format": "json", "root_type": type(value).__name__, "value": value}
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            rows = [row for _, row in zip(range(5_000), csv.reader(handle, delimiter=delimiter))]
        structure = {"format": suffix[1:], "rows": rows, "row_count": len(rows)}
    elif suffix in {".xml", ".html", ".htm"}:
        if suffix == ".xml":
            root = _safe_xml(path.read_bytes())
            parsed_markup = {
                "root_tag": root.tag,
                "text_excerpt": _text(" ".join(root.itertext())),
                "element_count": sum(1 for _ in root.iter()),
            }
        else:
            parsed_markup = _parse_html(path.read_bytes())
        structure = {"format": suffix[1:], **parsed_markup}
    elif suffix in {".txt", ".md"}:
        content = path.read_text(encoding="utf-8", errors="replace")
        structure = {
            "format": suffix[1:], "character_count": len(content),
            "line_count": content.count("\n") + 1, "text_excerpt": _text(content),
        }
    else:
        image = _image_dimensions(path)
        if image:
            structure = {**image, "visual_derivation": "deterministic"}
        elif zipfile.is_zipfile(path):
            with _safe_archive(path) as archive:
                structure = {
                    "format": "zip", "members": archive.namelist()[:1_000],
                    "member_count": len(archive.namelist()),
                }
        else:
            structure = {"format": suffix[1:] or "binary", "parseable": path.exists()}
    return {
        **_file_record(path), **structure,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "parseable": True,
    }


def _query_filesystem(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    scope = payload.get("scope") or {}
    path = _safe_path(scope.get("path"))
    if not path.exists():
        raise AgentError("not_found", f"guest path does not exist: {path}")
    if resource == "filesystem.entries":
        if not path.is_dir():
            raise AgentError("invalid_request", "filesystem.entries requires a directory")
        records = [_file_record(child) for child in sorted(path.iterdir(), key=lambda item: item.name)]
        return _private_page(
            records, payload, f"directory_{_json_hash(records)[:16]}"
        )
    if resource == "filesystem.metadata":
        record = _file_record(path)
        return {"records": [record], "revision": f"metadata_{_json_hash(record)[:16]}"}
    if resource == "filesystem.file":
        if not path.is_file():
            raise AgentError("invalid_request", "filesystem.file requires a regular file")
        parameters = payload.get("parameters") or {}
        offset = int(parameters.get("offset", 0))
        length = min(max(int(parameters.get("length", MAX_TEXT)), 1), MAX_TEXT)
        if offset < 0:
            raise AgentError("invalid_request", "file offset cannot be negative")
        full = path.read_text(encoding="utf-8", errors="replace")
        content = full[offset:offset + length]
        record = {
            **_file_record(path, include_hash=True), "content": content, "offset": offset,
            "content_truncated": offset + len(content) < len(full),
            "next_offset": offset + len(content) if offset + len(content) < len(full) else None,
            "character_count": len(full),
        }
        return {"records": [record], "revision": f"file_{_json_hash(record)[:16]}"}
    if resource == "artifact.structure":
        structure = _parse_artifact(path)
        return {"records": [structure], "revision": f"artifact_{structure['sha256'][:16]}"}
    if resource in {"artifact.owners", "artifact.sync"}:
        canonical_path = path.resolve()
        owners = []
        try:
            open_documents = _uno_documents()
        except AgentError:
            open_documents = []
        for document in open_documents:
            record = _uno_document_record(document)
            raw_url = record.get("url") or ""
            try:
                owned_path = Path(unquote(raw_url.removeprefix("file://"))).resolve()
            except Exception:
                continue
            if owned_path == canonical_path:
                owners.append(record)
        if resource == "artifact.owners":
            return {
                "records": owners, "revision": f"owners_{_json_hash(owners)[:16]}",
            }
        disk = _parse_artifact(path)
        comparisons = []
        for owner in owners:
            document = _resolve(owner["ref"])
            kind = owner.get("document_type")
            modified = bool(document.isModified())
            if kind == "writer":
                live_structure = _canonical_writer_live(document)
                disk_structure = _canonical_writer_disk(disk)
                sync_evidence = _structural_comparison(live_structure, disk_structure)
                matches = bool(sync_evidence["matched"] and not modified)
            elif kind == "calc":
                if disk.get("format") == "xlsx":
                    live_structure = _canonical_calc_live(document)
                    disk_structure = _canonical_calc_disk(disk)
                else:
                    live_structure = [record["name"] for record in _calc_sheet_records(document)]
                    disk_structure = [
                        record.get("name", "") for record in disk.get("tables", [])
                    ]
                sync_evidence = _structural_comparison(live_structure, disk_structure)
                matches = bool(sync_evidence["matched"] and not modified)
            elif kind == "impress":
                live_slides = _impress_slide_records(document, include_shapes=True)
                live_structure = {
                    "slide_count": document.getDrawPages().getCount(),
                    "text": [
                        record.get("text", "") for record in live_slides
                        if record.get("kind") == "presentation.shape"
                    ],
                }
                disk_structure = {
                    "slide_count": len(disk.get("slides", [])),
                    "text": [
                        shape.get("text", "")
                        for slide in disk.get("slides", [])
                        for shape in slide.get("shapes", [])
                    ],
                }
                sync_evidence = _structural_comparison(live_structure, disk_structure)
                matches = bool(sync_evidence["matched"] and not modified)
            else:
                matches = False
                sync_evidence = {
                    "matched": False, "live_sha256": None, "disk_sha256": None,
                    "first_difference": {"path": "$", "live": kind, "disk": disk.get("format")},
                }
            sync_evidence["document_unmodified"] = not modified
            comparisons.append({
                "kind": "artifact.sync", "artifact_path": str(path),
                "document_ref": owner["ref"], "document_type": kind,
                "live_app_matches_disk": matches, "live_modified": owner.get("modified"),
                "disk_sha256": disk["sha256"], "source": "libreoffice.uno+artifact-parser",
                "sync_evidence": sync_evidence,
                "freshness": "live",
            })
        if not comparisons:
            comparisons = [{
                "kind": "artifact.sync", "artifact_path": str(path),
                "document_ref": None, "live_app_matches_disk": None,
                "live_modified": None, "disk_sha256": disk["sha256"],
                "source": "artifact-parser", "freshness": "live",
            }]
        return {"records": comparisons, "revision": f"sync_{_json_hash(comparisons)[:16]}"}
    if resource in {"artifact.exports", "artifact.downloads"}:
        record = _file_record(path)
        record.update({
            "complete": path.is_file() and not path.name.endswith((".crdownload", ".part", ".tmp")),
            "parseable": bool(path.is_file() and _parse_artifact(path).get("parseable")),
        })
        return {"records": [record], "revision": f"transfer_{_json_hash(record)[:16]}"}
    raise AgentError("unknown_resource", resource)


def _bounded_command(argv: list[str], *, stdin: str | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=10, check=False,
            env={
                key: value for key, value in os.environ.items()
                if key in {"PATH", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "LANG"}
            },
        )
    except FileNotFoundError as error:
        raise AgentError("adapter_unavailable", f"native service command unavailable: {argv[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise AgentError("timeout", f"native service command timed out: {argv[0]}", retryable=True) from error
    return {
        "argv": argv, "exit_code": completed.returncode,
        "stdout": completed.stdout[:64 * 1024], "stderr": completed.stderr[:8 * 1024],
    }


def _bounded_native_mutation(
    argv: list[str], *, timeout_seconds: int
) -> dict[str, Any]:
    """Run one fixed argv mutation without exposing a shell or raw process API."""

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False,
            env={
                key: value for key, value in os.environ.items()
                if key in {
                    "PATH", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
                    "LANG", "HOME", "USER", "LOGNAME",
                }
            },
        )
    except FileNotFoundError as error:
        raise AgentError(
            "adapter_unavailable", f"native mutation command unavailable: {argv[0]}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise AgentError(
            "uncertain",
            f"native mutation timed out after execution began: {argv[0]}",
            side_effect_state="unknown",
        ) from error
    return {
        "argv": list(argv), "exit_code": completed.returncode,
        "stdout": completed.stdout[:64 * 1024], "stderr": completed.stderr[:8 * 1024],
    }


def _dispatch_desktop_entry(desktop_id: str) -> dict[str, Any]:
    """Dispatch one validated desktop entry without inheritable capture pipes.

    GUI children may inherit gtk-launch's stdout/stderr descriptors. Using
    ``subprocess.run(..., capture_output=True)`` then waits forever for EOF
    even after gtk-launch itself exits, falsely classifying a successful app
    launch as an uncertain timeout. Desktop launch is an asynchronous native
    dispatch, so accept the successfully spawned launcher and let semantic
    process/window queries establish the application's later state.
    """

    argv = ["gtk-launch", desktop_id.removesuffix(".desktop")]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env={
                key: value for key, value in os.environ.items()
                if key in {
                    "PATH", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                    "XDG_RUNTIME_DIR", "LANG", "HOME", "USER", "LOGNAME",
                }
            },
        )
    except FileNotFoundError as error:
        raise AgentError(
            "adapter_unavailable", "native desktop launcher is unavailable"
        ) from error
    except OSError as error:
        raise AgentError(
            "adapter_unavailable", "native desktop launch could not be dispatched"
        ) from error
    return {
        "argv": argv,
        "launcher_pid": process.pid,
        "dispatch_state": "accepted",
    }


_GSETTINGS_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,255}$")
_GSETTINGS_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
_DESKTOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}(?:\.desktop)?$")
_PACKAGE_NAME = re.compile(
    r"^[a-z0-9][a-z0-9+.-]{0,127}(?::[a-z0-9][a-z0-9-]{0,31})?$"
)
_EXTENSION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")


def _validated_package_name(raw: Any) -> str:
    if not isinstance(raw, str) or not _PACKAGE_NAME.fullmatch(raw):
        raise AgentError(
            "invalid_request",
            "package name must be one explicit Debian package identifier",
        )
    return raw


def _validated_extension_identifier(raw: Any) -> str:
    if not isinstance(raw, str) or not _EXTENSION_IDENTIFIER.fullmatch(raw):
        raise AgentError("invalid_request", "invalid LibreOffice extension identifier")
    return raw


_DPKG_QUERY_FORMAT = (
    "${binary:Package}\\t${db:Status-Abbrev}\\t${Version}\\t${Architecture}\\n"
)


def _package_records(name: str | None = None) -> list[dict[str, Any]]:
    argv = ["dpkg-query", "-W", f"-f={_DPKG_QUERY_FORMAT}"]
    if name is not None:
        argv.append(_validated_package_name(name))
    command = _bounded_command(argv)
    if command["exit_code"] != 0 and name is None:
        raise AgentError(
            "adapter_unavailable",
            _text(command["stderr"]) or "package registry is unavailable",
            retryable=True,
        )
    records: list[dict[str, Any]] = []
    for line in command["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        package_name, status, version, architecture = parts
        if not _PACKAGE_NAME.fullmatch(package_name):
            continue
        installed = status.startswith("ii")
        records.append({
            "kind": "os.package", "name": package_name,
            "installed": installed,
            "version": version if installed else None,
            "architecture": architecture or None,
            "status": status,
            "advertised_actions": ["install_package"],
            "source": "dpkg-query", "freshness": "live",
        })
    if name is not None:
        matches = [record for record in records if record["name"] == name]
        if matches:
            return matches
        return [{
            "kind": "os.package", "name": name, "installed": False,
            "version": None, "architecture": None, "status": "not-installed",
            "advertised_actions": ["install_package"],
            "source": "dpkg-query", "freshness": "live",
        }]
    return records


def _query_packages(payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    raw_name = parameters.get("name")
    name = _validated_package_name(raw_name) if raw_name is not None else None

    def build() -> tuple[list[dict[str, Any]], str]:
        records = _package_records(name)
        return records, f"packages_{_json_hash(records)[:16]}"

    return _private_snapshot_page("os.packages", payload, build)


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _oxt_metadata(path: Path) -> dict[str, str]:
    if path.suffix.casefold() != ".oxt" or not path.is_file():
        raise AgentError("invalid_request", "extension path must name an existing .oxt file")
    if path.stat().st_size > _LIBREOFFICE_EXTENSION_MAX_BYTES:
        raise AgentError("budget_exhausted", "extension package exceeds artifact limit")
    try:
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > _ARTIFACT_MAX_MEMBERS:
                raise AgentError("budget_exhausted", "extension package has too many members")
            raw = archive.read("description.xml")
    except AgentError:
        raise
    except (KeyError, OSError, zipfile.BadZipFile) as error:
        raise AgentError(
            "invalid_request", "extension package has no parseable description.xml"
        ) from error
    if len(raw) > 2 * 1024 * 1024:
        raise AgentError("budget_exhausted", "extension description is too large")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise AgentError("invalid_request", "extension description XML is malformed") from error
    metadata: dict[str, str] = {}
    for node in root.iter():
        name = _local_xml_name(str(node.tag))
        if name in {"identifier", "version"} and node.attrib.get("value"):
            metadata[name] = node.attrib["value"].strip()
        elif name == "name" and node.text and "display_name" not in metadata:
            metadata["display_name"] = node.text.strip()
    identifier = _validated_extension_identifier(metadata.get("identifier"))
    return {
        "identifier": identifier,
        "version": _text(metadata.get("version"), 256),
        "display_name": _text(metadata.get("display_name") or identifier, 512),
    }


def _parse_unopkg_extensions(output: str) -> list[dict[str, Any]]:
    raw_records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = re.match(r"^([^:]{1,80}):\s*(.*)$", line)
        if not match:
            continue
        key = "_".join(match.group(1).casefold().split())
        value = match.group(2).strip()
        if key == "identifier" and current.get("identifier"):
            raw_records.append(current)
            current = {}
        current[key] = value
    if current.get("identifier"):
        raw_records.append(current)
    records = []
    for raw in raw_records:
        identifier = raw.get("identifier", "")
        if not _EXTENSION_IDENTIFIER.fullmatch(identifier):
            continue
        registered = raw.get("is_registered", "").casefold()
        if registered in {"yes", "true", "1"}:
            registration_state = "registered"
            enabled: bool | None = True
        elif registered in {"no", "false", "0"}:
            registration_state = "not_registered"
            enabled = False
        elif registered in {"n/a", "not applicable", "not-applicable"}:
            # Package bundles with no registerable UNO components are valid
            # installed extensions. `unopkg` reports registration as n/a; it
            # does not mean that the bundle is disabled.
            registration_state = "not_applicable"
            enabled = None
        elif registered == "ambiguous":
            registration_state = "ambiguous"
            enabled = None
        else:
            registration_state = "unknown"
            enabled = None
        records.append({
            "kind": "libreoffice.extension", "identifier": identifier,
            "display_name": raw.get("display_name") or raw.get("name") or identifier,
            "version": raw.get("version") or None,
            "url": raw.get("url") or None,
            "installed": True, "enabled": enabled,
            "registration_state": registration_state,
            "advertised_actions": ["install_extension"],
            "source": "unopkg", "freshness": "live",
        })
    return records


def _libreoffice_extension_records(identifier: str | None = None) -> list[dict[str, Any]]:
    command = _bounded_command(["unopkg", "list", "--verbose"])
    if command["exit_code"] != 0:
        raise AgentError(
            "adapter_unavailable",
            _text(command["stderr"]) or "LibreOffice extension registry is unavailable",
            retryable=True,
        )
    records = _parse_unopkg_extensions(command["stdout"])
    if identifier is not None:
        matches = [record for record in records if record["identifier"] == identifier]
        if matches:
            return matches
        return [{
            "kind": "libreoffice.extension", "identifier": identifier,
            "display_name": identifier, "version": None, "url": None,
            "installed": False, "enabled": False, "registration_state": "absent",
            "advertised_actions": ["install_extension"],
            "source": "unopkg", "freshness": "live",
        }]
    return records


def _query_libreoffice_extensions(payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    raw_identifier = parameters.get("identifier")
    identifier = (
        _validated_extension_identifier(raw_identifier)
        if raw_identifier is not None else None
    )

    def build() -> tuple[list[dict[str, Any]], str]:
        records = _libreoffice_extension_records(identifier)
        if identifier is None:
            # A collection may be empty while its registry remains an
            # actionable semantic owner. Without this stable owner there is no
            # entity capability on which to invoke the first installation.
            records = [{
                "kind": "libreoffice.extension_registry",
                "name": "LibreOffice extension registry",
                "installed_extension_count": len(records),
                "advertised_actions": ["install_extension"],
                "source": "unopkg", "freshness": "live",
            }, *records]
        return records, f"lo_extensions_{_json_hash(records)[:16]}"

    return _private_snapshot_page("libreoffice.extensions", payload, build)


def _permission_failure(command: dict[str, Any]) -> bool:
    detail = f"{command.get('stdout', '')}\n{command.get('stderr', '')}".casefold()
    return any(value in detail for value in (
        "permission denied", "not permitted", "must be root", "are you root",
        "a password is required", "password is required", "not allowed to execute",
    ))


def _install_os_package(arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(arguments) - {"name"})
    if unknown:
        raise AgentError(
            "invalid_request", f"install_package received unsupported arguments: {unknown!r}"
        )
    name = _validated_package_name(arguments.get("name"))
    before = _package_records(name)[0]
    if before["installed"]:
        raise AgentError("no_effect", f"package is already installed: {name}")
    argv = [
        "sudo", "-n", "--",
        "/usr/local/libexec/ghost-semantic-install-package", name,
    ]
    command = _bounded_native_mutation(argv, timeout_seconds=180)
    after = _package_records(name)[0]
    if command["exit_code"] == 65 and not after["installed"]:
        raise AgentError(
            "not_found",
            _text(command["stderr"])
            or "package is unavailable from configured repositories",
            side_effect_state="none",
        )
    if command["exit_code"] != 0 and not after["installed"]:
        if _permission_failure(command):
            raise AgentError(
                "permission_denied",
                _text(command["stderr"]) or "package manager privileges are unavailable",
            )
        raise AgentError(
            "uncertain",
            _text(command["stderr"]) or "package installation did not complete",
            side_effect_state="unknown",
        )
    if not after["installed"]:
        raise AgentError(
            "postcondition_failed",
            "package registry does not show the requested package after installation",
            side_effect_state="applied",
        )
    return {
        "execution_path": "native_api", "name": name,
        "installed": True, "version": after["version"],
        "architecture": after["architecture"],
        "system_restart_required": Path("/var/run/reboot-required").exists(),
        "command_exit_code": command["exit_code"],
        "postcondition": {
            "registry": "dpkg-query", "installed": True,
            "version": after["version"],
        },
    }


def _install_libreoffice_extension(arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(arguments) - {"path"})
    if unknown:
        raise AgentError(
            "invalid_request",
            f"install_extension received unsupported arguments: {unknown!r}",
        )
    path = _safe_path(arguments.get("path")).resolve()
    metadata = _oxt_metadata(path)
    before = _libreoffice_extension_records(metadata["identifier"])[0]
    if before["installed"] and (
        not metadata["version"] or before.get("version") == metadata["version"]
    ):
        raise AgentError(
            "no_effect", f"LibreOffice extension is already installed: {metadata['identifier']}"
        )
    running = _bounded_command(["pgrep", "-x", "soffice.bin"])["exit_code"] == 0
    argv = ["unopkg", "add", "--force", "--suppress-license", str(path)]
    command = _bounded_native_mutation(argv, timeout_seconds=120)
    after = _libreoffice_extension_records(metadata["identifier"])[0]
    requested_version = metadata.get("version")
    observed_version = after.get("version")
    version_matches = bool(
        not requested_version or observed_version == requested_version
    )
    installed = bool(after["installed"] and version_matches)
    if command["exit_code"] != 0 and not installed:
        if _permission_failure(command):
            raise AgentError(
                "permission_denied",
                _text(command["stderr"]) or "extension registry is not writable",
            )
        raise AgentError(
            "uncertain",
            _text(command["stderr"]) or "extension installation did not complete",
            side_effect_state="unknown",
        )
    if not installed:
        raise AgentError(
            "postcondition_failed",
            "LibreOffice registry does not show the requested extension identity and version",
            side_effect_state="applied",
        )
    return {
        "execution_path": "native_api", "path": str(path),
        "sha256": _sha256_path(path),
        "identifier": metadata["identifier"],
        "display_name": after.get("display_name") or metadata["display_name"],
        "version": observed_version or requested_version or None,
        "installed": True, "enabled": after.get("enabled"),
        "registration_state": after.get("registration_state", "unknown"),
        "libreoffice_restart_required": running,
        "command_exit_code": command["exit_code"],
        "postcondition": {
            "registry": "unopkg", "installed": True,
            "enabled": after.get("enabled"),
            "registration_state": after.get("registration_state", "unknown"),
            "identifier": metadata["identifier"],
            "version": observed_version or requested_version or None,
        },
    }


def _desktop_entries() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    roots = [Path.home() / ".local/share/applications", Path("/usr/share/applications")]
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.desktop")):
            if path.name in seen:
                continue
            seen.add(path.name)
            name = path.stem
            executable = ""
            hidden = False
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Name=") and name == path.stem:
                        name = line[5:]
                    elif line.startswith("Exec=") and not executable:
                        executable = line[5:]
                    elif line == "Hidden=true":
                        hidden = True
            except OSError:
                continue
            records.append({
                "kind": "os.desktop_entry", "desktop_id": path.name, "name": _text(name),
                "executable_template": _text(executable), "hidden": hidden,
                "advertised_actions": ["launch"], "source": "desktop-entry", "freshness": "live",
            })
    return records[:MAX_RECORDS]


def _chrome_root_command() -> tuple[int, list[str]]:
    candidates: list[tuple[int, list[str]]] = []
    debugging_candidates: list[tuple[int, list[str]]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        except OSError:
            continue
        if not argv:
            continue
        # Chromium rewrites child process titles on Linux. In those processes
        # /proc/<pid>/cmdline can become one space-delimited argv[0] instead of
        # the original NUL-delimited vector. Normalize it before testing
        # --type and executable identity; never feed the full title to Path.
        if len(argv) == 1 and " --" in argv[0]:
            try:
                normalized = shlex.split(argv[0])
            except ValueError:
                normalized = []
            if normalized:
                argv = normalized
        executable = Path(argv[0]).name.casefold()
        try:
            resolved_executable = (entry / "exe").resolve()
            native_executable = resolved_executable.name.casefold()
        except OSError:
            resolved_executable = None
            native_executable = ""
        chrome_names = {
            "google-chrome", "google-chrome-stable", "chrome", "chromium",
            "chromium-browser",
        }
        if executable not in chrome_names and native_executable not in chrome_names:
            continue
        # Renderer, utility, zygote, and GPU subprocesses share Chrome's
        # executable but advertise a --type. The browser root does not. Some
        # OSWorld images expose CDP through a wrapper/forwarder and therefore
        # omit the debugging switch from the actual browser argv.
        if any(value.startswith("--type=") for value in argv):
            continue
        executable_resolves = False
        if len(argv[0]) < 4_096:
            try:
                executable_resolves = Path(argv[0]).exists() or shutil.which(argv[0]) is not None
            except OSError:
                executable_resolves = False
        if resolved_executable is not None and not executable_resolves:
            argv[0] = str(resolved_executable)
        candidates.append((int(entry.name), argv))
        if any(
            value == "--remote-debugging-port"
            or value.startswith("--remote-debugging-port=")
            for value in argv
        ):
            debugging_candidates.append((int(entry.name), argv))
    if not candidates:
        raise AgentError("adapter_unavailable", "running Chrome root process was not found", retryable=True)
    # Prefer a root that explicitly owns CDP, but accept a verified browser root
    # when the image's forwarding layer owns that port. Smallest PID breaks a
    # tie between multiple profile roots deterministically.
    return min(debugging_candidates or candidates, key=lambda item: item[0])


def _chrome_profile_context() -> tuple[int, list[str], Path]:
    pid, argv = _chrome_root_command()
    user_data: Path | None = None
    profile_name = "Default"
    for value in argv[1:]:
        if value.startswith("--user-data-dir="):
            user_data = Path(value.split("=", 1)[1]).expanduser()
        elif value.startswith("--profile-directory="):
            profile_name = value.split("=", 1)[1]
    if user_data is None:
        executable = Path(argv[0]).name.casefold()
        conventional = (
            Path.home() / ".config" / "chromium",
            Path.home() / ".config" / "google-chrome",
            Path.home() / "snap" / "chromium" / "common" / "chromium",
        )
        preferred = "chromium" if "chromium" in executable else "google-chrome"
        ordered = sorted(
            conventional,
            key=lambda path: (preferred not in path.parts, str(path)),
        )
        user_data = next(
            (path for path in ordered if (path / profile_name).is_dir()),
            ordered[0],
        )
    profile = (user_data / profile_name).resolve()
    home = Path.home().resolve()
    if home not in profile.parents:
        raise AgentError("policy_violation", "Chrome profile is outside the guest home")
    if not profile.is_dir():
        raise AgentError("not_found", "active Chrome profile directory is unavailable")
    return pid, argv, profile


def _chrome_read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AgentError("not_found", f"Chrome profile state is absent: {path.name}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentError(
            "adapter_unavailable", f"Chrome profile state is unreadable: {path.name}"
        ) from error
    if not isinstance(value, dict):
        raise AgentError("adapter_unavailable", "Chrome profile JSON root is invalid")
    return value


def _chrome_bookmark_records(profile: Path) -> list[dict[str, Any]]:
    data = _chrome_read_json(profile / "Bookmarks")
    records: list[dict[str, Any]] = []

    def visit(node: Any, parent_id: str | None = None) -> None:
        if not isinstance(node, dict) or len(records) >= MAX_RECORDS:
            return
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "folder")
        records.append({
            "kind": "chrome.profile_bookmark",
            "id": node_id,
            "parent_id": parent_id,
            "title": _text(node.get("name")),
            "url": _text(node.get("url"), MAX_TEXT) if node_type == "url" else None,
            "folder": node_type != "url",
        })
        for child in node.get("children") or []:
            visit(child, node_id)

    for root in (data.get("roots") or {}).values():
        visit(root)
    return records


def _chrome_flat_preferences(profile: Path) -> list[dict[str, Any]]:
    data = _chrome_read_json(profile / "Preferences")
    records: list[dict[str, Any]] = []

    def visit(value: Any, prefix: str = "") -> None:
        if len(records) >= MAX_RECORDS:
            return
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key], f"{prefix}.{key}" if prefix else str(key))
            return
        if isinstance(value, list) and len(value) > 100:
            public_value: Any = {"item_count": len(value), "sha256": _json_hash(value)}
        else:
            public_value = value
        sensitive = any(
            token in prefix.casefold()
            for token in ("password", "secret", "token", "credential", "auth")
        )
        records.append({
            "kind": "chrome.profile_preference",
            "key": prefix,
            "value": "[redacted]" if sensitive else _uno_json(public_value),
            "secret_value_redacted": sensitive,
        })

    visit(data)
    return records


def _chrome_history_records(profile: Path, kind: str) -> list[dict[str, Any]]:
    database = profile / "History"
    if not database.is_file():
        raise AgentError("not_found", "Chrome History database is unavailable")
    connection: sqlite3.Connection | None = None
    try:
        # A live Chrome process may hold an exclusive SQLite lock. Copy the
        # database and its WAL sidecar into an episode-private temporary
        # directory, then let SQLite reconcile that stable snapshot. This is
        # read-only with respect to the browser profile and avoids treating an
        # ordinary live lock as adapter unavailability.
        with tempfile.TemporaryDirectory(prefix="ghost-chrome-history-") as raw_temp:
            snapshot = Path(raw_temp) / "History"
            shutil.copy2(database, snapshot)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(str(snapshot) + suffix))
            connection = sqlite3.connect(str(snapshot), timeout=2.0)
            connection.execute("PRAGMA query_only=ON")
            if kind == "history":
                rows = connection.execute(
                    "SELECT url,title,visit_count,last_visit_time FROM urls "
                    "ORDER BY last_visit_time DESC LIMIT 5000"
                ).fetchall()
                return [{
                    "kind": "chrome.profile_history",
                    "url": _text(row[0], MAX_TEXT),
                    "title": _text(row[1]),
                    "visit_count": int(row[2] or 0),
                    "last_visit_time": int(row[3] or 0),
                } for row in rows]
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(downloads)")
            }
            required = {"id", "state"}
            if not required <= columns:
                raise AgentError(
                    "adapter_unavailable", "Chrome downloads schema is unsupported"
                )
            requested = [
                name for name in (
                    "id", "current_path", "target_path", "tab_url", "site_url",
                    "total_bytes", "received_bytes", "state", "danger_type", "start_time",
                )
                if name in columns
            ]
            rows = connection.execute(
                f"SELECT {','.join(requested)} FROM downloads "
                + ("ORDER BY start_time DESC " if "start_time" in columns else "")
                + "LIMIT 5000"
            ).fetchall()
            indexes = {name: index for index, name in enumerate(requested)}

            def item(row: Any, name: str, default: Any = None) -> Any:
                index = indexes.get(name)
                return row[index] if index is not None else default

            return [{
                "kind": "chrome.profile_download",
                "id": int(item(row, "id", 0)),
                "filename": _text(
                    item(row, "current_path") or item(row, "target_path"), MAX_TEXT
                ),
                "url": _text(item(row, "tab_url") or item(row, "site_url"), MAX_TEXT),
                "total_bytes": int(item(row, "total_bytes", 0) or 0),
                "bytes_received": int(item(row, "received_bytes", 0) or 0),
                "state": int(item(row, "state", 0) or 0),
                "danger_type": int(item(row, "danger_type", 0) or 0),
            } for row in rows]
    except sqlite3.Error as error:
        raise AgentError(
            "adapter_unavailable", "Chrome profile database query failed", retryable=True
        ) from error
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass


def _chrome_extension_records(profile: Path) -> list[dict[str, Any]]:
    preferences = _chrome_read_json(profile / "Preferences")
    settings = ((preferences.get("extensions") or {}).get("settings") or {})
    records = []
    for extension_id, value in list(settings.items())[:MAX_RECORDS]:
        if not isinstance(value, dict):
            continue
        manifest = value.get("manifest") if isinstance(value.get("manifest"), dict) else {}
        records.append({
            "kind": "chrome.profile_extension",
            "id": str(extension_id),
            "name": _text(manifest.get("name")),
            "version": _text(manifest.get("version"), 256),
            "enabled": int(value.get("state", 0) or 0) == 1,
            "install_type": value.get("location"),
            "path": _text(value.get("path"), MAX_TEXT),
        })
    return records


def _query_chrome_private(payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    kind = str(parameters.get("kind") or "")
    _pid, _argv, profile = _chrome_profile_context()
    if kind == "bookmarks":
        records = _chrome_bookmark_records(profile)
    elif kind == "settings":
        records = _chrome_flat_preferences(profile)
    elif kind in {"history", "downloads"}:
        records = _chrome_history_records(profile, kind)
    elif kind == "extensions":
        records = _chrome_extension_records(profile)
    elif kind == "profile":
        records = [{
            "kind": "chrome.profile",
            "profile_name": profile.name,
            "profile_hash": hashlib.sha256(str(profile).encode()).hexdigest(),
        }]
    else:
        raise AgentError("invalid_request", "unknown private Chrome profile query")
    revision = f"chrome_profile_{_json_hash(records)[:16]}"
    return _private_page(records, payload, revision)


def _chrome_load_unpacked(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_path = arguments.get("path")
    path = _safe_path(raw_path).resolve()
    manifest_path = path / "manifest.json"
    if not path.is_dir() or not manifest_path.is_file():
        raise AgentError("not_found", "unpacked extension directory/manifest does not exist")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentError("invalid_request", "extension manifest is not valid JSON") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("manifest_version") not in {2, 3}
        or not isinstance(manifest.get("name"), str)
        or not isinstance(manifest.get("version"), str)
    ):
        raise AgentError("invalid_request", "extension manifest is missing required fields")
    pid, old_argv = _chrome_root_command()
    executable = old_argv[0]
    if not Path(executable).exists() and not shutil.which(executable):
        raise AgentError("adapter_unavailable", "Chrome executable no longer exists")
    alternate = next(
        (
            candidate for name in ("chromium", "chromium-browser")
            if (candidate := shutil.which(name)) is not None
        ),
        None,
    )
    clean: list[str] = []
    extension_paths: list[str] = []
    disabled_features: list[str] = []
    for value in old_argv[1:]:
        if value.startswith("--load-extension="):
            extension_paths.extend(
                item for item in value.split("=", 1)[1].split(",") if item
            )
            continue
        if value.startswith("--disable-extensions-except="):
            continue
        if value.startswith("--disable-features="):
            disabled_features.extend(
                item for item in value.split("=", 1)[1].split(",") if item
            )
            continue
        # Existing URL arguments are restored from the typed live-target list.
        if value.startswith(("http://", "https://", "chrome://", "file://")):
            continue
        clean.append(value)
    extension_paths.append(str(path))
    # Chrome-branded desktop builds gate --load-extension behind this feature
    # in the versions used by the public OSWorld image. This guarded relaunch
    # is the semantic equivalent of the user-authorized Load unpacked action;
    # the persistent profile registry is independently verified below.
    disabled_features.append("DisableLoadExtensionCommandLineSwitch")
    clean.append(f"--disable-features={','.join(dict.fromkeys(disabled_features))}")
    clean.append(f"--load-extension={','.join(dict.fromkeys(extension_paths))}")
    raw_urls = arguments.get("restore_urls") or []
    if not isinstance(raw_urls, list) or len(raw_urls) > 100 or not all(isinstance(value, str) for value in raw_urls):
        raise AgentError("invalid_request", "restore_urls must be a bounded string array")
    urls = [
        value for value in dict.fromkeys(raw_urls)
        if value.startswith(("http://", "https://", "chrome://", "file://"))
    ]
    new_argv = [executable, *clean, *urls]
    if alternate is not None and "chromium" not in Path(executable).name.casefold():
        executable = alternate
        if not any(value.startswith("--user-data-dir=") for value in clean):
            clean.append(f"--user-data-dir={Path.home() / '.config' / 'google-chrome'}")
        new_argv = [executable, *clean, *urls]
    try:
        os.kill(pid, 15)
    except ProcessLookupError as error:
        raise AgentError("revision_conflict", "Chrome exited before guarded relaunch", retryable=True) from error
    def process_is_running(candidate: int) -> bool:
        try:
            state = Path(f"/proc/{candidate}/stat").read_text()
            closing = state.rfind(")")
            return closing >= 0 and state[closing + 2:closing + 3] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            return False
        except OSError:
            return True

    for _ in range(80):
        if not process_is_running(pid):
            break
        time.sleep(0.1)
    if process_is_running(pid):
        raise AgentError("timeout", "Chrome did not stop cleanly before relaunch", retryable=False)
    process = subprocess.Popen(
        new_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=os.environ.copy(), start_new_session=True, close_fds=True,
    )
    port = 9222
    user_data = Path.home() / ".config/google-chrome"
    profile_name = "Default"
    for value in clean:
        if value.startswith("--remote-debugging-port="):
            try:
                port = int(value.split("=", 1)[1])
            except ValueError:
                pass
        elif value.startswith("--user-data-dir="):
            user_data = Path(value.split("=", 1)[1])
        elif value.startswith("--profile-directory="):
            profile_name = value.split("=", 1)[1]
    ready = False
    for _ in range(60):
        if process.poll() is not None:
            raise AgentError("adapter_unavailable", "Chrome exited during guarded relaunch")
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                if response.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.2)
    if not ready:
        raise AgentError("timeout", "Chrome CDP did not recover after guarded relaunch")
    extension_id = None
    preference_path = user_data / profile_name / "Preferences"
    for _ in range(40):
        try:
            preferences = json.loads(preference_path.read_text(encoding="utf-8"))
            settings = preferences.get("extensions", {}).get("settings", {})
            for candidate, record in settings.items():
                if not isinstance(record, dict):
                    continue
                installed_path = record.get("path")
                if isinstance(installed_path, str) and Path(installed_path).resolve() == path:
                    extension_id = candidate
                    break
        except Exception:
            pass
        if extension_id:
            break
        time.sleep(0.2)
    registry_verified_on_disk = bool(extension_id)
    if not extension_id:
        # Unpacked command-line extensions are live in chrome.management but
        # are not guaranteed to be written into Preferences. Chrome derives a
        # stable ID from the normalized absolute path when no manifest key is
        # supplied. Return that candidate so the outer Chrome adapter can
        # prove it against the live management registry after CDP reconnects.
        digest = hashlib.sha256(str(path).encode("utf-8")).digest()[:16]
        extension_id = "".join(
            chr(ord("a") + nibble)
            for byte in digest
            for nibble in (byte >> 4, byte & 0x0F)
        )
    return {
        "execution_path": "native_api", "guarded_relaunch": True,
        "extension_id": extension_id, "name": manifest["name"], "path": str(path),
        "enabled": True, "browser_pid": process.pid, "cdp_port": port,
        "registry_verified_on_disk": registry_verified_on_disk,
    }


def _query_os(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    records: list[dict[str, Any]]
    if resource == "os.settings":
        schema = parameters.get("schema")
        key = parameters.get("key")
        if schema is None:
            if key is not None:
                raise AgentError("invalid_request", "os.settings key requires a schema")
            command = _bounded_command(["gsettings", "list-schemas"])
            if command["exit_code"] != 0:
                raise AgentError(
                    "adapter_unavailable",
                    _text(command["stderr"]) or "GSettings schemas are unavailable",
                    retryable=True,
                )
            records = [
                {
                    "kind": "os.setting_schema",
                    "schema": value.strip(),
                    "advertised_actions": [],
                    "source": "gsettings",
                    "freshness": "live",
                }
                for value in command["stdout"].splitlines()
                if _GSETTINGS_NAME.fullmatch(value.strip())
            ]
            revision = f"os_{_json_hash(records)[:16]}"
            return _private_page(records, payload, revision)
        if not isinstance(schema, str) or not _GSETTINGS_NAME.fullmatch(schema):
            raise AgentError("invalid_request", "os.settings requires a valid schema")
        if key is not None and (not isinstance(key, str) or not _GSETTINGS_KEY.fullmatch(key)):
            raise AgentError("invalid_request", "invalid GSettings key")
        command = _bounded_command(["gsettings", "get", schema, key] if key else ["gsettings", "list-recursively", schema])
        if command["exit_code"] != 0:
            raise AgentError("not_found", _text(command["stderr"]) or "GSettings schema/key unavailable")
        records = []
        for line in command["stdout"].splitlines():
            parts = line.split(maxsplit=2)
            if key:
                setting_key, value = key, line.strip()
            elif len(parts) >= 3:
                _line_schema, setting_key, value = parts
            else:
                continue
            records.append({
                "kind": "os.setting", "schema": schema, "key": setting_key, "value": value,
                "advertised_actions": ["set_setting"], "source": "gsettings", "freshness": "live",
            })
    elif resource == "os.clipboard":
        command = _bounded_command(["xclip", "-selection", "clipboard", "-o"])
        records = [{
            "kind": "os.clipboard", "text": _text(command["stdout"]),
            "available": command["exit_code"] == 0,
            "advertised_actions": ["write_clipboard"], "source": "xclip", "freshness": "live",
        }]
    elif resource == "os.desktop_entries":
        records = _desktop_entries()
    elif resource == "os.notifications":
        raise AgentError(
            "representation_gap",
            "the freedesktop notification service does not expose an active-notification list; visible alerts remain queryable through AT-SPI",
        )
    elif resource == "os.network_state":
        command = _bounded_command(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
        records = []
        for line in command["stdout"].splitlines():
            parts = line.split(":", 3)
            if len(parts) == 4:
                records.append({
                    "kind": "os.network_interface", "device": parts[0], "type": parts[1],
                    "state": parts[2], "connection": parts[3], "source": "networkmanager",
                    "freshness": "live",
                })
    elif resource == "os.audio_state":
        sink = _bounded_command(["pactl", "get-default-sink"])
        volume = _bounded_command(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        muted = _bounded_command(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        records = [{
            "kind": "os.audio_state", "default_sink": sink["stdout"].strip(),
            "volume": _text(volume["stdout"]), "muted": "yes" in muted["stdout"].casefold(),
            "advertised_actions": ["set_audio_volume", "set_audio_muted"],
            "source": "pulseaudio", "freshness": "live",
        }]
    elif resource == "os.display_state":
        command = _bounded_command(["xrandr", "--current"])
        records = [{
            "kind": "os.display_state", "description": _text(command["stdout"]),
            "source": "xrandr", "freshness": "live",
        }]
    elif resource == "os.power_state":
        command = _bounded_command(["upower", "-e"])
        records = [{
            "kind": "os.power_state", "devices": command["stdout"].splitlines()[:100],
            "source": "upower", "freshness": "live",
        }]
    elif resource == "os.session_state":
        command = _bounded_command(["loginctl", "show-session", "self", "--no-pager"])
        records = [{
            "kind": "os.session_state", "description": _text(command["stdout"]),
            "source": "logind", "freshness": "live",
        }]
    else:
        raise AgentError("unknown_resource", resource)
    revision = f"os_{_json_hash(records)[:16]}"
    return _private_page(records, payload, revision)


def query(payload: dict[str, Any]) -> dict[str, Any]:
    resource = payload.get("resource")
    if resource == "system.health":
        return {"records": [health()], "revision": "health_1"}
    if resource == "system.capabilities":
        return {"records": CAPABILITIES, "revision": "capabilities_1"}
    if resource == "system.capability":
        requested = (payload.get("parameters") or {}).get("resource")
        matches = [item for item in CAPABILITIES if requested in item["resources"]]
        if not matches:
            raise AgentError("unknown_resource", str(requested))
        return {"records": matches, "revision": "capabilities_1"}
    if resource == "system.pending_state":
        try:
            _uno_document_records()
        except AgentError:
            # LibreOffice is optional for non-office episodes.
            STATE.modified_documents = []
        return {
            "records": [{
                "uncertain_actions": [],
                "modified_documents": STATE.modified_documents,
                "running_exports": [],
                "pending_downloads": [],
            }],
            "revision": "pending_1",
        }
    if resource == "system.surfaces":
        return _query_accessibility(str(resource), payload)
    if resource in {
        "ui.elements", "ui.surfaces", "os.applications", "os.windows", "os.dialogs", "os.file_choosers",
    }:
        return _query_accessibility(str(resource), payload)
    if resource in {
        "filesystem.entries", "filesystem.file", "filesystem.metadata",
        "artifact.structure", "artifact.owners", "artifact.sync",
        "artifact.exports", "artifact.downloads",
    }:
        return _query_filesystem(str(resource), payload)
    if any(str(resource).startswith(prefix) for prefix in (
        "document.", "writer.", "spreadsheet.", "presentation.",
    )):
        return _query_uno(str(resource), payload)
    if resource == "libreoffice.extensions":
        return _query_libreoffice_extensions(payload)
    if resource == "os.packages":
        return _query_packages(payload)
    if resource in {
        "os.settings", "os.clipboard", "os.notifications", "os.desktop_entries",
        "os.network_state", "os.audio_state", "os.display_state", "os.power_state",
        "os.session_state",
    }:
        return _query_os(str(resource), payload)
    if resource == "os.processes":
        completed = subprocess.run(
            ["ps", "-eo", "pid=,comm=,stat="], capture_output=True, text=True,
            timeout=5, check=False,
        )
        records = []
        for line in completed.stdout.splitlines()[:1_000]:
            parts = line.strip().split(maxsplit=2)
            if len(parts) == 3:
                records.append({"pid": int(parts[0]), "name": parts[1], "state": parts[2]})
        return {"records": records, "revision": f"process_{_json_hash(records)[:16]}"}
    if resource == "chrome.private.profile":
        return _query_chrome_private(payload)
    raise AgentError("unknown_resource", str(resource))


def _resolve(ref: Any) -> Any:
    if not isinstance(ref, str) or ref not in STATE.refs:
        raise AgentError("stale_ref", "entity ref is missing or no longer resolves", retryable=True)
    return STATE.refs[ref]


def _private_semantic_click(ref: str, target: Any) -> dict[str, Any]:
    """Click a uniquely resolved AT-SPI entity without exposing geometry.

    This route is used only after the direct Action interface was proven
    absent.  Current visibility, positive extents, and an exact AT-SPI hit test
    are required before the server emits a private input event.
    """

    pyatspi = _atspi_module()
    states = _states(target, pyatspi)
    if states.get("enabled") is False:
        raise AgentError(
            "precondition_failed",
            "semantic input target is disabled "
            f"(role={_role_name(target)!r}, "
            f"name={_text(_safe_attr(target, 'name', ''), 200)!r})",
        )
    if states.get("visible") is False or states.get("showing") is False:
        raise AgentError("precondition_failed", "semantic input target is not showing")
    try:
        component = target.queryComponent()
        extents = component.getExtents(pyatspi.DESKTOP_COORDS)
        x = int(extents.x)
        y = int(extents.y)
        width = int(extents.width)
        height = int(extents.height)
    except Exception as error:
        raise AgentError(
            "unsupported", "semantic input target has no current component extents"
        ) from error
    if width <= 0 or height <= 0 or x < -100_000 or y < -100_000:
        raise AgentError("unsupported", "semantic input target has invalid private bounds")
    center_x = x + width // 2
    center_y = y + height // 2
    try:
        hit = component.getAccessibleAtPoint(
            center_x, center_y, pyatspi.DESKTOP_COORDS
        )
        hit_ref = STATE.ref_for(hit, {"kind": "private-hit-test"}) if hit else None
    except Exception as error:
        raise AgentError("unsupported", "semantic input hit test is unavailable") from error
    if hit_ref != ref:
        raise AgentError(
            "precondition_failed",
            "private hit test did not resolve to the same semantic target",
        )
    try:
        pyatspi.Registry.generateMouseEvent(center_x, center_y, "b1c")
    except Exception as error:
        raise AgentError(
            "uncertain",
            "private semantic input failed after event dispatch began",
            side_effect_state="unknown",
        ) from error
    return {
        "execution_path": "semantic_input",
        "target_ref": ref,
        "private_hit_test": "matched",
    }


def _atspi_action_index(
    interface: Any, semantic_action: str, requested: str
) -> int:
    candidates = [
        _text(interface.getName(index), 128)
        for index in range(interface.nActions)
    ]
    normalized = [
        " ".join(re.sub(r"[-_.]+", " ", name.casefold()).split())
        for name in candidates
    ]
    if requested:
        needle = " ".join(
            re.sub(r"[-_.]+", " ", requested.casefold()).split()
        )
        matches = [index for index, name in enumerate(normalized) if name == needle]
    else:
        vocabulary = {
            "invoke": {
                "click", "press", "activate", "invoke", "open", "link open",
                "open link", "jump", "do default", "dodefault",
            },
            "toggle": {"click", "press", "toggle"},
            "check": {"click", "press", "check", "toggle"},
            "uncheck": {"click", "press", "uncheck", "toggle"},
            "expand": {"expand"},
            "collapse": {"collapse"},
            "dismiss": {"dismiss", "close", "window close"},
            "activate_window": {"activate"},
            "close_window": {"close"},
        }.get(semantic_action, set())
        matches = [index for index, name in enumerate(normalized) if name in vocabulary]
    if not matches:
        raise AgentError("unsupported", "target advertises no matching semantic action")
    if len(matches) != 1:
        raise AgentError(
            "ambiguous", "multiple advertised actions match; name one explicitly"
        )
    return matches[0]


def _chooser_descendants(root: Any) -> list[Any]:
    output: list[Any] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 24 or len(output) >= MAX_RECORDS:
            return
        output.append(node)
        count = int(_safe_attr(node, "childCount", 0) or 0)
        for index in range(max(0, count)):
            try:
                visit(node[index], depth + 1)
            except Exception:
                continue

    visit(root, 0)
    return output


def _chooser_actionable_named(root: Any, names: set[str]) -> tuple[Any, list[str]] | None:
    normalized_names = {" ".join(value.casefold().split()) for value in names}
    for node in _chooser_descendants(root):
        labels = {
            " ".join(_text(_safe_attr(node, "name", "")).casefold().split()),
            " ".join(_accessible_text(node).casefold().split()),
        }
        if not labels.intersection(normalized_names):
            continue
        candidate = node
        for _ in range(5):
            actions = _actions(candidate)
            action_verbs = {
                " ".join(re.sub(r"[-_.]+", " ", action.casefold()).split()).split()[-1]
                for action in actions if action.strip()
            }
            if action_verbs.intersection({
                "activate", "click", "press", "select", "open",
            }):
                return candidate, actions
            candidate = _safe_attr(candidate, "parent")
            if candidate is None or candidate is root:
                break
    return None


def _chooser_actionable_summary(root: Any) -> list[dict[str, Any]]:
    """Return bounded semantic diagnostics for chooser route failures."""

    summary: list[dict[str, Any]] = []
    pyatspi = _atspi_module()
    for node in _chooser_descendants(root):
        actions = _actions(node)
        if not actions:
            continue
        summary.append({
            "role": _role_name(node),
            "name": _text(_safe_attr(node, "name", ""), 80),
            "text": _accessible_text(node)[:80],
            "actions": actions[:5],
            "states": {
                key: value for key, value in _states(node, pyatspi).items()
                if key in {"enabled", "visible", "showing", "selected"}
            },
        })
        if len(summary) >= 12:
            break
    return summary


def _invoke_chooser_node(node: Any, actions: list[str], *, final_item: bool = False) -> bool:
    normalized = [
        " ".join(re.sub(r"[-_.]+", " ", value.casefold()).split())
        for value in actions
    ]
    verbs = [value.split()[-1] if value.split() else "" for value in normalized]
    priorities = (
        ("select", "click", "press", "activate", "open", "link open", "dodefault")
        if final_item else
        ("open", "link open", "activate", "click", "press", "select", "dodefault")
    )
    index = next(
        (verbs.index(value) for value in priorities if value in verbs),
        None,
    )
    if index is None:
        raise AgentError(
            "unsupported",
            "file chooser item has no unambiguous action "
            f"(name={_text(_safe_attr(node, 'name', ''), 200)!r}, actions={actions[:10]!r})",
        )
    try:
        applied = node.queryAction().doAction(index)
    except Exception as error:
        # GTK portals sometimes replace their AT-SPI object tree while a
        # folder link is opening.  The old bus recipient then disconnects even
        # though navigation succeeded.  The caller must re-query and prove
        # progress before continuing; no blind replay is allowed.
        if "recipient disconnected" in str(error).casefold():
            return False
        raise AgentError(
            "uncertain",
            "file chooser action raised after dispatch began "
            f"({type(error).__name__}: {_text(error, 300)})",
            side_effect_state="unknown",
        ) from error
    if not applied:
        raise AgentError(
            "uncertain", "file chooser action returned false after dispatch began",
            side_effect_state="unknown",
        )
    return True


def _current_file_chooser_target() -> Any:
    last_count = 0
    # GTK replaces most of the AT-SPI subtree while changing folders.  During
    # that realization window the old object is gone and the new dialog proxy
    # may not yet be visible; wait for a unique live replacement instead of
    # treating a normal navigation transition as an uncertain mutation.
    for _ in range(20):
        # A chooser is a top-level surface. Walking every descendant of every
        # application on each retry can exceed the 30-second guest transport
        # deadline; use the same shallow surface probe as os.file_choosers.
        records, _revision = _walk_accessibility(max_depth=2)
        # Folder navigation can temporarily clear GTK's VISIBLE state on the
        # dialog proxy while retaining one live AT-SPI chooser object.  This
        # private progression probe accepts that unique role/name identity;
        # the next expected child or confirmation action still has to prove
        # that navigation actually advanced.
        choosers = [
            record for record in records
            if _is_file_chooser_record(record, require_visible=False)
        ]
        last_count = len(choosers)
        if len(choosers) == 1:
            return _resolve(choosers[0]["ref"])
        if len(choosers) > 1:
            break
        time.sleep(0.25)
    raise AgentError(
        "ambiguous" if last_count > 1 else "uncertain",
        "native file chooser no longer resolves uniquely after navigation",
        retryable=last_count > 1,
        side_effect_state="none" if last_count > 1 else "unknown",
    )


def _choose_file_path_semantic_input(target: Any, path: Path) -> dict[str, Any]:
    """Use GTK's exact-location entry when a portal tree is not showing.

    Some GNOME portal choosers expose a truthful visible modal surface but
    mark their entire AT-SPI child tree as not showing/disabled.  In that
    state component traversal cannot be truthful.  The private fallback uses
    the chooser's standard exact-location entry and verifies that the uniquely
    active modal closed; neither keys nor native window identity are exposed.
    """

    target_title = _normalized_window_title(_safe_attr(target, "name", ""))
    wm = _wm_active_window()
    if wm is None or _normalized_window_title(wm.get("title")) != target_title:
        if not _wm_activate_accessible(target):
            raise AgentError(
                "no_effect", "file chooser is not the unique active modal",
                side_effect_state="none",
            )
    pyatspi = _atspi_module()
    registry = pyatspi.Registry
    control_index = int(getattr(pyatspi, "MODIFIER_CONTROL", 2))
    control_mask = 1 << control_index
    lock_modifiers = getattr(pyatspi, "KEY_LOCKMODIFIERS", 5)
    unlock_modifiers = getattr(pyatspi, "KEY_UNLOCKMODIFIERS", 6)
    key_string = getattr(pyatspi, "KEY_STRING", 4)
    key_sym = getattr(pyatspi, "KEY_SYM", 3)
    locked = False
    try:
        locked = registry.generateKeyboardEvent(
            control_mask, None, lock_modifiers,
        ) is not False
        if not locked or registry.generateKeyboardEvent(0, "l", key_string) is False:
            raise AgentError(
                "no_effect", "file chooser exact-location entry is unavailable",
                side_effect_state="none",
            )
    except AgentError:
        raise
    except Exception as error:
        raise AgentError(
            "no_effect", "file chooser exact-location entry is unavailable",
            side_effect_state="none",
        ) from error
    finally:
        if locked:
            try:
                registry.generateKeyboardEvent(
                    control_mask, None, unlock_modifiers,
                )
            except Exception:
                pass
    time.sleep(0.1)
    try:
        if registry.generateKeyboardEvent(0, str(path), key_string) is False:
            raise RuntimeError("path string synthesis returned false")
        if registry.generateKeyboardEvent(0xFF0D, None, key_sym) is False:
            raise RuntimeError("Return synthesis returned false")
    except Exception as error:
        raise AgentError(
            "uncertain", "file chooser path confirmation failed after entry began",
            side_effect_state="unknown",
        ) from error
    for _ in range(5):
        current = _wm_active_window()
        if current is None or _normalized_window_title(current.get("title")) != target_title:
            return {
                "execution_path": "semantic_input",
                "path": str(path),
                "chooser_closed": True,
                "verification": "active_modal_changed",
            }
        time.sleep(0.1)
    # GTK's location entry uses the first Return to accept a complete path and
    # may require a second confirmation to choose the resolved file.  Repeat
    # only after proving that one live editable entry contains the exact path.
    current_target = _current_file_chooser_target()
    exact_entries = [
        node for node in _chooser_descendants(current_target)
        if _accessible_text(node) == str(path)
        and _states(node, pyatspi).get("editable") is True
    ]
    if len(exact_entries) != 1:
        raise AgentError(
            "uncertain",
            "file chooser retained the modal without an exact populated location",
            side_effect_state="unknown",
        )
    try:
        if registry.generateKeyboardEvent(0xFF0D, None, key_sym) is False:
            raise RuntimeError("second Return synthesis returned false")
    except Exception as error:
        raise AgentError(
            "uncertain", "file chooser final confirmation failed",
            side_effect_state="unknown",
        ) from error
    for _ in range(30):
        current = _wm_active_window()
        if current is None or _normalized_window_title(current.get("title")) != target_title:
            return {
                "execution_path": "semantic_input",
                "path": str(path),
                "chooser_closed": True,
                "verification": "exact_location_then_active_modal_changed",
            }
        time.sleep(0.1)
    raise AgentError(
        "uncertain", "file chooser remained active after exact path confirmation",
        side_effect_state="unknown",
    )


def _choose_file_path(target: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Choose one existing guest path through a live native file chooser."""
    path = _safe_path(arguments.get("path")).resolve()
    if not path.exists():
        raise AgentError("not_found", "file chooser path does not exist")
    role = _role_name(target).casefold()
    if role not in {"file chooser", "file chooser dialog", "dialog"}:
        raise AgentError("precondition_failed", "target is not a file chooser")

    states = _states(target, _atspi_module())
    if (
        states.get("visible") is True
        and states.get("modal") is True
        and states.get("showing") is False
    ):
        return _choose_file_path_semantic_input(target, path)

    home = Path.home().resolve()
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise AgentError(
            "representation_gap",
            "semantic chooser traversal currently requires a path under the guest home",
        ) from error
    components = list(relative.parts)
    if not components:
        raise AgentError("invalid_request", "choosing the guest home itself is unsupported")

    for index, component in enumerate(components):
        current = _chooser_actionable_named(target, {component})
        if current is None:
            raise AgentError(
                "not_found",
                f"file chooser does not expose path component {component!r}; "
                f"actionable={_chooser_actionable_summary(target)!r}",
                retryable=True,
            )
        node, actions = current
        dispatched = _invoke_chooser_node(
            node, actions, final_item=index == len(components) - 1
        )
        time.sleep(0.35)
        target = _current_file_chooser_target()
        if not dispatched:
            next_names = (
                {components[index + 1]}
                if index + 1 < len(components)
                else {"select", "open", "choose", "save", "add"}
            )
            if _chooser_actionable_named(target, next_names) is None:
                raise AgentError(
                    "uncertain",
                    "file chooser replaced its accessibility object without proving progress",
                    side_effect_state="unknown",
                )

    confirm = _chooser_actionable_named(
        target, {"select", "open", "choose", "save", "add"}
    )
    if confirm is None:
        raise AgentError("not_found", "file chooser exposes no semantic confirmation action")
    _invoke_chooser_node(confirm[0], confirm[1])
    for _ in range(30):
        try:
            if _states(target, _atspi_module()).get("showing") is False:
                break
        except Exception:
            break
        time.sleep(0.1)
    else:
        raise AgentError(
            "uncertain", "file chooser remained open after confirmation",
            side_effect_state="unknown",
        )
    return {
        "execution_path": "accessibility", "path": str(path),
        "chooser_closed": True,
    }


def _activate_window(ref: str, target: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Activate a top-level surface without falling back to pointer input.

    A direct AT-SPI Action is authoritative when the application exposes one.
    Some common window managers expose only Component.grabFocus, so use that
    only when no matching direct action exists and no mutation has started.
    """

    pyatspi = _atspi_module()
    before = _states(target, pyatspi)
    wm_available, active_surface_ref = _authoritative_active_surface_ref()
    if (
        (wm_available and active_surface_ref == ref)
        or (
            not wm_available
            and (before.get("active") is True or before.get("focused") is True)
        )
    ):
        raise AgentError(
            "no_effect", "window is already active or focused",
            side_effect_state="none",
        )

    requested = str(arguments.get("advertised_action", ""))
    interface = None
    try:
        interface = target.queryAction()
    except Exception:
        pass

    activation_path = "atspi_action"
    action_index: int | None = None
    if interface is not None:
        try:
            action_index = _atspi_action_index(
                interface, "activate_window", requested,
            )
        except AgentError as error:
            if error.code != "unsupported":
                raise

    if action_index is not None:
        try:
            applied = interface.doAction(action_index)
        except Exception as error:
            raise AgentError(
                "uncertain",
                "AT-SPI window activation raised after mutation began",
                side_effect_state="unknown",
            ) from error
        if not applied:
            raise AgentError(
                "uncertain",
                "AT-SPI window activation returned false after mutation began",
                side_effect_state="unknown",
            )
    else:
        activation_path = "component_focus"
        try:
            component = target.queryComponent()
        except Exception as error:
            raise AgentError(
                "unsupported",
                "target has neither an activation action nor a component focus interface",
            ) from error
        try:
            focused = component.grabFocus()
        except Exception as error:
            raise AgentError(
                "uncertain",
                "AT-SPI component focus raised after mutation began",
                side_effect_state="unknown",
            ) from error
        if focused is False:
            if not _wm_activate_accessible(target):
                raise AgentError(
                    "no_effect",
                    "window exposes no unique semantic activation route",
                    side_effect_state="none",
                )
            activation_path = "wm_semantic"

    # GTK and the window manager publish focus changes asynchronously. Always
    # give the authoritative WM a bounded propagation window: when WM state is
    # available there are intentionally no AT-SPI ``state_keys``, so gating the
    # sleep on those keys collapses the loop into six immediate stale reads.
    def wait_for_activation(
        path: str,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        verification_supported = False
        wm_authority_seen = False
        for attempt in range(8):
            wm_available, active_surface_ref = _authoritative_active_surface_ref()
            if wm_available:
                wm_authority_seen = True
                verification_supported = True
                if active_surface_ref == ref:
                    return ({
                        "execution_path": (
                            "semantic_input" if "wm_semantic" in path
                            else "accessibility"
                        ),
                        "target_ref": ref,
                        "action": "activate_window",
                        "status": "applied",
                        "changed": True,
                        "activation_path": path,
                        "verification": "window_manager",
                    }, verification_supported, wm_authority_seen)
            else:
                after = _states(target, pyatspi)
                state_keys = {"active", "focused"}.intersection(after)
                verification_supported = verification_supported or bool(state_keys)
                if after.get("active") is True or after.get("focused") is True:
                    return ({
                        "execution_path": (
                            "semantic_input" if "wm_semantic" in path
                            else "accessibility"
                        ),
                        "target_ref": ref,
                        "action": "activate_window",
                        "status": "applied",
                        "changed": True,
                        "activation_path": path,
                        "verification": "active_or_focused",
                    }, verification_supported, wm_authority_seen)
                if "wm_semantic" in path:
                    wm = _wm_active_window()
                    target_title = _normalized_window_title(
                        _safe_attr(target, "name", "")
                    )
                    target_pid = _accessible_process_id(target)
                    if wm is not None and (
                        (
                            target_pid is not None
                            and wm.get("pid") is not None
                            and int(wm["pid"]) == target_pid
                        )
                        or (
                            bool(target_title)
                            and _normalized_window_title(wm.get("title"))
                            == target_title
                        )
                    ):
                        return ({
                            "execution_path": "semantic_input",
                            "target_ref": ref,
                            "action": "activate_window",
                            "status": "applied",
                            "changed": True,
                            "activation_path": path,
                            "verification": "private_window_identity",
                        }, verification_supported, wm_authority_seen)
            if attempt < 7:
                time.sleep(0.1)
        return None, verification_supported, wm_authority_seen

    confirmed, verification_supported, wm_authority_seen = wait_for_activation(
        activation_path
    )
    if confirmed is not None:
        return confirmed

    # Some applications return True from Component.grabFocus (and some AT-SPI
    # activation actions return True) while the window remains behind another
    # application. If WM authority disproves activation, retry the same unique
    # semantic target through the private WM route. Window activation is
    # idempotent; this never guesses among multiple native candidates.
    if wm_authority_seen and "wm_semantic" not in activation_path:
        if _wm_activate_accessible(target):
            activation_path += "_then_wm_semantic"
            confirmed, fallback_supported, _fallback_wm_seen = wait_for_activation(
                activation_path
            )
            verification_supported = verification_supported or fallback_supported
            if confirmed is not None:
                return confirmed

    if verification_supported:
        raise AgentError(
            "postcondition_failed",
            "window did not report active or focused after activation",
            side_effect_state="unknown",
        )
    raise AgentError(
        "uncertain",
        "window activation was dispatched but active/focused state is unavailable",
        side_effect_state="unknown",
    )


def _ui_action(ref: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    target = _resolve(ref)
    if action == "choose_path":
        return _choose_file_path(target, arguments)
    if action == "activate_window":
        return _activate_window(ref, target, arguments)
    if action == "focus":
        try:
            target.queryComponent().grabFocus()
        except Exception as error:
            raise AgentError("unsupported", "target has no focus interface") from error
    elif action in {"set_text", "replace_text"}:
        value = _text(arguments.get("value"), 64 * 1024)
        try:
            editable = target.queryEditableText()
            editable.setTextContents(value)
        except Exception as error:
            raise AgentError("unsupported", "target has no editable-text interface") from error
    elif action == "insert_text":
        value = _text(arguments.get("value"), 64 * 1024)
        position = int(arguments.get("position", 0))
        try:
            target.queryEditableText().insertText(position, value, len(value))
        except Exception as error:
            raise AgentError("unsupported", "target has no editable-text interface") from error
    elif action == "set_value":
        try:
            target.queryValue().currentValue = float(arguments["value"])
        except Exception as error:
            raise AgentError("unsupported", "target has no value interface") from error
    elif action in {"select", "clear_selection"}:
        try:
            selection = target.querySelection()
            if action == "clear_selection":
                selection.clearSelection()
            else:
                selection.selectChild(int(arguments.get("index", 0)))
        except Exception as error:
            raise AgentError("unsupported", "target has no selection interface") from error
    elif action == "scroll_into_view":
        try:
            component = target.queryComponent()
            scroll_type = getattr(pyatspi := _atspi_module(), "SCROLL_ANYWHERE", 0)
            if not component.scrollTo(scroll_type):
                raise AgentError("no_effect", "AT-SPI component did not scroll")
        except AgentError:
            raise
        except Exception as error:
            raise AgentError("unsupported", "target has no component scroll interface") from error
    elif action in {
        "invoke", "toggle", "check", "uncheck", "expand", "collapse", "dismiss",
        "close_window",
    }:
        state = _states(target, _atspi_module())
        if action == "check" and state.get("checked") is True:
            raise AgentError("no_effect", "target is already checked")
        if action == "uncheck" and state.get("checked") is False:
            raise AgentError("no_effect", "target is already unchecked")
        if action == "expand" and state.get("expanded") is True:
            raise AgentError("no_effect", "target is already expanded")
        if action == "collapse" and state.get("expanded") is False:
            raise AgentError("no_effect", "target is already collapsed")
        try:
            interface = target.queryAction()
        except Exception:
            if action not in {"invoke", "toggle", "check", "uncheck", "expand", "collapse"}:
                raise AgentError(
                    "unsupported", "target has no direct action interface"
                )
            fallback = _private_semantic_click(ref, target)
            return {**fallback, "action": action}
        requested = str(arguments.get("advertised_action", ""))
        index = _atspi_action_index(interface, action, requested)
        try:
            applied = interface.doAction(index)
        except Exception as error:
            raise AgentError(
                "uncertain",
                "AT-SPI action raised after mutation began",
                side_effect_state="unknown",
            ) from error
        if not applied:
            raise AgentError(
                "uncertain",
                "AT-SPI action returned false after mutation began",
                side_effect_state="unknown",
            )
    else:
        raise AgentError("unsupported", f"unsupported accessibility action: {action}")
    return {"execution_path": "accessibility", "target_ref": ref, "action": action}


_WRITER_PARAGRAPH_PROPERTIES = set(_WRITER_PARAGRAPH_PROPERTY_NAMES)
_WRITER_CHARACTER_PROPERTIES = set(_WRITER_CHARACTER_PROPERTY_NAMES)
_CALC_PROPERTIES = set(_CALC_PROPERTY_NAMES)
_DRAW_PROPERTIES = set(_DRAW_PROPERTY_NAMES)

_PARAGRAPH_ALIGNMENT = {"left": 0, "right": 1, "justify": 2, "center": 3}
_WRITER_CELL_PROPERTIES = (
    _WRITER_PARAGRAPH_PROPERTIES
    | _WRITER_CHARACTER_PROPERTIES
    | {"BackColor", "BackTransparent", "VertOrient"}
)


def _validate_uno_action_arguments(action: str, arguments: dict[str, Any]) -> None:
    """Fail closed if the guest receives arguments the advertised action ignores."""

    schema = _LIBREOFFICE_ACTION_SCHEMAS.get(action)
    if schema is None:
        raise AgentError("unsupported", f"unsupported LibreOffice action: {action}")
    properties = schema.get("properties") or {}
    unknown = sorted(set(arguments) - set(properties))
    if unknown and schema.get("additionalProperties") is False:
        raise AgentError(
            "invalid_request",
            f"{action} received unsupported arguments: {', '.join(unknown)}",
        )
    missing = [name for name in schema.get("required", ()) if name not in arguments]
    if missing:
        raise AgentError(
            "invalid_request", f"{action} is missing arguments: {', '.join(missing)}"
        )


def _replacement_text(arguments: dict[str, Any], action: str) -> str:
    has_text = "text" in arguments
    has_value = "value" in arguments
    if has_text == has_value:
        raise AgentError(
            "invalid_request", f"{action} requires exactly one of text or value"
        )
    value = arguments["text"] if has_text else arguments["value"]
    if not isinstance(value, str):
        raise AgentError("invalid_request", f"{action} replacement must be a string")
    return _text(value, 64 * 1024)


def _color_value(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AgentError("invalid_request", f"{field} must be #RRGGBB or integer RGB")
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return int(value[1:], 16)
    raise AgentError("invalid_request", f"{field} must be #RRGGBB or integer RGB")


def _alignment_value(value: Any, field: str) -> int:
    if not isinstance(value, str) or value.casefold() not in _PARAGRAPH_ALIGNMENT:
        raise AgentError(
            "invalid_request", f"{field} must be left, right, center, or justify"
        )
    return _PARAGRAPH_ALIGNMENT[value.casefold()]


def _values_equivalent(requested: Any, observed: Any) -> bool:
    if (
        isinstance(requested, (int, float)) and not isinstance(requested, bool)
        and isinstance(observed, (int, float)) and not isinstance(observed, bool)
    ):
        return abs(float(requested) - float(observed)) <= 0.001
    return requested == observed


def _matrix_matches(
    requested: tuple[tuple[Any, ...], ...],
    observed: Any,
    *, numeric_equivalence: bool,
) -> bool:
    if not isinstance(observed, (list, tuple)) or len(observed) != len(requested):
        return False
    for requested_row, observed_row in zip(requested, observed):
        if not isinstance(observed_row, (list, tuple)) or len(observed_row) != len(requested_row):
            return False
        for requested_value, observed_value in zip(requested_row, observed_row):
            if numeric_equivalence:
                if not _values_equivalent(requested_value, observed_value):
                    return False
            elif requested_value != observed_value:
                return False
    return True


def _set_properties(
    target: Any, properties: dict[str, Any], allowed: set[str]
) -> dict[str, dict[str, Any]]:
    if not properties:
        raise AgentError("invalid_request", "properties must be a non-empty object")
    unknown = sorted(set(properties) - allowed)
    if unknown:
        raise AgentError(
            "invalid_request", "unsupported or unsafe UNO properties: " + ", ".join(unknown)
        )
    evidence: dict[str, dict[str, Any]] = {}
    for name, value in properties.items():
        _uno_set_property(target, name, value)
        observed = _uno_property(target, name)
        if not _values_equivalent(value, observed):
            raise AgentError(
                "postcondition_failed",
                f"LibreOffice did not retain requested property {name}",
                side_effect_state="applied",
            )
        evidence[name] = {
            "requested": _uno_json(value),
            "observed": _uno_json(observed),
            "matched": True,
        }
    return evidence


def _set_whitelisted_properties(
    target: Any, arguments: dict[str, Any], allowed: set[str]
) -> dict[str, dict[str, Any]]:
    properties = arguments.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise AgentError("invalid_request", "properties must be a non-empty object")
    return _set_properties(target, dict(properties), allowed)


def _semantic_format_properties(
    arguments: dict[str, Any], *, alignment_key: str
) -> dict[str, Any]:
    properties = dict(arguments.get("properties") or {})
    aliases = {
        "font_size": ("CharHeight", lambda value: float(value)),
        "font_color": ("CharColor", lambda value: _color_value(value, "font_color")),
        "font_name": ("CharFontName", str),
        alignment_key: ("ParaAdjust", lambda value: _alignment_value(value, alignment_key)),
    }
    for field, (name, convert) in aliases.items():
        if field not in arguments:
            continue
        if name in properties:
            raise AgentError(
                "invalid_request", f"specify either {field} or properties.{name}, not both"
            )
        try:
            properties[name] = convert(arguments[field])
        except AgentError:
            raise
        except (TypeError, ValueError) as error:
            raise AgentError("invalid_request", f"invalid {field}") from error
    return properties


def _target_text(target: Any, document: Any, kind: str) -> str:
    value = _uno_property(target, "String", None)
    if value is not None:
        return _text(value, 64 * 1024)
    if target is document and kind == "writer":
        text = document.getText()
        if hasattr(text, "getString"):
            return _text(text.getString(), 64 * 1024)
        return _text(_uno_property(text, "String", ""), 64 * 1024)
    return ""


def _uno_action(ref: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with UNO_LOCK:
        _validate_uno_action_arguments(action, arguments)
        target = _resolve(ref)
        document = target if _uno_kind(target) != "unknown" else _uno_doc_for_object(target)
        kind = _uno_kind(document)
        detail: dict[str, Any] = {}

        if action == "activate":
            try:
                document.getCurrentController().getFrame().activate()
            except Exception as error:
                raise AgentError("unsupported", "document frame cannot be activated") from error
        elif action == "save":
            if not _uno_document_record(document).get("url"):
                raise AgentError("precondition_failed", "untitled document requires save_as")
            document.store()
            if bool(document.isModified()):
                raise AgentError(
                    "postcondition_failed", "document remains modified after save",
                    side_effect_state="applied",
                )
            detail["save_evidence"] = {
                "modified": False,
                "url": _text(document.getURL(), 4_096),
            }
        elif action in {"save_as", "export"}:
            path = arguments.get("path")
            if not isinstance(path, str):
                raise AgentError("invalid_request", f"{action} requires an absolute path")
            path_obj = _safe_path(path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            values: dict[str, Any] = {"Overwrite": True}
            filter_name = arguments.get("filter_name")
            if isinstance(filter_name, str) and filter_name:
                values["FilterName"] = filter_name
            url = _uno_file_url(str(path_obj))
            if action == "save_as":
                document.storeAsURL(url, _uno_property_values(values))
            else:
                document.storeToURL(url, _uno_property_values(values))
            if not path_obj.exists():
                raise AgentError("postcondition_failed", f"{action} did not create target artifact")
            detail.update({
                "path": str(path_obj), "size": path_obj.stat().st_size,
                "sha256": hashlib.sha256(path_obj.read_bytes()).hexdigest(),
                "save_evidence": {
                    "artifact_exists": True,
                    "modified": bool(document.isModified()),
                },
            })
        elif action in {"undo", "redo"}:
            try:
                manager = document.getUndoManager()
                if action == "undo":
                    if not manager.isUndoPossible():
                        raise AgentError("no_effect", "no undo action is available")
                    manager.undo()
                else:
                    if not manager.isRedoPossible():
                        raise AgentError("no_effect", "no redo action is available")
                    manager.redo()
            except AgentError:
                raise
            except Exception as error:
                raise AgentError("unsupported", "document undo manager unavailable") from error
        elif action == "reload":
            if bool(document.isModified()) and not bool(arguments.get("discard_changes", False)):
                raise AgentError(
                    "artifact_conflict", "reload would discard unsaved changes; pass discard_changes=true"
                )
            try:
                reload_document = document.reload
            except AttributeError as error:
                raise AgentError(
                    "unsupported",
                    "this LibreOffice document does not expose the reload interface",
                    side_effect_state="none",
                ) from error
            try:
                reload_document()
            except Exception as error:
                raise AgentError(
                    "uncertain",
                    "LibreOffice reload failed after invocation began",
                    side_effect_state="unknown",
                ) from error
        elif action in {"replace_text", "set_text", "delete_text"}:
            value = "" if action == "delete_text" else _replacement_text(arguments, action)
            try:
                _uno_set_property(target, "String", value)
            except AgentError:
                if target is document and kind == "writer":
                    cursor = document.getText().createTextCursor()
                    cursor.gotoStart(False)
                    cursor.gotoEnd(True)
                    cursor.setString(value)
                else:
                    raise
            observed_text = _target_text(target, document, kind)
            if observed_text != value:
                raise AgentError(
                    "postcondition_failed", "LibreOffice did not retain requested text",
                    side_effect_state="applied",
                )
            detail["text_evidence"] = {
                "requested": value, "observed": observed_text, "matched": True,
            }
            if action != "delete_text":
                properties = _semantic_format_properties(
                    arguments, alignment_key="paragraph_alignment"
                )
                if properties:
                    detail["property_evidence"] = _set_properties(
                        target,
                        properties,
                        _WRITER_CHARACTER_PROPERTIES | _WRITER_PARAGRAPH_PROPERTIES,
                    )
        elif action == "insert_text":
            value = _text(arguments.get("value"), 64 * 1024)
            if not value:
                raise AgentError("no_effect", "insert_text value is empty")
            try:
                text = target.getText() if hasattr(target, "getText") else document.getText()
                cursor = text.createTextCursorByRange(target) if target is not document else text.createTextCursor()
                position = str(arguments.get("position") or "end")
                if position == "start":
                    cursor.gotoStart(False)
                elif position == "end":
                    cursor.gotoEnd(False)
                elif isinstance(arguments.get("offset"), int):
                    cursor.gotoStart(False)
                    cursor.goRight(int(arguments["offset"]), False)
                text.insertString(cursor, value, False)
            except Exception as error:
                raise AgentError("unsupported", "target does not support semantic text insertion") from error
        elif action == "replace_with_paragraphs":
            values = arguments.get("paragraphs")
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) for value in values)
            ):
                raise AgentError(
                    "invalid_request", "replace_with_paragraphs requires a non-empty string array"
                )
            try:
                text = target.getText()
                target.String = values[0]
                cursor = text.createTextCursorByRange(target)
                cursor.gotoEndOfParagraph(False)
                # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK == 0.
                # Keep the literal so the bundle imports before UNO is loaded.
                for value in values[1:]:
                    text.insertControlCharacter(cursor, 0, False)
                    if value:
                        text.insertString(cursor, value, False)
            except Exception as error:
                raise AgentError(
                    "unsupported", "target does not support structural paragraph replacement"
                ) from error
            observed = [
                str(record.get("text") or "")
                for record in _writer_paragraph_records(document)
            ]
            matched = any(
                observed[index:index + len(values)] == values
                for index in range(max(0, len(observed) - len(values) + 1))
            )
            if not matched:
                raise AgentError(
                    "postcondition_failed",
                    "Writer did not retain the requested paragraph sequence",
                    side_effect_state="applied",
                )
            detail["paragraph_evidence"] = {
                "requested": values,
                "matched": True,
                "observed_paragraph_count": len(observed),
            }
        elif action == "insert_paragraphs":
            values = arguments.get("paragraphs")
            position = arguments.get("position")
            if (
                not isinstance(values, list)
                or not values
                or len(values) > 2_000
                or any(not isinstance(value, str) for value in values)
            ):
                raise AgentError(
                    "invalid_request", "insert_paragraphs requires a bounded non-empty string array"
                )
            if sum(len(value) for value in values) > 64 * 1024:
                raise AgentError("budget_exhausted", "inserted paragraph text exceeds 64 KiB")
            if position not in {"before", "after", "end"}:
                raise AgentError(
                    "invalid_request", "insert_paragraphs position must be before, after, or end"
                )
            before_records = _writer_paragraph_records(document)
            matches = [
                index for index, record in enumerate(before_records)
                if record.get("ref") == ref
            ]
            if len(matches) != 1:
                raise AgentError(
                    "stale_ref",
                    "target paragraph no longer resolves uniquely in the Writer document",
                    retryable=True,
                )
            if len(before_records) + len(values) > MAX_RECORDS:
                raise AgentError(
                    "budget_exhausted", "resulting Writer document exceeds paragraph verification limit"
                )
            target_index = matches[0]
            insertion_index = (
                len(before_records)
                if position == "end"
                else target_index if position == "before" else target_index + 1
            )
            before_text = [str(record.get("text") or "") for record in before_records]
            expected = before_text[:insertion_index] + values + before_text[insertion_index:]
            try:
                text = document.getText()
                if position == "end":
                    cursor = text.createTextCursor()
                    cursor.gotoEnd(False)
                    for value in values:
                        text.insertControlCharacter(cursor, 0, False)
                        if value:
                            text.insertString(cursor, value, False)
                else:
                    cursor = text.createTextCursorByRange(target)
                    if position == "before":
                        cursor.gotoStartOfParagraph(False)
                        for value in values:
                            if value:
                                text.insertString(cursor, value, False)
                            text.insertControlCharacter(cursor, 0, False)
                    else:
                        cursor.gotoEndOfParagraph(False)
                        for value in values:
                            text.insertControlCharacter(cursor, 0, False)
                            if value:
                                text.insertString(cursor, value, False)
            except Exception as error:
                raise AgentError(
                    "unsupported", "target does not support structural paragraph insertion"
                ) from error
            observed_records = _writer_paragraph_records(document)
            observed = [str(record.get("text") or "") for record in observed_records]
            if observed != expected:
                raise AgentError(
                    "postcondition_failed",
                    "Writer did not retain the exact requested paragraph insertion",
                    side_effect_state="applied",
                )
            detail["paragraph_evidence"] = {
                "operation": "insert", "position": position,
                "insertion_index": insertion_index,
                "inserted_count": len(values),
                "requested_sha256": _json_hash(values),
                "matched": True,
                "observed_paragraph_count": len(observed),
            }
        elif action == "set_paragraph_properties":
            properties = dict(arguments.get("properties") or {})
            if "alignment" in arguments:
                if "ParaAdjust" in properties:
                    raise AgentError(
                        "invalid_request",
                        "specify either alignment or properties.ParaAdjust, not both",
                    )
                properties["ParaAdjust"] = _alignment_value(arguments["alignment"], "alignment")
            detail["property_evidence"] = _set_properties(
                target, properties, _WRITER_PARAGRAPH_PROPERTIES
            )
        elif action in {"set_character_properties", "set_style_properties"}:
            detail["properties"] = _set_whitelisted_properties(
                target, arguments, _WRITER_CHARACTER_PROPERTIES | _WRITER_PARAGRAPH_PROPERTIES
            )
        elif action == "set_table_cell":
            cell_name = arguments.get("cell")
            if not isinstance(cell_name, str):
                raise AgentError("invalid_request", "set_table_cell requires cell name")
            try:
                cell = target.getCellByName(cell_name)
            except AgentError:
                raise
            except Exception as error:
                raise AgentError("not_found", f"table cell does not exist: {cell_name}") from error
            has_text = "text" in arguments or "value" in arguments
            properties = _semantic_format_properties(
                arguments, alignment_key="paragraph_alignment"
            )
            if "character_color" in arguments:
                if "font_color" in arguments or "CharColor" in properties:
                    raise AgentError(
                        "invalid_request",
                        "specify only one of character_color, font_color, or properties.CharColor",
                    )
                properties["CharColor"] = _color_value(
                    arguments["character_color"], "character_color"
                )
            if "background_color" in arguments:
                if "BackColor" in properties:
                    raise AgentError(
                        "invalid_request",
                        "specify either background_color or properties.BackColor, not both",
                    )
                properties["BackColor"] = _color_value(
                    arguments["background_color"], "background_color"
                )
            if not has_text and not properties:
                raise AgentError(
                    "invalid_request", "set_table_cell requires text/value or formatting"
                )
            if has_text:
                value = _replacement_text(arguments, action)
                _uno_set_property(cell, "String", value)
                observed = _text(_uno_property(cell, "String", ""), 64 * 1024)
                if observed != value:
                    raise AgentError(
                        "postcondition_failed", "LibreOffice did not retain requested cell text",
                        side_effect_state="applied",
                    )
                detail["text_evidence"] = {
                    "requested": value, "observed": observed, "matched": True,
                }
            if properties:
                detail["property_evidence"] = _set_properties(
                    cell, properties, _WRITER_CELL_PROPERTIES
                )
            detail["cell"] = cell_name
        elif action == "set_value":
            value = arguments.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AgentError("invalid_request", "set_value requires a number")
            _uno_set_property(target, "Value", float(value))
        elif action == "set_formula":
            formula = arguments.get("formula")
            if not isinstance(formula, str):
                raise AgentError("invalid_request", "set_formula requires a string")
            _uno_set_property(target, "Formula", formula)
        elif action in {"set_cell_properties", "set_range_properties"}:
            detail["property_evidence"] = _set_whitelisted_properties(
                target, arguments, _CALC_PROPERTIES
            )
        elif action in {"set_range_values", "set_range_formulas"}:
            key = "values" if action == "set_range_values" else "formulas"
            values = arguments.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(row, list) for row in values):
                raise AgentError("invalid_request", f"{key} must be a non-empty rectangular array")
            width = len(values[0])
            if width == 0 or any(len(row) != width for row in values):
                raise AgentError("invalid_request", f"{key} must be rectangular")
            if len(values) * width > MAX_RECORDS:
                raise AgentError("budget_exhausted", f"{key} exceeds the 5000-cell mutation limit")
            data = tuple(tuple(cell for cell in row) for row in values)
            try:
                if action == "set_range_values":
                    target.setDataArray(data)
                else:
                    target.setFormulaArray(data)
            except Exception as error:
                raise AgentError("invalid_request", f"{key} shape does not match target range") from error
            try:
                observed = (
                    target.getDataArray()
                    if action == "set_range_values" else target.getFormulaArray()
                )
            except Exception as error:
                raise AgentError(
                    "postcondition_failed",
                    f"LibreOffice could not re-read the mutated range {key}",
                    side_effect_state="applied",
                ) from error
            matched = _matrix_matches(
                data, observed, numeric_equivalence=action == "set_range_values"
            )
            if not matched:
                raise AgentError(
                    "postcondition_failed",
                    f"LibreOffice did not retain the exact requested range {key}",
                    side_effect_state="applied",
                )
            observed_json = _uno_json(observed)
            detail["range_evidence"] = {
                "kind": key, "rows": len(data), "columns": width,
                "requested_sha256": _json_hash(_uno_json(data)),
                "observed_sha256": _json_hash(observed_json),
                "matched": True,
            }
        elif action == "fill":
            direction = str(arguments.get("direction") or "down")
            try:
                series = target
                if direction == "down":
                    series.fillAuto(0, int(arguments.get("source_count", 1)))
                elif direction == "right":
                    series.fillAuto(1, int(arguments.get("source_count", 1)))
                else:
                    raise AgentError("invalid_request", "fill direction must be down or right")
            except AgentError:
                raise
            except Exception as error:
                raise AgentError("unsupported", "range does not support fill") from error
        elif action == "rename_sheet":
            name = _text(arguments.get("name"), 256).strip()
            if not name:
                raise AgentError("invalid_request", "rename_sheet requires a name")
            target.setName(name)
        elif action == "reorder_sheet":
            name = _text(_uno_property(target, "Name", ""), 256)
            document.getSheets().moveByName(name, int(arguments.get("index", 0)))
        elif action == "add_sheet":
            name = _text(arguments.get("name"), 256).strip()
            document.getSheets().insertNewByName(name, int(arguments.get("index", document.getSheets().getCount())))
        elif action == "delete_sheet":
            name = _text(_uno_property(target, "Name", arguments.get("name", "")), 256)
            document.getSheets().removeByName(name)
        elif action in {"insert_rows", "delete_rows", "insert_columns", "delete_columns"}:
            index = int(arguments.get("index", 0))
            count = int(arguments.get("count", 1))
            if index < 0 or count < 1 or count > 10_000:
                raise AgentError("invalid_request", "invalid row/column index or count")
            collection = target.getRows() if "rows" in action else target.getColumns()
            if action.startswith("insert"):
                collection.insertByIndex(index, count)
            else:
                collection.removeByIndex(index, count)
        elif action in {"freeze", "unfreeze"}:
            controller = document.getCurrentController()
            columns = 0 if action == "unfreeze" else int(arguments.get("columns", 0))
            rows = 0 if action == "unfreeze" else int(arguments.get("rows", 0))
            controller.freezeAtPosition(columns, rows)
        elif action == "create_chart":
            if kind != "calc":
                raise AgentError("unsupported", "create_chart requires a spreadsheet")
            sheet_name = str(arguments.get("sheet") or _calc_sheet_records(document)[0]["name"])
            sheet = document.getSheets().getByName(sheet_name)
            source_range, _sheet, _requested = _calc_range(document, {
                "sheet": sheet_name, "range": arguments.get("range", "A1:B2")
            })
            uno, _context, _desktop = _uno_connection()
            rectangle = uno.createUnoStruct("com.sun.star.awt.Rectangle")
            rectangle.X = int(arguments.get("x", 1_000)); rectangle.Y = int(arguments.get("y", 1_000))
            rectangle.Width = int(arguments.get("width", 12_000)); rectangle.Height = int(arguments.get("height", 8_000))
            name = _text(arguments.get("name") or "Chart", 256)
            sheet.getCharts().addNewByName(
                name, rectangle, (source_range.getRangeAddress(),),
                bool(arguments.get("column_header", True)), bool(arguments.get("row_header", True)),
            )
        elif action == "delete_chart":
            name = _text(arguments.get("name") or _uno_property(target, "Name", ""), 256)
            sheet_name = _text(arguments.get("sheet"), 256)
            if not sheet_name:
                raise AgentError("invalid_request", "delete_chart requires sheet")
            document.getSheets().getByName(sheet_name).getCharts().removeByName(name)
        elif action == "create_slide":
            pages = document.getDrawPages()
            index = int(arguments.get("index", pages.getCount()))
            page = pages.insertNewByIndex(index)
            detail["slide_ref"] = _uno_ref(page, "presentation.slide", {"index": index})
        elif action == "delete_slide":
            document.getDrawPages().remove(target)
        elif action == "reorder_slide":
            raise AgentError(
                "representation_gap", "this LibreOffice version exposes no atomic semantic slide-move API"
            )
        elif action == "add_text_shape":
            page = target
            shape = document.createInstance("com.sun.star.drawing.TextShape")
            uno, _context, _desktop = _uno_connection()
            position = uno.createUnoStruct("com.sun.star.awt.Point")
            position.X = int(arguments.get("x", 1_000)); position.Y = int(arguments.get("y", 1_000))
            size = uno.createUnoStruct("com.sun.star.awt.Size")
            size.Width = int(arguments.get("width", 12_000)); size.Height = int(arguments.get("height", 3_000))
            shape.setPosition(position); shape.setSize(size)
            page.add(shape)
            _uno_set_property(shape, "String", _text(arguments.get("text"), 64 * 1024))
            detail["shape_ref"] = _uno_ref(shape, "presentation.shape", {"created": secrets.token_hex(6)})
        elif action in {"set_shape_properties", "set_slide_properties"}:
            properties = _semantic_format_properties(
                arguments, alignment_key="paragraph_alignment"
            )
            if properties:
                detail["property_evidence"] = _set_properties(
                    target, properties, _DRAW_PROPERTIES
                )
            elif "position" not in arguments and "size" not in arguments:
                raise AgentError(
                    "invalid_request",
                    "shape/slide update requires properties, position, or size",
                )
            if "position" in arguments:
                if not isinstance(arguments["position"], dict):
                    raise AgentError("invalid_request", "position must be an object")
                unknown = sorted(set(arguments["position"]) - {"x", "y"})
                if unknown:
                    raise AgentError(
                        "invalid_request",
                        "position received unsupported fields: " + ", ".join(unknown),
                    )
                position = target.getPosition()
                position.X = int(arguments["position"].get("x", position.X))
                position.Y = int(arguments["position"].get("y", position.Y))
                requested_position = (int(position.X), int(position.Y))
                target.setPosition(position)
                observed = target.getPosition()
                if (int(observed.X), int(observed.Y)) != requested_position:
                    raise AgentError(
                        "postcondition_failed", "LibreOffice did not retain requested position",
                        side_effect_state="applied",
                    )
                detail["position_evidence"] = {
                    "x": int(observed.X), "y": int(observed.Y), "matched": True,
                }
            if "size" in arguments:
                if not isinstance(arguments["size"], dict):
                    raise AgentError("invalid_request", "size must be an object")
                unknown = sorted(set(arguments["size"]) - {"width", "height"})
                if unknown:
                    raise AgentError(
                        "invalid_request",
                        "size received unsupported fields: " + ", ".join(unknown),
                    )
                size = target.getSize()
                size.Width = int(arguments["size"].get("width", size.Width))
                size.Height = int(arguments["size"].get("height", size.Height))
                requested_size = (int(size.Width), int(size.Height))
                target.setSize(size)
                observed = target.getSize()
                if (int(observed.Width), int(observed.Height)) != requested_size:
                    raise AgentError(
                        "postcondition_failed", "LibreOffice did not retain requested size",
                        side_effect_state="applied",
                    )
                detail["size_evidence"] = {
                    "width": int(observed.Width), "height": int(observed.Height),
                    "matched": True,
                }
        elif action == "delete_shape":
            # DrawPage implements XShapes.remove; locate the owning slide.
            removed = False
            pages = document.getDrawPages()
            for index in range(pages.getCount()):
                page = pages.getByIndex(index)
                try:
                    page.remove(target)
                    removed = True
                    break
                except Exception:
                    continue
            if not removed:
                raise AgentError("not_found", "shape is no longer owned by a slide")
        elif action in {"sort", "filter", "set_chart_properties"}:
            raise AgentError(
                "representation_gap", f"{action} requires a richer typed descriptor in this LibreOffice build"
            )
        else:
            raise AgentError("unsupported", f"unsupported LibreOffice action: {action}")

        document_record = _uno_document_record(document)
        _uno_document_records()
        return {
            "execution_path": "native_api", "target_ref": ref, "action": action,
            "document_ref": document_record["ref"], "document_type": kind,
            "modified": document_record["modified"], **detail,
        }


def act(payload: dict[str, Any]) -> dict[str, Any]:
    # A mutation may invalidate any live UI/document snapshot, including when
    # it later reports a typed failure or uncertainty. Never serve a retained
    # continuation across an action boundary.
    STATE.clear_private_snapshots()
    target = payload.get("target") or {}
    action = str(payload.get("action") or "")
    arguments = payload.get("arguments") or {}
    if "ref" in target:
        ref = str(target["ref"])
        _resolve(ref)
        if ref in UNO_REFS:
            return _uno_action(ref, action, arguments)
        return _ui_action(ref, action, arguments)
    if action == "install_package":
        return _install_os_package(arguments)
    if action == "install_extension":
        return _install_libreoffice_extension(arguments)
    if action == "create_directory":
        path = _safe_path(arguments.get("path"))
        path.mkdir(parents=bool(arguments.get("parents", False)), exist_ok=True)
        return {"execution_path": "native_api", "path": str(path)}
    if action in {"copy", "move", "rename"}:
        source = _safe_path(arguments.get("source"))
        destination = _safe_path(arguments.get("destination"))
        if action == "copy":
            shutil.copy2(source, destination)
        else:
            os.replace(source, destination)
        return {"execution_path": "native_api", "path": str(destination)}
    if action == "write_text":
        path = _safe_path(arguments.get("path"))
        expected_hash = arguments.get("expected_hash")
        if path.exists():
            if not isinstance(expected_hash, str):
                raise AgentError("artifact_conflict", "overwriting a file requires expected_hash")
            current_hash = _sha256_path(path)
            if current_hash != expected_hash:
                raise AgentError("artifact_conflict", "file hash differs from expected_hash")
        temporary = path.with_name(
            f".{path.stem}.ghost-{secrets.token_hex(6)}{path.suffix or '.tmp'}"
        )
        data = _text(arguments.get("content"), 64 * 1024)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _parse_artifact(temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "execution_path": "native_api",
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if action == "write_base64_atomic":
        import base64

        path = _safe_path(arguments.get("path"))
        encoded = arguments.get("base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_BODY_BYTES:
            raise AgentError("invalid_request", "base64 payload is invalid")
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as error:
            raise AgentError("invalid_request", "base64 payload is malformed") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = arguments.get("expected_hash")
        if path.exists():
            if not isinstance(expected_hash, str):
                raise AgentError("artifact_conflict", "overwriting a file requires expected_hash")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise AgentError("artifact_conflict", "file hash differs from expected_hash")
        temporary = path.with_name(
            f".{path.stem}.ghost-{secrets.token_hex(6)}{path.suffix or '.tmp'}"
        )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _parse_artifact(temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "execution_path": "native_api", "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": len(data),
        }
    if action == "extract_archive":
        source = _safe_path(arguments.get("source")).resolve()
        destination = _safe_path(arguments.get("destination")).resolve()
        if not source.is_file() or not zipfile.is_zipfile(source):
            raise AgentError("invalid_request", "extract_archive requires a ZIP archive")
        expected_hash = arguments.get("expected_hash")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_hash is not None and expected_hash != source_hash:
            raise AgentError("artifact_conflict", "archive hash differs from expected_hash")
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > _ARTIFACT_MAX_MEMBERS:
                raise AgentError("budget_exhausted", "archive contains too many members")
            total_size = sum(max(0, int(member.file_size)) for member in members)
            if total_size > _ARTIFACT_MAX_BYTES:
                raise AgentError("budget_exhausted", "archive expands beyond the artifact limit")
            for member in members:
                parts = Path(member.filename).parts
                mode = (member.external_attr >> 16) & 0o170000
                if (
                    not member.filename
                    or member.filename.startswith(("/", "\\"))
                    or ".." in parts
                    or mode == 0o120000
                ):
                    raise AgentError("policy_violation", "archive contains an unsafe member")
            with tempfile.TemporaryDirectory(
                prefix=".ghost-extract-", dir=str(destination.parent)
            ) as raw_stage:
                stage = Path(raw_stage)
                archive.extractall(stage)
                roots = sorted(stage.iterdir(), key=lambda item: item.name)
                conflicts = [str(destination / item.name) for item in roots if (destination / item.name).exists()]
                if conflicts:
                    raise AgentError(
                        "artifact_conflict",
                        "archive extraction would overwrite existing paths",
                    )
                for item in roots:
                    os.replace(item, destination / item.name)
        return {
            "execution_path": "native_api", "source": str(source),
            "destination": str(destination), "source_sha256": source_hash,
            "member_count": len(members), "expanded_bytes": total_size,
        }
    if action == "stage_base64_chunk":
        import base64

        transfer_id = arguments.get("transfer_id")
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", transfer_id):
            raise AgentError("invalid_request", "invalid blob transfer_id")
        encoded = arguments.get("base64")
        if not isinstance(encoded, str) or len(encoded) > 800_000:
            raise AgentError("invalid_request", "blob chunk exceeds request limit")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as error:
            raise AgentError("invalid_request", "blob chunk is malformed") from error
        offset = arguments.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AgentError("invalid_request", "blob offset is invalid")
        record = STATE.blob_staging.get(transfer_id)
        if record is None:
            if offset != 0:
                raise AgentError("revision_conflict", "blob transfer must begin at offset zero")
            path = _safe_path(arguments.get("path"))
            artifact_kind = arguments.get("artifact_kind", "generic")
            if artifact_kind not in {"generic", "libreoffice_extension"}:
                raise AgentError("invalid_request", "blob artifact_kind is invalid")
            if artifact_kind == "libreoffice_extension":
                expected_root = (Path.home() / "Downloads" / ".ghost-semantic").resolve()
                resolved_path = path.resolve()
                if (
                    resolved_path.suffix.casefold() != ".oxt"
                    or resolved_path.parent != expected_root
                ):
                    raise AgentError(
                        "permission_denied",
                        "LibreOffice extension staging is confined to its guest artifact root",
                    )
            expected_hash = arguments.get("expected_hash")
            if path.exists():
                if not isinstance(expected_hash, str):
                    raise AgentError("artifact_conflict", "overwriting a file requires expected_hash")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    raise AgentError("artifact_conflict", "file hash differs from expected_hash")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}.ghost-stage-", suffix=path.suffix or ".tmp",
                dir=str(path.parent),
            )
            os.close(descriptor)
            record = {
                "path": path,
                "expected_hash": expected_hash,
                "temporary": Path(temporary_name),
                "size": 0,
                "sha256": hashlib.sha256(),
                "artifact_kind": artifact_kind,
                "max_bytes": (
                    _LIBREOFFICE_EXTENSION_MAX_BYTES
                    if artifact_kind == "libreoffice_extension"
                    else _ARTIFACT_MAX_BYTES
                ),
            }
            STATE.blob_staging[transfer_id] = record
        if offset != record["size"]:
            raise AgentError("revision_conflict", "blob chunk offset does not match staged length")
        next_size = record["size"] + len(chunk)
        if next_size > record["max_bytes"]:
            record["temporary"].unlink(missing_ok=True)
            STATE.blob_staging.pop(transfer_id, None)
            raise AgentError("budget_exhausted", "staged blob exceeds artifact limit")
        if chunk:
            with record["temporary"].open("ab") as handle:
                handle.write(chunk)
            record["sha256"].update(chunk)
            record["size"] = next_size
        if not bool(arguments.get("final", False)):
            return {
                "execution_path": "native_api", "transfer_id": transfer_id,
                "staged_size": record["size"], "complete": False,
            }
        path = record["path"]
        temporary = record["temporary"]
        try:
            with temporary.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            structure = _parse_artifact(temporary)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            STATE.blob_staging.pop(transfer_id, None)
        return {
            "execution_path": "native_api", "transfer_id": transfer_id,
            "path": str(path), "complete": True, "size": path.stat().st_size,
            "sha256": record["sha256"].hexdigest(),
            "artifact": structure,
        }
    if action == "abort_blob_transfer":
        transfer_id = arguments.get("transfer_id")
        if not isinstance(transfer_id, str):
            raise AgentError("invalid_request", "abort_blob_transfer requires transfer_id")
        record = STATE.blob_staging.pop(transfer_id, None)
        if record is not None:
            record["temporary"].unlink(missing_ok=True)
        existed = record is not None
        return {
            "execution_path": "native_api", "transfer_id": transfer_id,
            "aborted": existed,
        }
    if action == "remove_staged_artifact":
        path = _safe_path(arguments.get("path")).resolve()
        expected_root = (Path.home() / "Downloads" / ".ghost-semantic").resolve()
        if path.parent != expected_root or path.suffix.casefold() != ".oxt":
            raise AgentError(
                "permission_denied",
                "staged artifact cleanup is confined to the LibreOffice staging root",
            )
        expected_hash = arguments.get("expected_hash")
        if path.exists():
            if not isinstance(expected_hash, str):
                raise AgentError(
                    "artifact_conflict", "staged artifact cleanup requires expected_hash"
                )
            current_hash = _sha256_path(path)
            if current_hash != expected_hash:
                raise AgentError(
                    "artifact_conflict", "staged artifact hash differs from expected_hash"
                )
            path.unlink()
        return {
            "execution_path": "native_api", "path": str(path),
            "removed": not path.exists(),
        }
    if action == "create_desktop_entry":
        name = _text(arguments.get("name"), 200).strip()
        url = _text(arguments.get("url"), 4_096).strip()
        profile = _text(arguments.get("profile"), 200).strip()
        if not name or not url.startswith(("http://", "https://")):
            raise AgentError("invalid_request", "desktop entry requires name and public URL")
        safe_name = "".join(character if character.isalnum() or character in "-_" else "-" for character in name).strip("-")
        path = Path.home() / "Desktop" / f"{safe_name or 'chrome-shortcut'}.desktop"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            expected_hash = arguments.get("expected_hash")
            if not isinstance(expected_hash, str):
                raise AgentError("artifact_conflict", "overwriting a desktop entry requires expected_hash")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise AgentError("artifact_conflict", "desktop entry hash differs from expected_hash")
        profile_arg = f" --profile-directory={profile}" if profile else ""
        content = (
            "[Desktop Entry]\nType=Application\n"
            f"Name={name}\nExec=google-chrome{profile_arg} --app={url}\n"
            "Terminal=false\nCategories=Network;WebBrowser;\n"
        )
        temporary = path.with_name(f".{path.name}.ghost-{secrets.token_hex(6)}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o755)
        os.replace(temporary, path)
        applications = Path.home() / ".local/share/applications"
        applications.mkdir(parents=True, exist_ok=True)
        if shutil.which("update-desktop-database"):
            _bounded_command(["update-desktop-database", str(applications)])
        if shutil.which("gio"):
            _bounded_command(["gio", "set", str(path), "metadata::trusted", "true"])
        return {
            "execution_path": "native_api", "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "recognized": path.is_file() and os.access(path, os.X_OK),
        }
    if action == "launch":
        desktop_id = arguments.get("desktop_id")
        if not isinstance(desktop_id, str) or not _DESKTOP_ID.fullmatch(desktop_id):
            raise AgentError("invalid_request", "launch requires a valid desktop_id")
        known = {record["desktop_id"] for record in _desktop_entries()}
        normalized = desktop_id if desktop_id.endswith(".desktop") else f"{desktop_id}.desktop"
        if desktop_id not in known and normalized not in known:
            raise AgentError("not_found", f"desktop entry is not installed: {desktop_id}")
        dispatch = _dispatch_desktop_entry(desktop_id)
        return {
            "execution_path": "native_api", "desktop_id": desktop_id,
            "dispatch_state": dispatch["dispatch_state"],
            "launcher_pid": dispatch["launcher_pid"],
        }
    if action == "set_setting":
        schema = arguments.get("schema")
        key = arguments.get("key")
        value = arguments.get("value")
        if (
            not isinstance(schema, str) or not _GSETTINGS_NAME.fullmatch(schema)
            or not isinstance(key, str) or not _GSETTINGS_KEY.fullmatch(key)
            or not isinstance(value, str) or len(value) > 8_192
        ):
            raise AgentError("invalid_request", "set_setting arguments are invalid")
        command = _bounded_command(["gsettings", "set", schema, key, value])
        if command["exit_code"] != 0:
            raise AgentError("postcondition_failed", _text(command["stderr"]) or "GSettings mutation failed")
        check = _bounded_command(["gsettings", "get", schema, key])
        if check["exit_code"] != 0:
            raise AgentError("postcondition_failed", "could not read setting after mutation")
        return {
            "execution_path": "native_api", "schema": schema, "key": key,
            "value": check["stdout"].strip(),
        }
    if action == "write_clipboard":
        value = arguments.get("text")
        if not isinstance(value, str) or len(value) > 64 * 1024:
            raise AgentError("invalid_request", "clipboard text is invalid")
        command = _bounded_command(["xclip", "-selection", "clipboard", "-i"], stdin=value)
        if command["exit_code"] != 0:
            raise AgentError("postcondition_failed", _text(command["stderr"]) or "clipboard write failed")
        return {
            "execution_path": "native_api", "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    if action == "set_audio_volume":
        value = arguments.get("percent")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 150:
            raise AgentError("invalid_request", "audio percent must be between 0 and 150")
        command = _bounded_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{float(value):g}%"])
        if command["exit_code"] != 0:
            raise AgentError("postcondition_failed", _text(command["stderr"]) or "audio volume mutation failed")
        return {"execution_path": "native_api", "percent": float(value)}
    if action == "set_audio_muted":
        muted = arguments.get("muted")
        if not isinstance(muted, bool):
            raise AgentError("invalid_request", "muted must be boolean")
        command = _bounded_command([
            "pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"
        ])
        if command["exit_code"] != 0:
            raise AgentError("postcondition_failed", _text(command["stderr"]) or "audio mute mutation failed")
        return {"execution_path": "native_api", "muted": muted}
    if action == "set_wallpaper":
        path = _safe_path(arguments.get("path"))
        if not path.is_file():
            raise AgentError("not_found", f"wallpaper file does not exist: {path}")
        value = f"file://{path}"
        for key in ("picture-uri", "picture-uri-dark"):
            command = _bounded_command([
                "gsettings", "set", "org.gnome.desktop.background", key, value
            ])
            if command["exit_code"] != 0:
                raise AgentError("internal_error", _text(command["stderr"]) or "wallpaper mutation may be partial")
        return {"execution_path": "native_api", "path": str(path)}
    if action == "dismiss_notification":
        raise AgentError(
            "representation_gap", "notification service exposes no generic notification identity to dismiss"
        )
    if action == "chrome_load_unpacked":
        return _chrome_load_unpacked(arguments)
    raise AgentError("unsupported", f"unsupported action: {action}")


def health() -> dict[str, Any]:
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except OSError:
        machine_id = "unavailable"
    try:
        os_release_hash = hashlib.sha256(Path("/etc/os-release").read_bytes()).hexdigest()
    except OSError:
        os_release_hash = "unavailable"
    return {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "bundle_hash": STATE.bundle_hash,
        "started_at": STATE.started_at,
        "pid": os.getpid(),
        "capability_count": len(CAPABILITIES),
        "guest_machine_id": machine_id,
        "guest_os_release_hash": os_release_hash,
        "guest_platform": platform.system().casefold(),
        "display_identity": os.environ.get("DISPLAY", "unavailable"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GhostSemanticAgent/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        return bool(TOKEN) and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise AgentError("invalid_request", "invalid Content-Length") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise AgentError("invalid_request", "request body exceeds 1 MiB")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise AgentError("invalid_request", "body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise AgentError("invalid_request", "body must be a JSON object")
        return payload

    def _dispatch(self) -> None:
        if not self._authorized():
            self._write(401, {"ok": False, "error": {"code": "permission_denied"}})
            return
        path = urlparse(self.path).path
        try:
            if path.startswith("/v1/adapters/"):
                from native_app_bridges import dispatch_native_app_request

                native_payload = self._payload() if self.command == "POST" else None
                native_response = dispatch_native_app_request(
                    self.command, path, native_payload
                )
                native_response.setdefault("observed_at", _now())
                self._write(200 if native_response.get("ok") else 400, native_response)
                return
            if self.command == "GET" and path == "/v1/health":
                result = health()
            elif self.command == "GET" and path == "/v1/capabilities":
                result = {"records": CAPABILITIES}
            elif self.command == "POST" and path == "/v1/query":
                result = query(self._payload())
            elif self.command == "POST" and path == "/v1/act":
                result = act(self._payload())
            elif self.command == "POST" and path == "/v1/shutdown":
                result = {"stopping": True}
                # Let the response flush before serve_forever is stopped.
                threading.Timer(0.05, self.server.shutdown).start()
            else:
                self._write(404, {"ok": False, "error": {"code": "not_found"}})
                return
            self._write(200, {"ok": True, "observed_at": _now(), "result": result})
        except AgentError as error:
            self._write(400, {
                "ok": False,
                "observed_at": _now(),
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "side_effect_state": error.side_effect_state,
                    "missing_capability": error.missing_capability,
                },
            })
        except Exception as error:
            self._write(500, {
                "ok": False,
                "observed_at": _now(),
                "error": {
                    "code": "internal_error",
                    "message": f"{type(error).__name__}: {error}",
                    "retryable": False,
                },
            })

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()


def main() -> None:
    if not TOKEN or len(TOKEN) < 24:
        raise SystemExit("GHOST_SEMANTIC_TOKEN must contain at least 24 characters")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
