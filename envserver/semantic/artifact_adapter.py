"""Semantic filesystem and artifact inspection/mutation.

This adapter is task-agnostic and evaluator-blind.  It exposes only explicitly
mounted guest paths, uses expected hashes for overwrites, and commits through a
parsed sibling temporary file plus ``fsync``/atomic replace.  Office structures
are parsed from their public package formats, independently of live app state.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Callable, Mapping, Sequence
from xml.etree import ElementTree as ET

from .adapters import AdapterActionResult, AdapterContext, AdapterObservation, SemanticAdapter
from .data_handles import DataHandleStore
from .protocol import ErrorCode, ProtocolError, SideEffectState, Status, utc_now
from .research_adapter import MAX_FETCH_BYTES, PublicHTTPTransport, validate_public_url


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".tsv", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".py", ".js", ".ts", ".css", ".ini", ".desktop",
}
_OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
_ODF_EXTENSIONS = {".odt", ".ods", ".odp"}
_MAX_PARSE_BYTES = 100 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_PATH_ARGUMENT_SCHEMA = {"type": "string", "pattern": "^/"}
_HASH_ARGUMENT_SCHEMA = {"type": "string", "maxLength": 64}
_XML_NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_xml(data: bytes) -> ET.Element:
    if len(data) > _MAX_PARSE_BYTES:
        raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "XML artifact is too large")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ProtocolError(ErrorCode.POLICY_VIOLATION, "XML DTD/entities are forbidden")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "artifact XML is not parseable") from error


def _safe_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "artifact package is not a valid ZIP") from error
    entries = archive.infolist()
    if len(entries) > _MAX_ARCHIVE_MEMBERS:
        archive.close()
        raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "artifact package has too many members")
    total = sum(entry.file_size for entry in entries)
    if total > _MAX_PARSE_BYTES:
        archive.close()
        raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "artifact package expands beyond limit")
    for entry in entries:
        parts = PurePosixPath(entry.filename).parts
        if entry.filename.startswith("/") or ".." in parts:
            archive.close()
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "unsafe artifact package member")
    return archive


def _read_xml_member(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return _safe_xml(archive.read(name))
    except KeyError as error:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"artifact is missing {name}") from error


_BUILTIN_NUMBER_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 9: "0%", 10: "0.00%",
    14: "mm-dd-yy", 22: "m/d/yy h:mm", 49: "@",
}


def _cell_value(
    cell: ET.Element,
    shared: Sequence[str],
    cell_formats: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    kind = cell.attrib.get("t")
    formula = cell.findtext("x:f", default=None, namespaces=_XML_NAMESPACES)
    raw = cell.findtext("x:v", default=None, namespaces=_XML_NAMESPACES)
    inline = "".join(cell.itertext()) if kind == "inlineStr" else None
    value: Any = inline if inline is not None else raw
    if kind == "s" and raw is not None:
        try:
            value = shared[int(raw)]
        except (ValueError, IndexError):
            value = raw
    elif kind == "b" and raw is not None:
        value = raw == "1"
    elif kind in {None, "n"} and raw is not None:
        try:
            value = float(raw) if any(ch in raw for ch in ".eE") else int(raw)
        except ValueError:
            value = raw
    style_id = int(cell.attrib.get("s", "0"))
    style = dict(cell_formats[style_id]) if style_id < len(cell_formats) else {}
    return {
        "address": cell.attrib.get("r"),
        "value": value,
        "formula": formula,
        "style_id": style_id,
        "number_format_id": style.get("number_format_id"),
        "number_format": style.get("number_format"),
        "data_type": kind or "number",
    }


def parse_docx(path: Path) -> dict[str, Any]:
    with _safe_zip(path) as archive:
        document = _read_xml_member(archive, "word/document.xml")
        paragraphs: list[dict[str, Any]] = []
        for index, paragraph in enumerate(document.findall(".//w:p", _XML_NAMESPACES)):
            runs = []
            for run_index, run in enumerate(paragraph.findall("w:r", _XML_NAMESPACES)):
                text = "".join(node.text or "" for node in run.findall(".//w:t", _XML_NAMESPACES))
                props = run.find("w:rPr", _XML_NAMESPACES)
                runs.append({
                    "index": run_index,
                    "text": text,
                    "bold": props is not None and props.find("w:b", _XML_NAMESPACES) is not None,
                    "italic": props is not None and props.find("w:i", _XML_NAMESPACES) is not None,
                })
            style = paragraph.find("w:pPr/w:pStyle", _XML_NAMESPACES)
            alignment = paragraph.find("w:pPr/w:jc", _XML_NAMESPACES)
            spacing = paragraph.find("w:pPr/w:spacing", _XML_NAMESPACES)
            paragraphs.append({
                "index": index,
                "text": "".join(run["text"] for run in runs),
                "style": style.attrib.get(f"{{{_XML_NAMESPACES['w']}}}val") if style is not None else None,
                "alignment": alignment.attrib.get(f"{{{_XML_NAMESPACES['w']}}}val") if alignment is not None else None,
                "spacing": dict(spacing.attrib) if spacing is not None else {},
                "runs": runs,
            })
        tables = []
        for table_index, table in enumerate(document.findall(".//w:tbl", _XML_NAMESPACES)):
            rows = []
            for row in table.findall("w:tr", _XML_NAMESPACES):
                rows.append([
                    "".join(cell.itertext()) for cell in row.findall("w:tc", _XML_NAMESPACES)
                ])
            tables.append({"index": table_index, "rows": rows})
        return {
            "format": "docx",
            "paragraph_count": len(paragraphs),
            "paragraphs": paragraphs,
            "tables": tables,
        }


def parse_xlsx(path: Path) -> dict[str, Any]:
    with _safe_zip(path) as archive:
        workbook = _read_xml_member(archive, "xl/workbook.xml")
        relationships: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in archive.namelist():
            rels = _read_xml_member(archive, "xl/_rels/workbook.xml.rels")
            for node in rels:
                relationships[node.attrib.get("Id", "")] = node.attrib.get("Target", "")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = _read_xml_member(archive, "xl/sharedStrings.xml")
            shared = ["".join(node.itertext()) for node in shared_root.findall("x:si", _XML_NAMESPACES)]
        number_formats = dict(_BUILTIN_NUMBER_FORMATS)
        cell_formats: list[dict[str, Any]] = []
        if "xl/styles.xml" in archive.namelist():
            styles_root = _read_xml_member(archive, "xl/styles.xml")
            for node in styles_root.findall("x:numFmts/x:numFmt", _XML_NAMESPACES):
                try:
                    number_formats[int(node.attrib.get("numFmtId", ""))] = node.attrib.get("formatCode", "")
                except ValueError:
                    continue
            for node in styles_root.findall("x:cellXfs/x:xf", _XML_NAMESPACES):
                try:
                    number_id = int(node.attrib.get("numFmtId", "0"))
                except ValueError:
                    number_id = 0
                cell_formats.append({
                    "number_format_id": number_id,
                    "number_format": number_formats.get(number_id),
                    "font_id": int(node.attrib.get("fontId", "0")),
                    "fill_id": int(node.attrib.get("fillId", "0")),
                    "border_id": int(node.attrib.get("borderId", "0")),
                })
        sheets = []
        for index, sheet in enumerate(workbook.findall(".//x:sheet", _XML_NAMESPACES)):
            rel_id = sheet.attrib.get(f"{{{_XML_NAMESPACES['r']}}}id", "")
            target = relationships.get(rel_id, f"worksheets/sheet{index + 1}.xml")
            member = target.lstrip("/")
            if not member.startswith("xl/"):
                member = f"xl/{member}"
            member = str(PurePosixPath(member))
            xml = _read_xml_member(archive, member)
            cells = [_cell_value(cell, shared, cell_formats) for cell in xml.findall(".//x:c", _XML_NAMESPACES)]
            sheets.append({
                "index": index,
                "name": sheet.attrib.get("name", ""),
                "state": sheet.attrib.get("state", "visible"),
                "cells": cells,
                "cell_count": len(cells),
                "merged_ranges": [
                    node.attrib.get("ref") for node in xml.findall(".//x:mergeCell", _XML_NAMESPACES)
                ],
                "columns": [
                    {
                        "min": int(node.attrib.get("min", "0")),
                        "max": int(node.attrib.get("max", "0")),
                        "width": float(node.attrib["width"]) if "width" in node.attrib else None,
                        "hidden": node.attrib.get("hidden") == "1",
                    }
                    for node in xml.findall("x:cols/x:col", _XML_NAMESPACES)
                ],
                "rows": [
                    {
                        "index": int(node.attrib.get("r", "0")),
                        "height": float(node.attrib["ht"]) if "ht" in node.attrib else None,
                        "hidden": node.attrib.get("hidden") == "1",
                    }
                    for node in xml.findall(".//x:sheetData/x:row", _XML_NAMESPACES)
                ],
            })
        return {"format": "xlsx", "sheet_count": len(sheets), "sheets": sheets}


def parse_pptx(path: Path) -> dict[str, Any]:
    with _safe_zip(path) as archive:
        members = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", Path(name).stem).group()),  # type: ignore[union-attr]
        )
        slides = []
        for index, member in enumerate(members):
            root = _read_xml_member(archive, member)
            shapes = []
            for shape_index, shape in enumerate(root.findall(".//p:sp", {
                **_XML_NAMESPACES,
                "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            })):
                name_node = shape.find("p:nvSpPr/p:cNvPr", {
                    "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                })
                shapes.append({
                    "index": shape_index,
                    "name": name_node.attrib.get("name") if name_node is not None else None,
                    "text": "".join(node.text or "" for node in shape.findall(".//a:t", _XML_NAMESPACES)),
                    "position": {
                        "x": int(shape.find("p:spPr/a:xfrm/a:off", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}).attrib.get("x", "0")),
                        "y": int(shape.find("p:spPr/a:xfrm/a:off", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}).attrib.get("y", "0")),
                    } if shape.find("p:spPr/a:xfrm/a:off", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}) is not None else None,
                    "size": {
                        "width": int(shape.find("p:spPr/a:xfrm/a:ext", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}).attrib.get("cx", "0")),
                        "height": int(shape.find("p:spPr/a:xfrm/a:ext", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}).attrib.get("cy", "0")),
                    } if shape.find("p:spPr/a:xfrm/a:ext", {**_XML_NAMESPACES, "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}) is not None else None,
                })
            slides.append({"index": index, "shapes": shapes, "text": "\n".join(s["text"] for s in shapes if s["text"])})
        return {"format": "pptx", "slide_count": len(slides), "slides": slides}


def parse_odf(path: Path) -> dict[str, Any]:
    with _safe_zip(path) as archive:
        root = _read_xml_member(archive, "content.xml")
        suffix = path.suffix.casefold()
        if suffix == ".odt":
            paragraphs = [
                {"index": index, "text": "".join(node.itertext()), "style": node.attrib.get(f"{{{_XML_NAMESPACES['text']}}}style-name")}
                for index, node in enumerate(root.findall(".//text:p", _XML_NAMESPACES))
            ]
            return {"format": "odt", "paragraph_count": len(paragraphs), "paragraphs": paragraphs}
        if suffix == ".ods":
            sheets = []
            for index, table in enumerate(root.findall(".//table:table", _XML_NAMESPACES)):
                rows = []
                for row in table.findall("table:table-row", _XML_NAMESPACES):
                    values = []
                    for cell in row.findall("table:table-cell", _XML_NAMESPACES):
                        values.append({
                            "value": cell.attrib.get(f"{{{_XML_NAMESPACES['office']}}}value") or "".join(cell.itertext()),
                            "formula": cell.attrib.get(f"{{{_XML_NAMESPACES['table']}}}formula"),
                        })
                    rows.append(values)
                sheets.append({"index": index, "name": table.attrib.get(f"{{{_XML_NAMESPACES['table']}}}name"), "rows": rows})
            return {"format": "ods", "sheet_count": len(sheets), "sheets": sheets}
        pages = []
        for index, page in enumerate(root.findall(".//draw:page", _XML_NAMESPACES)):
            pages.append({"index": index, "name": page.attrib.get(f"{{{_XML_NAMESPACES['draw']}}}name"), "text": " ".join(page.itertext())})
        return {"format": "odp", "slide_count": len(pages), "slides": pages}


def parse_pdf(path: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages.append({"index": index, "text": text[:64_000]})
        metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
        return {"format": "pdf", "page_count": len(pages), "pages": pages, "metadata": metadata}
    except ImportError:
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "PDF artifact is not parseable")
        # Conservative fallback: count only concrete /Type /Page objects, not
        # the /Pages tree.  Text extraction requires the optional parser.
        page_count = len(re.findall(rb"/Type\s*/Page(?!s)\b", data))
        return {"format": "pdf", "page_count": page_count, "pages": [], "text_available": False}
    except ProtocolError:
        raise
    except Exception as error:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "PDF artifact is not parseable") from error


def _parse_artifact_impl(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, "artifact file does not exist")
    if path.stat().st_size > _MAX_PARSE_BYTES:
        raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "artifact exceeds parse size limit")
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".xlsx":
        return parse_xlsx(path)
    if suffix == ".pptx":
        return parse_pptx(path)
    if suffix in _ODF_EXTENSIONS:
        return parse_odf(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    data = path.read_bytes()
    if suffix in {".xml", ".html", ".htm"}:
        root = _safe_xml(data) if suffix == ".xml" else None
        return {"format": suffix.lstrip("."), "parseable": True, "root_tag": root.tag if root is not None else "html", "size": len(data)}
    if suffix == ".json":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "JSON artifact is not parseable") from error
        return {"format": "json", "root_type": type(value).__name__, "value": value}
    if suffix in {".csv", ".tsv"}:
        delimiter = "," if suffix == ".csv" else "\t"
        try:
            rows = list(csv.reader(io.StringIO(data.decode("utf-8")), delimiter=delimiter))
        except (UnicodeDecodeError, csv.Error) as error:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "delimited artifact is not parseable") from error
        return {"format": suffix.lstrip("."), "row_count": len(rows), "column_counts": [len(row) for row in rows], "rows": rows}
    if suffix in _TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "text artifact is not UTF-8") from error
        return {"format": suffix.lstrip(".") or "text", "characters": len(text), "lines": text.count("\n") + (1 if text else 0)}
    return {"format": suffix.lstrip(".") or "binary", "parseable": True, "size": len(data)}


def parse_artifact(path: Path) -> dict[str, Any]:
    try:
        return _parse_artifact_impl(path)
    except ProtocolError:
        raise
    except (OSError, ValueError, KeyError, IndexError, UnicodeError, zipfile.BadZipFile) as error:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "artifact structure is not parseable") from error


def _structure_records(structure: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten package structure so one giant sheet/slide is still pageable."""

    records: list[dict[str, Any]] = []
    for paragraph in structure.get("paragraphs", ()):
        records.append({"kind": "artifact.paragraph", **dict(paragraph)})
    for table in structure.get("tables", ()):
        table_record = {key: value for key, value in table.items() if key != "rows"}
        records.append({"kind": "artifact.table", **table_record})
        for row_index, row in enumerate(table.get("rows", ())):
            records.append({"kind": "artifact.table_row", "table_index": table.get("index"), "row_index": row_index, "cells": row})
    for sheet in structure.get("sheets", ()):
        sheet_record = {key: value for key, value in sheet.items() if key not in {"cells", "rows"}}
        records.append({"kind": "artifact.sheet", **sheet_record})
        for cell in sheet.get("cells", ()):
            records.append({"kind": "artifact.cell", "sheet_index": sheet.get("index"), "sheet_name": sheet.get("name"), **dict(cell)})
        for row_index, row in enumerate(sheet.get("rows", ())):
            records.append({"kind": "artifact.sheet_row", "sheet_index": sheet.get("index"), "sheet_name": sheet.get("name"), "row_index": row_index, "cells": row})
    for slide in structure.get("slides", ()):
        slide_record = {key: value for key, value in slide.items() if key != "shapes"}
        records.append({"kind": "artifact.slide", **slide_record})
        for shape in slide.get("shapes", ()):
            records.append({"kind": "artifact.shape", "slide_index": slide.get("index"), **dict(shape)})
    return records or [{"kind": "artifact.structure", "structure": dict(structure)}]


class ArtifactLeaseRegistry:
    """Tracks explicit live-app ownership without inferring task semantics."""

    def __init__(self) -> None:
        self._leases: dict[Path, dict[str, Any]] = {}
        self._lock = RLock()

    def set(self, path: Path, *, owner: str, modified: bool, revision: str | None = None) -> None:
        with self._lock:
            self._leases[path.resolve()] = {"owner": owner, "modified": bool(modified), "revision": revision}

    def clear(self, path: Path) -> None:
        with self._lock:
            self._leases.pop(path.resolve(), None)

    def get(self, path: Path) -> dict[str, Any] | None:
        with self._lock:
            value = self._leases.get(path.resolve())
            return dict(value) if value is not None else None


class ArtifactFilesystemAdapter(SemanticAdapter):
    adapter_id = "artifact.filesystem@1"
    application = "guest filesystem and public artifact formats"
    supported_versions = ("POSIX", "OOXML", "ODF", "PDF")
    accepts_entity_target = True
    resources = frozenset({
        "filesystem.entries", "filesystem.file", "filesystem.metadata",
        "artifact.owners", "artifact.structure", "artifact.sync",
        "artifact.exports", "artifact.downloads",
    })
    capabilities = frozenset({"create_directory", "copy", "move", "rename", "write_text", "patch_text", "download"})
    resource_schemas = {
        "filesystem.entries": {
            "type": "object",
            "properties": {"recursive": {"type": "boolean"}},
            "additionalProperties": False,
        },
        "filesystem.file": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "data_handle": {"type": "string"}},
            "additionalProperties": False,
        },
        "filesystem.metadata": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        "artifact.structure": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "data_handle": {"type": "string"}},
            "additionalProperties": False,
        },
        "artifact.owners": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        "artifact.sync": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        "artifact.exports": {"type": "object", "properties": {}, "additionalProperties": False},
        "artifact.downloads": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    action_schemas = {
        "create_directory": {
            "arguments_schema": {
                "type": "object",
                "properties": {"path": _PATH_ARGUMENT_SCHEMA, "parents": {"type": "boolean"}},
                "required": ["path"], "additionalProperties": False,
            },
            "risk": "persistent", "idempotent": True,
            "execution_paths": ["native_api"],
        },
        **{
            action: {
                "arguments_schema": {
                    "type": "object",
                    "properties": {"path": _PATH_ARGUMENT_SCHEMA, "expected_hash": _HASH_ARGUMENT_SCHEMA},
                    "required": ["path"], "additionalProperties": False,
                },
                "risk": "persistent", "idempotent": False,
                "execution_paths": ["native_api"],
            }
            for action in ("copy", "move", "rename")
        },
        "write_text": {
            "arguments_schema": {
                "type": "object",
                "properties": {
                    "path": _PATH_ARGUMENT_SCHEMA,
                    "text": {"type": "string", "maxLength": 1_048_576},
                    "expected_hash": _HASH_ARGUMENT_SCHEMA,
                },
                "required": ["text"], "additionalProperties": False,
            },
            "risk": "persistent", "idempotent": False,
            "execution_paths": ["native_api"],
        },
        "patch_text": {
            "arguments_schema": {
                "type": "object",
                "properties": {
                    "expected_hash": _HASH_ARGUMENT_SCHEMA,
                    "replacements": {
                        "type": "array", "minItems": 1, "maxItems": 1_000,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"}, "new": {"type": "string"},
                                "count": {"type": "integer", "minimum": 1},
                            },
                            "required": ["old", "new"], "additionalProperties": False,
                        },
                    },
                },
                "required": ["expected_hash", "replacements"], "additionalProperties": False,
            },
            "risk": "persistent", "idempotent": False,
            "execution_paths": ["native_api"],
        },
        "download": {
            "arguments_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": 8_192},
                    "path": _PATH_ARGUMENT_SCHEMA,
                    "expected_hash": _HASH_ARGUMENT_SCHEMA,
                },
                "required": ["url", "path"], "additionalProperties": False,
            },
            "risk": "persistent", "idempotent": False,
            "execution_paths": ["native_api"],
        },
    }

    def __init__(
        self,
        host_root: str | Path,
        *,
        guest_root: str = "/home/user",
        handles: DataHandleStore | None = None,
        leases: ArtifactLeaseRegistry | None = None,
        live_structure: Callable[[str], Mapping[str, Any] | None] | None = None,
        http_transport: Any | None = None,
    ) -> None:
        self.host_root = Path(host_root).resolve()
        self.host_root.mkdir(parents=True, exist_ok=True)
        self.guest_root = PurePosixPath(guest_root)
        if not self.guest_root.is_absolute() or ".." in self.guest_root.parts:
            raise ValueError("guest_root must be a normalized absolute path")
        self.handles = handles or DataHandleStore()
        self.leases = leases or ArtifactLeaseRegistry()
        self.live_structure = live_structure
        self.http_transport = http_transport or PublicHTTPTransport()
        self._native: dict[str, Path] = {}
        self._exports: list[dict[str, Any]] = []
        self._downloads: list[dict[str, Any]] = []
        self._lock = RLock()

    def probe(self) -> Mapping[str, Any]:
        return {
            "ok": self.host_root.is_dir() and os.access(self.host_root, os.R_OK | os.W_OK),
            "adapter_id": self.adapter_id,
            "guest_root": str(self.guest_root),
            "formats": sorted(_OOXML_EXTENSIONS | _ODF_EXTENSIONS | {".pdf"}),
        }

    def resolve_ref(self, ref: str) -> Mapping[str, Any]:
        path = self._native.get(ref)
        if path is None:
            raise ProtocolError(ErrorCode.STALE_REF, "artifact ref no longer resolves")
        return self._record(path)

    def _map(self, guest_path: str) -> Path:
        if not isinstance(guest_path, str) or not guest_path or "\x00" in guest_path:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "guest path is invalid")
        candidate = PurePosixPath(guest_path)
        if not candidate.is_absolute() or candidate != self.guest_root and self.guest_root not in candidate.parents:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "path is outside the artifact mount")
        relative = candidate.relative_to(self.guest_root)
        if ".." in relative.parts:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "path traversal is forbidden")
        lexical = self.host_root / Path(*relative.parts)
        current_lexical = self.host_root
        for part in relative.parts:
            current_lexical = current_lexical / part
            if current_lexical.is_symlink():
                raise ProtocolError(ErrorCode.PERMISSION_DENIED, "symlink paths are forbidden")
        mapped = lexical.resolve(strict=False)
        if mapped != self.host_root and self.host_root not in mapped.parents:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "resolved path escapes artifact mount")
        # Existing symlink parents must not redirect outside the mount.
        current = mapped
        while current != self.host_root and not current.exists():
            current = current.parent
        resolved_parent = current.resolve()
        if resolved_parent != self.host_root and self.host_root not in resolved_parent.parents:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "symlink escapes artifact mount")
        return mapped

    def _guest(self, host_path: Path) -> str:
        try:
            relative = host_path.relative_to(self.host_root)
        except ValueError as error:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "host path is outside artifact mount") from error
        return str(self.guest_root / PurePosixPath(*relative.parts))

    def _remember(self, path: Path) -> str:
        key = hashlib.sha256(str(path.resolve(strict=False)).encode()).hexdigest()[:30]
        ref = f"native_artifact_{key}"
        self._native[ref] = path
        return ref

    def _record(self, path: Path) -> dict[str, Any]:
        if path.is_symlink():
            info = path.lstat()
            return {
                "ref": self._remember(path),
                "kind": "filesystem.symlink",
                "path": self._guest(path),
                "name": path.name,
                "exists": True,
                "is_directory": False,
                "size": None,
                "modified_ns": info.st_mtime_ns,
                "sha256": None,
                "advertised_actions": [],
            }
        exists = path.exists()
        info = path.stat() if exists else None
        kind = "directory" if exists and path.is_dir() else "file"
        record = {
            "ref": self._remember(path),
            "kind": f"filesystem.{kind}",
            "path": self._guest(path),
            "name": path.name or str(self.guest_root),
            "exists": exists,
            "is_directory": bool(exists and path.is_dir()),
            "size": info.st_size if info and path.is_file() else None,
            "modified_ns": info.st_mtime_ns if info else None,
            "sha256": sha256_file(path) if exists and path.is_file() else None,
                "advertised_actions": ["create_directory", "write_text", "download"] if exists and path.is_dir() else ["copy", "move", "rename", "write_text", "patch_text"],
        }
        return record

    def _tree_revision(self) -> str:
        values: list[tuple[Any, ...]] = []
        entries = [self.host_root, *sorted(self.host_root.rglob("*"), key=lambda value: str(value).casefold())]
        for path in entries[:5_000]:
            try:
                if path.is_symlink():
                    info = path.lstat()
                    values.append((self._guest(path), "symlink", info.st_mtime_ns))
                elif path.is_file():
                    info = path.stat()
                    values.append((self._guest(path), "file", info.st_size, info.st_mtime_ns, sha256_file(path)))
                elif path.is_dir():
                    info = path.stat()
                    values.append((self._guest(path), "directory", info.st_mtime_ns))
            except FileNotFoundError:
                continue
        values.append(("truncated", len(entries) > 5_000))
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    def _path_from_payload(self, payload: Mapping[str, Any]) -> Path:
        scope = payload.get("scope") or {}
        parameters = payload.get("parameters") or {}
        value = scope.get("path") or parameters.get("path") or str(self.guest_root)
        return self._map(str(value))

    def observe(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterObservation:
        parameters = payload.get("parameters") or {}
        scope = payload.get("scope") or {}
        explicit_path = bool(
            isinstance(scope, Mapping) and scope.get("path") is not None
            or isinstance(parameters, Mapping) and parameters.get("path") is not None
        )
        requested_handle = parameters.get("data_handle") if isinstance(parameters, Mapping) else None
        if requested_handle is not None and context.resource in {"filesystem.file", "artifact.structure"}:
            kind = "artifact.text" if context.resource == "filesystem.file" else "artifact.structure"
            stored = self.handles.get(str(requested_handle), kind=kind)
            records = [
                {
                    "kind": f"{kind}.chunk",
                    "data_handle": stored.handle,
                    **dict(record),
                    "advertised_actions": [],
                }
                for record in stored.records
            ]
            revision = self._tree_revision()
            return AdapterObservation(
                items=tuple(records),
                provenance=({"source": self.adapter_id, "freshness": "cache_ok"},),
                summary={"record_count": len(records), "data_handle": stored.handle},
                native_revision=f"artifact_{revision[:20]}",
            )
        path = self._path_from_payload(payload)
        resource = context.resource
        if resource == "filesystem.entries":
            if not path.exists():
                raise ProtocolError(ErrorCode.NOT_FOUND, "directory does not exist")
            if not path.is_dir():
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "entries resource requires a directory")
            recursive = bool((payload.get("parameters") or {}).get("recursive", False))
            entries = path.rglob("*") if recursive or not explicit_path else path.iterdir()
            records = []
            truncated = False
            if not explicit_path:
                records.append(self._record(path))
            for index, entry in enumerate(sorted(entries, key=lambda value: str(value).casefold())):
                if len(records) >= 5_000:
                    truncated = True
                    break
                records.append(self._record(entry))
        elif resource in {"filesystem.file", "filesystem.metadata"}:
            if not explicit_path:
                entries = [path, *sorted(path.rglob("*"), key=lambda value: str(value).casefold())]
                records = [self._record(entry) for entry in entries[:5_000]]
                truncated = len(entries) > 5_000
                record = None
            else:
                record = self._record(path)
            if resource == "filesystem.file" and path.exists() and path.is_file():
                if path.suffix.casefold() in _TEXT_EXTENSIONS and path.stat().st_size <= _MAX_PARSE_BYTES:
                    try:
                        text = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        text = ""
                    if len(text) <= 2_000:
                        record["content"] = text
                    else:
                        chunks = [{"index": index, "text": text[start:start + 2_000]} for index, start in enumerate(range(0, len(text), 2_000))]
                        handle_records = [
                            self.handles.create(
                                "artifact.text", chunks[start:start + 5_000],
                                metadata={"path": self._guest(path), "sha256": record["sha256"], "part": start // 5_000},
                            )
                            for start in range(0, len(chunks), 5_000)
                        ]
                        record.update({
                            "content_excerpt": text[:2_000], "content_truncated": True,
                            "data_handle": handle_records[0].handle,
                            "data_handles": [value.handle for value in handle_records],
                        })
            if record is not None:
                records = [record]
        elif resource == "artifact.structure":
            structure = parse_artifact(path)
            encoded = json.dumps(structure, ensure_ascii=False, default=str)
            record = {**self._record(path), "parseable": True}
            if len(encoded) <= 8_000:
                record["structure"] = structure
            else:
                top_records: Sequence[Mapping[str, Any]] = _structure_records(structure)
                handle_records = [
                    self.handles.create(
                        "artifact.structure", top_records[start:start + 5_000],
                        metadata={
                            "path": self._guest(path), "sha256": record["sha256"],
                            "format": structure.get("format"), "part": start // 5_000,
                        },
                    )
                    for start in range(0, len(top_records), 5_000)
                ]
                record.update({
                    "structure_summary": {key: value for key, value in structure.items() if not isinstance(value, (list, dict))},
                    "data_handle": handle_records[0].handle,
                    "data_handles": [value.handle for value in handle_records],
                    "structure_truncated": True,
                })
            records = [record]
        elif resource == "artifact.owners":
            lease = self.leases.get(path)
            records = [{**self._record(path), "owner": lease}]
        elif resource == "artifact.sync":
            if self.live_structure is None:
                raise ProtocolError(
                    ErrorCode.REPRESENTATION_GAP,
                    "live application structure is unavailable for synchronization",
                    missing_capability="live_application_structure",
                )
            disk = parse_artifact(path)
            live = self.live_structure(self._guest(path))
            records = [{
                **self._record(path),
                "disk_parseable": True,
                "live_available": live is not None,
                "live_app_matches_disk": live == disk if live is not None else None,
            }]
        elif resource == "artifact.exports":
            records = list(self._exports)
        elif resource == "artifact.downloads":
            records = list(self._downloads)
        else:
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, resource)
        revision = self._tree_revision()
        return AdapterObservation(
            items=tuple(records),
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={"record_count": len(records), "truncated": locals().get("truncated", False)},
            native_revision=f"artifact_{revision[:20]}",
        )

    def _target(self, payload: Mapping[str, Any]) -> Path:
        target = payload.get("target") or {}
        native_ref = target.get("ref")
        path = self._native.get(str(native_ref))
        if path is None:
            raise ProtocolError(ErrorCode.STALE_REF, "artifact ref no longer resolves", retryable=True)
        return path

    def _guard_live_conflict(self, path: Path) -> None:
        lease = self.leases.get(path)
        if lease and lease.get("modified"):
            raise ProtocolError(
                ErrorCode.ARTIFACT_CONFLICT,
                "artifact has unsaved changes in a live application",
                retryable=True,
            )

    def _destination(self, target: Path, arguments: Mapping[str, Any], key: str = "path") -> Path:
        guest = arguments.get(key)
        if guest is None:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{key} is required")
        return self._map(str(guest))

    def _atomic_write(self, path: Path, data: bytes, expected_hash: str | None) -> dict[str, Any]:
        self._guard_live_conflict(path)
        if path.is_symlink():
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "artifact writes through symlinks are forbidden")
        existed = path.exists()
        prior_hash = sha256_file(path) if existed and path.is_file() else None
        if existed:
            if not isinstance(expected_hash, str) or not expected_hash:
                raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "overwriting requires expected_hash")
            if expected_hash != prior_hash:
                raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "artifact hash changed before write", retryable=True)
        elif expected_hash is not None:
            raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "expected_hash supplied for a missing artifact")
        path.parent.mkdir(parents=True, exist_ok=True)
        old_mode = stat.S_IMODE(path.stat().st_mode) if existed else None
        temporary: Path | None = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=path.suffix, dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            parse_artifact(temporary)
            if old_mode is not None:
                os.chmod(temporary, old_mode)
            os.replace(temporary, path)
            replaced = True
            temporary = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            final_hash = sha256_file(path)
            if final_hash != hashlib.sha256(data).hexdigest():
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    "final artifact hash does not match write",
                    side_effect_state=SideEffectState.APPLIED,
                )
            parse_artifact(path)
            return {"path": self._guest(path), "before_hash": prior_hash, "after_hash": final_hash, "parseable": True}
        except ProtocolError as error:
            if replaced:
                error.side_effect_state = SideEffectState.APPLIED
            raise
        except OSError as error:
            raise ProtocolError(
                ErrorCode.UNCERTAIN if replaced else ErrorCode.INTERNAL_ERROR,
                "atomic artifact commit failed",
                side_effect_state=(SideEffectState.APPLIED if replaced else SideEffectState.NONE),
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _act_impl(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterActionResult:
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        target = self._target(payload)
        if target.is_symlink():
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "artifact actions on symlinks are forbidden")
        with self._lock:
            if action == "create_directory":
                destination = self._destination(target, arguments)
                if destination.is_symlink():
                    raise ProtocolError(ErrorCode.POLICY_VIOLATION, "artifact destinations may not be symlinks")
                if destination.exists():
                    if not destination.is_dir():
                        raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "directory path is occupied by a file")
                    changed = False
                else:
                    destination.mkdir(parents=bool(arguments.get("parents", False)), exist_ok=False)
                    changed = True
                result = {"path": self._guest(destination), "created": changed}
            elif action in {"copy", "move", "rename"}:
                if not target.exists() or not target.is_file():
                    raise ProtocolError(ErrorCode.NOT_FOUND, "source artifact does not exist")
                destination = self._destination(target, arguments)
                self._guard_live_conflict(target)
                self._guard_live_conflict(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                source_hash = sha256_file(target)
                expected_hash = arguments.get("expected_hash")
                if destination.exists():
                    if not isinstance(expected_hash, str) or sha256_file(destination) != expected_hash:
                        raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "destination overwrite hash mismatch")
                if action == "copy":
                    data = target.read_bytes()
                    result = self._atomic_write(destination, data, expected_hash)
                else:
                    replaced = False
                    try:
                        os.replace(target, destination)
                        replaced = True
                        for directory in {target.parent, destination.parent}:
                            descriptor = os.open(directory, os.O_RDONLY)
                            try:
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        final_hash = sha256_file(destination)
                        parse_artifact(destination)
                    except ProtocolError as error:
                        if replaced:
                            error.side_effect_state = SideEffectState.APPLIED
                        raise
                    except OSError as error:
                        raise ProtocolError(
                            ErrorCode.UNCERTAIN if replaced else ErrorCode.INTERNAL_ERROR,
                            "atomic artifact move failed",
                            side_effect_state=(SideEffectState.APPLIED if replaced else SideEffectState.NONE),
                        ) from error
                    result = {"path": self._guest(destination), "before_hash": source_hash, "after_hash": final_hash, "parseable": True}
                changed = True
            elif action in {"write_text", "patch_text"}:
                destination = target
                if target.is_dir():
                    destination = self._destination(target, arguments)
                expected_hash = arguments.get("expected_hash")
                if expected_hash is not None and not isinstance(expected_hash, str):
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "expected_hash must be a string or null")
                if action == "write_text":
                    text = arguments.get("text")
                    if not isinstance(text, str):
                        raise ProtocolError(ErrorCode.INVALID_REQUEST, "write_text requires text")
                else:
                    if not destination.is_file():
                        raise ProtocolError(ErrorCode.NOT_FOUND, "patch target does not exist")
                    if not isinstance(expected_hash, str):
                        raise ProtocolError(ErrorCode.ARTIFACT_CONFLICT, "patch_text requires expected_hash")
                    try:
                        text = destination.read_text(encoding="utf-8")
                    except UnicodeDecodeError as error:
                        raise ProtocolError(ErrorCode.INVALID_REQUEST, "patch target is not UTF-8") from error
                    replacements = arguments.get("replacements")
                    if not isinstance(replacements, list) or not replacements:
                        raise ProtocolError(ErrorCode.INVALID_REQUEST, "patch_text requires replacements")
                    for replacement in replacements:
                        if not isinstance(replacement, Mapping) or set(replacement) - {"old", "new", "count"}:
                            raise ProtocolError(ErrorCode.INVALID_REQUEST, "patch replacement is invalid")
                        old, new = replacement.get("old"), replacement.get("new")
                        count = replacement.get("count", 1)
                        if not isinstance(old, str) or not old or not isinstance(new, str) or not isinstance(count, int) or count < 1:
                            raise ProtocolError(ErrorCode.INVALID_REQUEST, "patch replacement fields are invalid")
                        if text.count(old) < count:
                            raise ProtocolError(ErrorCode.PRECONDITION_FAILED, "patch source text did not match requested count")
                        text = text.replace(old, new, count)
                result = self._atomic_write(destination, text.encode("utf-8"), expected_hash)
                changed = result["before_hash"] != result["after_hash"]
            elif action == "download":
                if not target.is_dir():
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "download target must be a directory")
                destination = self._destination(target, arguments)
                expected_hash = arguments.get("expected_hash")
                if expected_hash is not None and not isinstance(expected_hash, str):
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "expected_hash must be a string or null")
                url = arguments.get("url")
                resolver = getattr(self.http_transport, "resolver", None)
                if resolver is None:
                    validate_public_url(str(url))
                else:
                    validate_public_url(str(url), resolver=resolver)
                response = self.http_transport.fetch(str(url))
                if not isinstance(response.body, (bytes, bytearray)) or len(response.body) > MAX_FETCH_BYTES:
                    raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "public download body exceeds its byte limit")
                validator = (
                    (lambda value: validate_public_url(value, resolver=resolver))
                    if resolver is not None else validate_public_url
                )
                validator(response.final_url)
                for redirected in response.redirect_chain:
                    validator(redirected)
                if not 200 <= int(response.status) < 300:
                    raise ProtocolError(ErrorCode.NO_EFFECT, "public download returned a non-success HTTP status")
                result = self._atomic_write(destination, bytes(response.body), expected_hash)
                download_identity = hashlib.sha256(
                    f"{response.final_url}:{result['after_hash']}".encode()
                ).hexdigest()[:24]
                download = {
                    "ref": f"native_download_{download_identity}",
                    "kind": "artifact.download",
                    "path": self._guest(destination),
                    "requested_url": str(url),
                    "url": response.final_url,
                    "http_status": int(response.status),
                    "content_hash": result["after_hash"],
                    "fetched_at": response.fetched_at,
                    "redirect_chain": list(response.redirect_chain),
                    "status": "completed",
                    "advertised_actions": [],
                }
                self._downloads.append(download)
                result = {**result, "download": {key: value for key, value in download.items() if key != "ref"}}
                changed = True
            else:
                raise ProtocolError(ErrorCode.UNSUPPORTED, f"unsupported artifact action: {action}")
        return AdapterActionResult(
            changed=changed,
            result={"execution_path": "native_api", **result},
            provenance=({"source": self.adapter_id, "freshness": "live", "observed_at": utc_now()},),
            status=Status.OK,
        )

    def act(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterActionResult:
        try:
            return self._act_impl(context, payload)
        except ProtocolError:
            raise
        except FileNotFoundError as error:
            raise ProtocolError(ErrorCode.NOT_FOUND, "artifact path does not exist") from error
        except PermissionError as error:
            raise ProtocolError(ErrorCode.PERMISSION_DENIED, "artifact path permission denied") from error
        except (IsADirectoryError, NotADirectoryError) as error:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "artifact path has the wrong kind") from error
        except OSError as error:
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "filesystem action failed") from error

    def close(self) -> None:
        self.handles.clear()
