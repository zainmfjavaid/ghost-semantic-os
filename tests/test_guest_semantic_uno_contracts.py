"""Focused regression tests for truthful LibreOffice mutation contracts."""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from guest_agent import semantic_agent


class _Properties:
    pass


class _WriterDocument:
    def supportsService(self, name):
        return name == "com.sun.star.text.TextDocument"

    def isModified(self):
        return True


class _Table(_Properties):
    def __init__(self):
        self.cell = _Properties()
        self.cell.String = "before"

    def getCellByName(self, name):
        if name != "A1":
            raise KeyError(name)
        return self.cell


class _Shape(_Properties):
    def __init__(self):
        self.Name = "Google Shape;84;p13"
        self.String = "Quarterly plan"
        self.ParaAdjust = 0
        self.CharHeight = 10.0
        self.CharColor = 0
        self._position = SimpleNamespace(X=10, Y=20)
        self._size = SimpleNamespace(Width=100, Height=200)

    def getPosition(self):
        return SimpleNamespace(X=self._position.X, Y=self._position.Y)

    def setPosition(self, value):
        self._position = SimpleNamespace(X=value.X, Y=value.Y)

    def getSize(self):
        return SimpleNamespace(Width=self._size.Width, Height=self._size.Height)

    def setSize(self, value):
        self._size = SimpleNamespace(Width=value.Width, Height=value.Height)

    def getShapeType(self):
        return "com.sun.star.presentation.TitleTextShape"


class _ImpressPage(_Properties):
    def __init__(self, name, shapes):
        self.Name = name
        self.Layout = 1
        self.shapes = shapes

    def getCount(self):
        return len(self.shapes)

    def getByIndex(self, index):
        return self.shapes[index]


class _ImpressPages:
    def __init__(self, pages):
        self.pages = pages

    def getCount(self):
        return len(self.pages)

    def getByIndex(self, index):
        return self.pages[index]


class _ImpressDocument:
    def __init__(self, pages):
        self.pages = _ImpressPages(pages)

    def getDrawPages(self):
        return self.pages


class _ParagraphCursor:
    def gotoEndOfParagraph(self, _expand):
        return True


class _ParagraphText:
    def __init__(self, document):
        self.document = document

    def createTextCursorByRange(self, _target):
        return _ParagraphCursor()

    def insertControlCharacter(self, _cursor, control, _absorb):
        if control != 0:
            raise ValueError(control)
        self.document.paragraphs.append("")

    def insertString(self, _cursor, value, _absorb):
        self.document.paragraphs[-1] = value


class _Paragraph(_Properties):
    def __init__(self, document, index):
        self.document = document
        self.index = index
        self.ParaStyleName = "Default Paragraph Style"
        self.ParaAdjust = 0

    @property
    def String(self):
        return self.document.paragraphs[self.index]

    @String.setter
    def String(self, value):
        self.document.paragraphs[self.index] = value

    def getText(self):
        return _ParagraphText(self.document)

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"


class _ParagraphEnumeration:
    def __init__(self, document):
        self.document = document
        self.index = 0

    def hasMoreElements(self):
        return self.index < len(self.document.paragraphs)

    def nextElement(self):
        paragraph = _Paragraph(self.document, self.index)
        self.index += 1
        return paragraph


class _StructuralText:
    def __init__(self, document):
        self.document = document

    def createEnumeration(self):
        return _ParagraphEnumeration(self.document)


class _StructuralWriterDocument(_WriterDocument):
    def __init__(self, first):
        self.paragraphs = [first]

    def getText(self):
        return _StructuralText(self)


class _CalcCell:
    def __init__(self, sheet_name, column, row):
        self.AbsoluteName = f"${sheet_name}.${semantic_agent._calc_a1(column, row)}"
        self.Value = float((row + 1) * 10 + column + 1)
        self.String = str(int(self.Value))
        self.Formula = self.String
        self.Type = "VALUE"
        self.NumberFormat = 0
        self.CellStyle = "Default"
        self.CellBackColor = -1
        self._address = SimpleNamespace(Sheet=1, Column=column, Row=row)

    def getCellAddress(self):
        return self._address


class _CalcRange:
    def __init__(self, address):
        self.address = address

    def getRangeAddress(self):
        return self.address


class _CalcSheet:
    def __init__(self, name, selection_address):
        self.name = name
        self.selection_address = selection_address
        self.requested_ranges = []

    def getCellRangeByName(self, name):
        self.requested_ranges.append(name)
        return _CalcRange(self.selection_address)

    def getCellByPosition(self, column, row):
        return _CalcCell(self.name, column, row)


class _CalcSheets:
    def __init__(self, selection_address):
        self.names = ["Summary", "Data"]
        self.values = {
            name: _CalcSheet(name, selection_address) for name in self.names
        }

    def getElementNames(self):
        return tuple(self.names)

    def hasByName(self, name):
        return name in self.values

    def getByName(self, name):
        return self.values[name]


class _CalcController:
    def __init__(self, sheets, selection_address):
        self.sheets = sheets
        self.selection = _CalcRange(selection_address)

    def getSelection(self):
        return self.selection

    def getActiveSheet(self):
        return self.sheets.getByName("Summary")


class _CalcDocument:
    def __init__(self, selection_address):
        self.sheets = _CalcSheets(selection_address)
        self.controller = _CalcController(self.sheets, selection_address)

    def supportsService(self, name):
        return name == "com.sun.star.sheet.SpreadsheetDocument"

    def getSheets(self):
        return self.sheets

    def getCurrentController(self):
        return self.controller


class LibreOfficeActionContractTests(unittest.TestCase):
    def _run(self, target, action, arguments):
        document = _WriterDocument()
        with ExitStack() as stack:
            stack.enter_context(patch.object(semantic_agent, "_resolve", return_value=target))
            stack.enter_context(
                patch.object(semantic_agent, "_uno_doc_for_object", return_value=document)
            )
            stack.enter_context(
                patch.object(
                    semantic_agent,
                    "_uno_document_record",
                    return_value={"ref": "document_ref", "modified": True},
                )
            )
            stack.enter_context(patch.object(semantic_agent, "_uno_document_records", return_value=[]))
            return semantic_agent._uno_action("target_ref", action, arguments)

    def test_every_advertised_action_has_an_argument_schema(self):
        descriptor = next(
            value for value in semantic_agent.CAPABILITIES
            if value["adapter_id"] == "libreoffice.uno@1"
        )
        self.assertEqual(set(descriptor["actions"]), set(descriptor["action_schemas"]))

    def test_calc_and_impress_resource_actions_match_entity_targets(self):
        descriptor = next(
            value for value in semantic_agent.CAPABILITIES
            if value["adapter_id"] == "libreoffice.uno@1"
        )
        resource_actions = descriptor["resource_actions"]
        self.assertEqual(
            set(resource_actions["spreadsheet.sheets"]),
            {
                "rename_sheet", "reorder_sheet", "add_sheet", "delete_sheet",
                "insert_rows", "delete_rows", "insert_columns", "delete_columns",
            },
        )
        self.assertEqual(
            set(resource_actions["presentation.slides"]),
            {"create_slide", "delete_slide", "set_slide_properties", "add_text_shape"},
        )
        self.assertEqual(
            set(resource_actions["presentation.shapes"]),
            {"replace_text", "set_shape_properties", "delete_shape"},
        )
        self.assertEqual(
            set(resource_actions["presentation.notes"]),
            {"add_text_shape"},
        )

    def test_impress_shape_labels_include_slide_and_object_context(self):
        document = _ImpressDocument([
            _ImpressPage("Opening", []),
            _ImpressPage("Plan", [_Shape()]),
        ])

        records = semantic_agent._impress_slide_records(document, include_shapes=True)

        shape = next(record for record in records if record["kind"] == "presentation.shape")
        self.assertEqual(shape["slide_index"], 1)
        self.assertEqual(shape["shape_index"], 0)
        self.assertEqual(
            shape["name"],
            "Slide 2 — Object 1 — Google Shape;84;p13",
        )
        self.assertEqual(shape["native_name"], "Google Shape;84;p13")

    def test_replace_text_applies_and_proves_text_font_color_and_alignment(self):
        target = _Properties()
        target.String = "before"
        target.CharHeight = 10.0
        target.CharColor = 0
        target.ParaAdjust = 0

        result = self._run(target, "replace_text", {
            "text": "after", "font_size": 18,
            "font_color": "#12AB34", "paragraph_alignment": "right",
        })

        self.assertEqual(target.String, "after")
        self.assertEqual(target.CharHeight, 18.0)
        self.assertEqual(target.CharColor, 0x12AB34)
        self.assertEqual(target.ParaAdjust, 1)
        self.assertTrue(result["text_evidence"]["matched"])
        self.assertEqual(
            set(result["property_evidence"]),
            {"CharHeight", "CharColor", "ParaAdjust"},
        )

    def test_table_cell_applies_and_proves_text_and_character_color(self):
        table = _Table()
        result = self._run(table, "set_table_cell", {
            "cell": "A1", "text": "all", "character_color": "#FF0000",
        })
        self.assertEqual(table.cell.String, "all")
        self.assertEqual(table.cell.CharColor, 0xFF0000)
        self.assertTrue(result["text_evidence"]["matched"])
        self.assertTrue(result["property_evidence"]["CharColor"]["matched"])

    def test_shape_applies_and_proves_alignment_font_color_and_geometry(self):
        shape = _Shape()
        result = self._run(shape, "set_shape_properties", {
            "paragraph_alignment": "center", "font_size": 22,
            "font_color": "#010203", "position": {"x": 300},
            "size": {"height": 500},
        })
        self.assertEqual(shape.ParaAdjust, 3)
        self.assertEqual(shape.CharHeight, 22.0)
        self.assertEqual(shape.CharColor, 0x010203)
        self.assertEqual(shape._position.X, 300)
        self.assertEqual(shape._size.Height, 500)
        self.assertTrue(result["property_evidence"]["ParaAdjust"]["matched"])

    def test_structural_paragraph_replacement_creates_real_blank_paragraphs(self):
        document = _StructuralWriterDocument("One. Two.")
        target = _Paragraph(document, 0)
        with ExitStack() as stack:
            stack.enter_context(patch.object(semantic_agent, "_resolve", return_value=target))
            stack.enter_context(
                patch.object(semantic_agent, "_uno_doc_for_object", return_value=document)
            )
            stack.enter_context(
                patch.object(
                    semantic_agent,
                    "_uno_document_record",
                    return_value={"ref": "document_ref", "modified": True},
                )
            )
            stack.enter_context(
                patch.object(semantic_agent, "_uno_document_records", return_value=[])
            )
            result = semantic_agent._uno_action(
                "target_ref",
                "replace_with_paragraphs",
                {"paragraphs": ["One.", "", "Two."]},
            )

        self.assertEqual(document.paragraphs, ["One.", "", "Two."])
        self.assertTrue(result["paragraph_evidence"]["matched"])

    def test_ignored_or_unknown_arguments_are_rejected(self):
        target = _Properties()
        target.String = "before"
        with self.assertRaises(semantic_agent.AgentError) as raised:
            self._run(target, "replace_text", {"text": "after", "font_size_twips": 360})
        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertEqual(target.String, "before")

        target.ParaAdjust = 0
        with self.assertRaises(semantic_agent.AgentError) as raised:
            self._run(
                target,
                "set_paragraph_properties",
                {"properties": {"EvaluatorExpectedAlignment": 3}},
            )
        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertEqual(target.ParaAdjust, 0)


class LibreOfficeSelectionQueryContractTests(unittest.TestCase):
    def test_spreadsheet_selection_defaults_to_controller_selection(self):
        selected = SimpleNamespace(
            Sheet=1,
            StartColumn=1,
            StartRow=1,
            EndColumn=2,
            EndRow=2,
        )
        document = _CalcDocument(selected)

        with patch.object(
            semantic_agent, "_uno_document_records", return_value=[]
        ), patch.object(
            semantic_agent, "_uno_active_document", return_value=document
        ):
            records, _revision = semantic_agent._build_uno_snapshot(
                "spreadsheet.selection",
                {"scope": {}, "parameters": {}},
            )

        # The controller points at the second sheet and B2:C3. An omitted range
        # must not silently fall back to the active sheet's A1 cell.
        self.assertEqual(
            document.sheets.getByName("Data").requested_ranges,
            ["B2:C3"],
        )
        self.assertEqual(
            [(record["sheet"], record["address"]) for record in records],
            [
                ("Data", "B2"),
                ("Data", "C2"),
                ("Data", "B3"),
                ("Data", "C3"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
