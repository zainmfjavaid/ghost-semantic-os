"""Regression contracts for truthful LibreOffice live/disk synchronization."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from guest_agent import semantic_agent


class WriterDiskCanonicalizationTests(unittest.TestCase):
    def test_docx_parser_includes_displayed_hyperlink_runs(self) -> None:
        document_xml = b"""<?xml version='1.0' encoding='UTF-8'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
 xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
 <w:body><w:p>
  <w:r><w:t>PDF: </w:t></w:r>
  <w:hyperlink r:id='rId1'><w:r><w:t>https://example.test/paper.pdf</w:t></w:r></w:hyperlink>
 </w:p><w:sectPr/></w:body>
</w:document>"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "linked.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            structure = semantic_agent._parse_docx(path)

        expected = "PDF: https://example.test/paper.pdf"
        self.assertEqual(structure["paragraphs"][0]["text"], expected)
        self.assertEqual(structure["body_paragraphs"][0]["text"], expected)
        self.assertEqual(
            semantic_agent._canonical_writer_disk(structure)["paragraphs"],
            [expected],
        )

    def test_writer_canonicalizer_separates_body_flow_from_table_cells(self) -> None:
        disk = {
            "body_paragraphs": [{"text": "body"}],
            "paragraphs": [{"text": "body"}, {"text": "inside table"}],
            "tables": [{"rows": [["inside table", "second cell"]]}],
        }
        self.assertEqual(semantic_agent._canonical_writer_disk(disk), {
            "paragraphs": ["body"],
            "tables": [{"cells": ["inside table", "second cell"]}],
        })


class _Cell:
    def __init__(
        self, kind: str, *, formula: str = "", display: str = "", value: float = 0.0
    ) -> None:
        self.kind = kind
        self.Formula = formula
        self.String = display
        self.Value = value

    def getType(self):
        return SimpleNamespace(value=self.kind.upper())


class _Cursor:
    def __init__(self, end_column: int, end_row: int) -> None:
        self.address = SimpleNamespace(EndColumn=end_column, EndRow=end_row)

    def gotoEndOfUsedArea(self, _expand: bool) -> None:
        return None

    def getRangeAddress(self):
        return self.address


class _Sheet:
    def __init__(self, cells: list[list[_Cell]]) -> None:
        self.cells = cells

    def createCursor(self):
        return _Cursor(len(self.cells[0]) - 1, len(self.cells) - 1)

    def getCellByPosition(self, column: int, row: int):
        return self.cells[row][column]


class _Sheets:
    def __init__(self, sheet: _Sheet) -> None:
        self.sheet = sheet

    def getElementNames(self):
        return ("Sheet1",)

    def getByName(self, name: str):
        if name != "Sheet1":
            raise KeyError(name)
        return self.sheet


class _CalcDocument:
    def __init__(self, sheet: _Sheet) -> None:
        self.sheets = _Sheets(sheet)

    def getSheets(self):
        return self.sheets


class CalcCanonicalizationTests(unittest.TestCase):
    def test_numeric_cells_compare_as_numbers_not_formatted_display_strings(self) -> None:
        document = _CalcDocument(_Sheet([[
            _Cell("text", formula="year", display="year"),
            _Cell("value", formula="2022", display="2,022", value=2022.0),
            _Cell("value", formula="0", display="0", value=0.0),
            _Cell("formula", formula="=B1+C1", display="2,022", value=2022.0),
        ]]))
        live = semantic_agent._canonical_calc_live(document)
        disk = semantic_agent._canonical_calc_disk({
            "sheets": [{
                "name": "Sheet1",
                "cells": [
                    {"address": "A1", "value": "year", "formula": None},
                    {"address": "B1", "value": 2022, "formula": None},
                    {"address": "C1", "value": 0, "formula": None},
                    {"address": "D1", "value": 2022, "formula": "B1+C1"},
                ],
            }],
        })

        self.assertEqual(live, disk)
        self.assertEqual(live[0]["cells"][1]["value"], 2022)
        self.assertEqual(live[0]["cells"][2]["value"], 0)
        self.assertEqual(live[0]["cells"][3]["formula"], "=B1+C1")

    def test_structural_evidence_reports_first_real_difference(self) -> None:
        evidence = semantic_agent._structural_comparison(
            [{"name": "Sheet1", "cells": [{"address": "A1", "value": 1}]}],
            [{"name": "Sheet1", "cells": [{"address": "A1", "value": 2}]}],
        )
        self.assertFalse(evidence["matched"])
        self.assertEqual(evidence["first_difference"]["path"], "$[0].cells[0].value")
        self.assertEqual(evidence["first_difference"]["live"], "1")
        self.assertEqual(evidence["first_difference"]["disk"], "2")


if __name__ == "__main__":
    unittest.main()
