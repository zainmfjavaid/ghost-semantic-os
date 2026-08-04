"""Contracts for native package/extension plumbing and structural office edits."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from guest_agent import package_installer
from guest_agent import semantic_agent


def _package(name: str, *, installed: bool, version: str | None = None) -> dict:
    return {
        "kind": "os.package", "name": name, "installed": installed,
        "version": version, "architecture": "amd64" if installed else None,
        "status": "ii " if installed else "not-installed",
        "advertised_actions": ["install_package"],
        "source": "dpkg-query", "freshness": "live",
    }


def _extension(identifier: str, *, installed: bool, version: str | None = None) -> dict:
    return {
        "kind": "libreoffice.extension", "identifier": identifier,
        "display_name": "Example", "version": version, "url": None,
        "installed": installed, "enabled": installed,
        "registration_state": "registered" if installed else "absent",
        "advertised_actions": ["install_extension"],
        "source": "unopkg", "freshness": "live",
    }


class NativePackageContractTests(unittest.TestCase):
    def test_package_helper_preflight_failure_proves_no_mutation(self) -> None:
        failed = type("Completed", (), {
            "returncode": 100,
            "stdout": "",
            "stderr": "E: Unable to locate package missing-package\n",
        })()
        with ExitStack() as stack:
            stack.enter_context(patch.object(package_installer.os, "geteuid", return_value=0))
            simulated = stack.enter_context(patch.object(
                package_installer.subprocess, "run", return_value=failed
            ))
            execute = stack.enter_context(patch.object(package_installer.os, "execve"))
            status = package_installer.main(["package_installer.py", "missing-package"])
        self.assertEqual(status, package_installer._PREFLIGHT_NO_EFFECT)
        execute.assert_not_called()
        self.assertIn("--simulate", simulated.call_args.args[0])

    def test_package_helper_crosses_mutation_boundary_only_after_preflight(self) -> None:
        passed = type("Completed", (), {
            "returncode": 0, "stdout": "Inst package", "stderr": "",
        })()
        with ExitStack() as stack:
            stack.enter_context(patch.object(package_installer.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(
                package_installer.subprocess, "run", return_value=passed
            ))
            execute = stack.enter_context(patch.object(package_installer.os, "execve"))
            status = package_installer.main(["package_installer.py", "example-utils"])
        self.assertEqual(status, 70, "mocked execve returns only in the test process")
        actual_argv = execute.call_args.args[1]
        self.assertIn("--yes", actual_argv)
        self.assertNotIn("--simulate", actual_argv)

    def test_package_query_parses_registry_and_exposes_absent_exact_name(self) -> None:
        response = {
            "argv": [], "exit_code": 0,
            "stdout": "example-utils\tii \t1.2.3\tamd64\n",
            "stderr": "",
        }
        with patch.object(semantic_agent, "_bounded_command", return_value=response):
            installed = semantic_agent._package_records("example-utils")
        self.assertTrue(installed[0]["installed"])
        self.assertEqual(installed[0]["version"], "1.2.3")

        with patch.object(
            semantic_agent, "_bounded_command",
            return_value={"argv": [], "exit_code": 1, "stdout": "", "stderr": "missing"},
        ):
            absent = semantic_agent._package_records("missing-package")
        self.assertEqual(absent[0]["name"], "missing-package")
        self.assertFalse(absent[0]["installed"])

    def test_package_install_uses_fixed_argv_and_verifies_dpkg_state(self) -> None:
        command = {"argv": [], "exit_code": 0, "stdout": "ok", "stderr": ""}
        with ExitStack() as stack:
            registry = stack.enter_context(patch.object(
                semantic_agent, "_package_records",
                side_effect=[
                    [_package("example-utils", installed=False)],
                    [_package("example-utils", installed=True, version="1.2.3")],
                ],
            ))
            run = stack.enter_context(patch.object(
                semantic_agent, "_bounded_native_mutation", return_value=command
            ))
            result = semantic_agent._install_os_package({"name": "example-utils"})

        self.assertEqual(registry.call_count, 2)
        run.assert_called_once_with(
            [
                "sudo", "-n", "--",
                "/usr/local/libexec/ghost-semantic-install-package",
                "example-utils",
            ],
            timeout_seconds=180,
        )
        self.assertEqual(result["execution_path"], "native_api")
        self.assertTrue(result["postcondition"]["installed"])

    def test_package_name_injection_and_privilege_failure_are_typed(self) -> None:
        for value in ("-oDebug=true", "example;id", "example other", "Example"):
            with self.subTest(value=value), self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent._validated_package_name(value)
            self.assertEqual(raised.exception.code, "invalid_request")

        denied = {
            "argv": [], "exit_code": 1, "stdout": "",
            "stderr": "sudo: a password is required",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                semantic_agent, "_package_records",
                side_effect=[
                    [_package("example-utils", installed=False)],
                    [_package("example-utils", installed=False)],
                ],
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_bounded_native_mutation", return_value=denied
            ))
            with self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent._install_os_package({"name": "example-utils"})
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertEqual(raised.exception.side_effect_state, "none")

    def test_package_preflight_no_candidate_is_not_uncertain(self) -> None:
        no_candidate = {
            "argv": [], "exit_code": package_installer._PREFLIGHT_NO_EFFECT,
            "stdout": "", "stderr": "package preflight failed without mutation",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                semantic_agent, "_package_records",
                side_effect=[
                    [_package("missing-package", installed=False)],
                    [_package("missing-package", installed=False)],
                ],
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_bounded_native_mutation", return_value=no_candidate
            ))
            with self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent._install_os_package({"name": "missing-package"})
        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(raised.exception.side_effect_state, "none")


class LibreOfficeExtensionContractTests(unittest.TestCase):
    def _oxt(self, directory: str) -> Path:
        import zipfile

        path = Path(directory) / "example.oxt"
        description = b"""<?xml version='1.0'?>
<description xmlns='http://openoffice.org/extensions/description/2006'>
  <identifier value='org.example.semantic'/>
  <version value='2.0'/>
  <display-name><name lang='en'>Example Semantic Extension</name></display-name>
</description>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("description.xml", description)
        return path

    def test_oxt_metadata_is_parsed_and_identifier_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            metadata = semantic_agent._oxt_metadata(self._oxt(raw))
        self.assertEqual(metadata, {
            "identifier": "org.example.semantic", "version": "2.0",
            "display_name": "Example Semantic Extension",
        })

    def test_extension_install_uses_unopkg_argv_and_verifies_registry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._oxt(raw)
            command = {"argv": [], "exit_code": 0, "stdout": "ok", "stderr": ""}
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    semantic_agent, "_libreoffice_extension_records",
                    side_effect=[
                        [_extension("org.example.semantic", installed=False)],
                        [_extension("org.example.semantic", installed=True, version="2.0")],
                    ],
                ))
                stack.enter_context(patch.object(
                    semantic_agent, "_bounded_command",
                    return_value={"argv": [], "exit_code": 0, "stdout": "123", "stderr": ""},
                ))
                run = stack.enter_context(patch.object(
                    semantic_agent, "_bounded_native_mutation", return_value=command
                ))
                result = semantic_agent._install_libreoffice_extension({"path": str(path)})

        run.assert_called_once_with(
            ["unopkg", "add", "--force", "--suppress-license", str(path.resolve())],
            timeout_seconds=120,
        )
        self.assertEqual(result["execution_path"], "native_api")
        self.assertTrue(result["libreoffice_restart_required"])
        self.assertTrue(result["postcondition"]["enabled"])
        self.assertEqual(result["postcondition"]["registration_state"], "registered")

    def test_unopkg_output_is_queryable(self) -> None:
        output = """All deployed user extensions:
Identifier: org.example.semantic
Version: 2.0
URL: vnd.sun.star.expand:$UNO_USER_PACKAGES_CACHE/example
is registered: yes
"""
        records = semantic_agent._parse_unopkg_extensions(output)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["identifier"], "org.example.semantic")
        self.assertTrue(records[0]["installed"])
        self.assertTrue(records[0]["enabled"])
        self.assertEqual(records[0]["registration_state"], "registered")

    def test_unopkg_registration_states_are_not_conflated_with_installation(self) -> None:
        template = """All deployed user extensions:
Identifier: org.example.{suffix}
Version: 2.0
URL: vnd.sun.star.expand:$UNO_USER_PACKAGES_CACHE/example
is registered: {registered}
"""
        cases = (
            ("empty", "n/a", "not_applicable", None),
            ("disabled", "no", "not_registered", False),
            ("mixed", "ambiguous", "ambiguous", None),
            ("unknown", "unexpected", "unknown", None),
        )
        for suffix, raw, state, enabled in cases:
            with self.subTest(raw=raw):
                records = semantic_agent._parse_unopkg_extensions(
                    template.format(suffix=suffix, registered=raw)
                )
                self.assertEqual(len(records), 1)
                self.assertTrue(records[0]["installed"])
                self.assertEqual(records[0]["registration_state"], state)
                self.assertIs(records[0]["enabled"], enabled)

    def test_empty_bundle_install_is_proven_without_inventing_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._oxt(raw)
            before = _extension("org.example.semantic", installed=False)
            after = {
                **_extension("org.example.semantic", installed=True, version="2.0"),
                "enabled": None,
                "registration_state": "not_applicable",
            }
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    semantic_agent, "_libreoffice_extension_records",
                    side_effect=[[before], [after]],
                ))
                stack.enter_context(patch.object(
                    semantic_agent, "_bounded_command",
                    return_value={"argv": [], "exit_code": 1, "stdout": "", "stderr": ""},
                ))
                stack.enter_context(patch.object(
                    semantic_agent, "_bounded_native_mutation",
                    return_value={"argv": [], "exit_code": 0, "stdout": "", "stderr": ""},
                ))
                result = semantic_agent._install_libreoffice_extension({"path": str(path)})

        self.assertTrue(result["installed"])
        self.assertIsNone(result["enabled"])
        self.assertEqual(result["registration_state"], "not_applicable")
        self.assertIsNone(result["postcondition"]["enabled"])

    def test_install_rejects_a_stale_registry_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._oxt(raw)
            before = _extension("org.example.semantic", installed=False)
            stale = _extension("org.example.semantic", installed=True, version="1.0")
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    semantic_agent, "_libreoffice_extension_records",
                    side_effect=[[before], [stale]],
                ))
                stack.enter_context(patch.object(
                    semantic_agent, "_bounded_command",
                    return_value={"argv": [], "exit_code": 1, "stdout": "", "stderr": ""},
                ))
                stack.enter_context(patch.object(
                    semantic_agent, "_bounded_native_mutation",
                    return_value={"argv": [], "exit_code": 0, "stdout": "", "stderr": ""},
                ))
                with self.assertRaises(semantic_agent.AgentError) as raised:
                    semantic_agent._install_libreoffice_extension({"path": str(path)})
        self.assertEqual(raised.exception.code, "postcondition_failed")


class _WriterDocument:
    def __init__(self, paragraphs: list[str], target_index: int) -> None:
        self.paragraphs = list(paragraphs)
        self.target = _WriterParagraph(self, target_index)
        self.text = _WriterText(self)

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.text.TextDocument"

    def isModified(self) -> bool:
        return True

    def getText(self):
        return self.text


class _WriterParagraph:
    def __init__(self, document: _WriterDocument, index: int) -> None:
        self.document = document
        self.index = index

    def getText(self):
        return self.document.text


class _WriterCursor:
    def __init__(self, document: _WriterDocument, position: int) -> None:
        self.document = document
        self.position = position
        self.pending: str | None = None
        self.last_created: int | None = None
        self.mode = "after"

    def gotoStartOfParagraph(self, _expand: bool) -> bool:
        self.position = self.document.target.index
        self.mode = "before"
        return True

    def gotoEndOfParagraph(self, _expand: bool) -> bool:
        self.position = self.document.target.index + 1
        self.mode = "after"
        return True

    def gotoEnd(self, _expand: bool) -> bool:
        self.position = len(self.document.paragraphs)
        self.mode = "after"
        return True


class _WriterText:
    def __init__(self, document: _WriterDocument) -> None:
        self.document = document

    def createTextCursorByRange(self, target: _WriterParagraph) -> _WriterCursor:
        return _WriterCursor(self.document, target.index)

    def createTextCursor(self) -> _WriterCursor:
        return _WriterCursor(self.document, len(self.document.paragraphs))

    def insertString(self, cursor: _WriterCursor, value: str, _absorb: bool) -> None:
        if cursor.mode == "after" and cursor.last_created is not None:
            self.document.paragraphs[cursor.last_created] += value
            cursor.last_created = None
        else:
            cursor.pending = (cursor.pending or "") + value

    def insertControlCharacter(self, cursor: _WriterCursor, control: int, _absorb: bool) -> None:
        if control != 0:
            raise ValueError(control)
        value = cursor.pending if cursor.pending is not None else ""
        self.document.paragraphs.insert(cursor.position, value)
        if cursor.position <= self.document.target.index:
            self.document.target.index += 1
        cursor.last_created = cursor.position if cursor.pending is None else None
        cursor.pending = None
        cursor.position += 1


class WriterInsertionContractTests(unittest.TestCase):
    def _records(self, document: _WriterDocument) -> list[dict]:
        return [
            {
                "ref": "target_ref" if index == document.target.index else f"paragraph_{index}",
                "text": value,
            }
            for index, value in enumerate(document.paragraphs)
        ]

    def _insert(self, position: str) -> tuple[_WriterDocument, dict]:
        document = _WriterDocument(["first", "target", "last"], 1)
        with ExitStack() as stack:
            stack.enter_context(patch.object(semantic_agent, "_resolve", return_value=document.target))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_doc_for_object", return_value=document
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_writer_paragraph_records",
                side_effect=lambda doc: self._records(doc),
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_document_record",
                return_value={"ref": "document_ref", "modified": True},
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_document_records", return_value=[]
            ))
            result = semantic_agent._uno_action(
                "target_ref", "insert_paragraphs",
                {"paragraphs": ["new-a", "", "new-b"], "position": position},
            )
        return document, result

    def test_insert_paragraphs_has_exact_before_after_and_end_semantics(self) -> None:
        expected = {
            "before": ["first", "new-a", "", "new-b", "target", "last"],
            "after": ["first", "target", "new-a", "", "new-b", "last"],
            "end": ["first", "target", "last", "new-a", "", "new-b"],
        }
        for position, paragraphs in expected.items():
            with self.subTest(position=position):
                document, result = self._insert(position)
                self.assertEqual(document.paragraphs, paragraphs)
                self.assertTrue(result["paragraph_evidence"]["matched"])
                self.assertEqual(result["paragraph_evidence"]["position"], position)


class _CalcDocument:
    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.sheet.SpreadsheetDocument"

    def isModified(self) -> bool:
        return True


class _ReloadlessCalcDocument:
    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.sheet.SpreadsheetDocument"

    def isModified(self) -> bool:
        return False


class _FailingReloadCalcDocument(_ReloadlessCalcDocument):
    def reload(self) -> None:
        raise RuntimeError("transport disappeared")


class _Range:
    def __init__(self) -> None:
        self.data = ()
        self.formulas = ()

    def setDataArray(self, data) -> None:
        self.data = tuple(tuple(float(value) if isinstance(value, int) else value for value in row) for row in data)

    def getDataArray(self):
        return self.data

    def setFormulaArray(self, formulas) -> None:
        self.formulas = tuple(tuple(row) for row in formulas)

    def getFormulaArray(self):
        return self.formulas


class CalcRangeEvidenceContractTests(unittest.TestCase):
    def _run(self, target: _Range, action: str, arguments: dict) -> dict:
        document = _CalcDocument()
        with ExitStack() as stack:
            stack.enter_context(patch.object(semantic_agent, "_resolve", return_value=target))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_doc_for_object", return_value=document
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_document_record",
                return_value={"ref": "document_ref", "modified": True},
            ))
            stack.enter_context(patch.object(
                semantic_agent, "_uno_document_records", return_value=[]
            ))
            return semantic_agent._uno_action("range_ref", action, arguments)

    def test_range_values_and_formulas_are_reread_with_compact_evidence(self) -> None:
        values = self._run(_Range(), "set_range_values", {"values": [[1, "x"], [2, "y"]]})
        self.assertEqual(values["range_evidence"]["rows"], 2)
        self.assertEqual(values["range_evidence"]["columns"], 2)
        self.assertTrue(values["range_evidence"]["matched"])
        self.assertNotIn("values", values["range_evidence"])

        formulas = self._run(
            _Range(), "set_range_formulas", {"formulas": [["=A1+1", "=B1+1"]]}
        )
        self.assertEqual(formulas["range_evidence"]["kind"], "formulas")
        self.assertTrue(formulas["range_evidence"]["matched"])

    def test_range_mismatch_is_postcondition_failure_after_applied_mutation(self) -> None:
        target = _Range()
        target.getDataArray = lambda: ((999.0,),)
        with self.assertRaises(semantic_agent.AgentError) as raised:
            self._run(target, "set_range_values", {"values": [[1]]})
        self.assertEqual(raised.exception.code, "postcondition_failed")
        self.assertEqual(raised.exception.side_effect_state, "applied")


class NativeRouteClassificationTests(unittest.TestCase):
    def test_missing_libreoffice_reload_interface_is_unsupported_without_effect(self) -> None:
        document = _ReloadlessCalcDocument()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                semantic_agent, "_resolve", return_value=document
            ))
            with self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent._uno_action("document_ref", "reload", {})
        self.assertEqual(raised.exception.code, "unsupported")
        self.assertEqual(raised.exception.side_effect_state, "none")

    def test_libreoffice_reload_failure_after_invocation_remains_uncertain(self) -> None:
        document = _FailingReloadCalcDocument()
        with patch.object(semantic_agent, "_resolve", return_value=document):
            with self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent._uno_action("document_ref", "reload", {})
        self.assertEqual(raised.exception.code, "uncertain")
        self.assertEqual(raised.exception.side_effect_state, "unknown")

    def test_desktop_launch_dispatch_does_not_capture_child_pipes(self) -> None:
        process = type("Process", (), {"pid": 4242})()
        with patch.object(
            semantic_agent.subprocess, "Popen", return_value=process
        ) as spawned:
            result = semantic_agent._dispatch_desktop_entry("google-chrome.desktop")
        self.assertEqual(result["dispatch_state"], "accepted")
        self.assertEqual(result["launcher_pid"], 4242)
        kwargs = spawned.call_args.kwargs
        self.assertIs(kwargs["stdout"], semantic_agent.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], semantic_agent.subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])


class CapabilityMetadataContractTests(unittest.TestCase):
    def test_new_resources_publish_typed_actions_and_risks(self) -> None:
        descriptors = {item["adapter_id"]: item for item in semantic_agent.CAPABILITIES}
        os_descriptor = descriptors["guest-os@1"]
        office = descriptors["libreoffice.uno@1"]

        self.assertEqual(os_descriptor["resource_actions"]["os.packages"], ["install_package"])
        self.assertEqual(os_descriptor["risk_classes"]["install_package"], "persistent")
        self.assertFalse(
            os_descriptor["action_schemas"]["install_package"]["additionalProperties"]
        )
        self.assertEqual(
            office["resource_actions"]["libreoffice.extensions"], ["install_extension"]
        )
        self.assertEqual(office["risk_classes"]["install_extension"], "persistent")
        self.assertIn("insert_paragraphs", office["resource_actions"]["writer.paragraphs"])
        self.assertEqual(office["risk_classes"]["insert_paragraphs"], "reversible")


if __name__ == "__main__":
    unittest.main()
