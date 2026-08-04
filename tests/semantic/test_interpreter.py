from __future__ import annotations

import time
import unittest

from envserver.semantic.interpreter import InterpreterLimits, SemanticInterpreter
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.state import EpisodeState


class FakeComputer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def query(self, *args, **kwargs):
        self.calls.append(("query", args, kwargs))
        return [
            {"name": "  Alpha ", "enabled": True},
            {"name": "Beta", "enabled": False},
            {"name": "Gamma", "enabled": True},
        ]

    def act(self, *args, **kwargs):
        self.calls.append(("act", args, kwargs))
        return {"changed": True}

    def verify(self, *args, **kwargs):
        self.calls.append(("verify", args, kwargs))
        return {"passed": True}


class InterpreterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.computer = FakeComputer()
        self.interpreter = SemanticInterpreter()

    def test_allowed_program_and_exact_computer_surface(self) -> None:
        result = self.interpreter.execute(
            """
rows = computer.query({"resource": "controls"})
names = []
for index, row in enumerate(rows):
    if not row["enabled"]:
        continue
    names.append(row["name"].strip().lower())
    if len(names) == 2:
        break
emit("|".join(names))
computer.act({"action": "save", "names": names})
computer.verify({"kind": "count", "value": len(names)})
names[0:2]
""",
            computer=self.computer,
        )
        self.assertEqual(result.value, ["alpha", "gamma"])
        self.assertEqual(result.output, ("alpha|gamma",))
        self.assertEqual(result.loop_iterations, 3)
        self.assertEqual(result.semantic_operations, 3)
        self.assertEqual(result.operation_count, 3)
        self.assertEqual(result.applied_operations, 1)
        self.assertEqual(
            set(result.to_dict()),
            {"value", "output", "operation_count", "applied_operations", "failed_operation"},
        )
        self.assertEqual([entry[0] for entry in self.computer.calls], ["query", "act", "verify"])

    def test_safe_builtins_methods_assignment_and_arithmetic(self) -> None:
        result = self.interpreter.execute(
            """
values = list(range(5))
values.extend([5, 6])
mapping = dict([("total", sum(values))])
mapping["power"] = 2 ** 8
pair = list(zip(["a", "b"], sorted([2, 1])))
emit(mapping)
[mapping.get("total"), mapping["power"], pair]
""",
            computer=self.computer,
        )
        self.assertEqual(result.value[0:2], [21, 256])
        self.assertEqual(result.value[2], [("a", 1), ("b", 2)])

    def test_disallowed_syntax_and_attribute_access(self) -> None:
        cases = {
            "import os": ErrorCode.POLICY_VIOLATION,
            "while True:\n    break": ErrorCode.POLICY_VIOLATION,
            "f = lambda x: x": ErrorCode.POLICY_VIOLATION,
            "def f():\n    return 1": ErrorCode.POLICY_VIOLATION,
            "class X:\n    pass": ErrorCode.POLICY_VIOLATION,
            "try:\n    x = 1\nexcept:\n    x = 2": ErrorCode.POLICY_VIOLATION,
            "with open('x'):\n    x = 1": ErrorCode.POLICY_VIOLATION,
            "raise ValueError('x')": ErrorCode.POLICY_VIOLATION,
            "async def f():\n    await g()": ErrorCode.POLICY_VIOLATION,
            "def f():\n    yield 1": ErrorCode.POLICY_VIOLATION,
            "(x for x in [1])": ErrorCode.POLICY_VIOLATION,
            "computer.run('x')": ErrorCode.POLICY_VIOLATION,
            "'x'.__class__": ErrorCode.POLICY_VIOLATION,
            "computer.delete({})": ErrorCode.POLICY_VIOLATION,
            "open('x')": ErrorCode.POLICY_VIOLATION,
            "any([True])": ErrorCode.POLICY_VIOLATION,
            "all([True])": ErrorCode.POLICY_VIOLATION,
            "'x'.upper()": ErrorCode.POLICY_VIOLATION,
            "{}.pop('x')": ErrorCode.POLICY_VIOLATION,
            "[].sort()": ErrorCode.POLICY_VIOLATION,
            "{**{'x': 1}}": ErrorCode.POLICY_VIOLATION,
            "[x for x in [1]]": ErrorCode.POLICY_VIOLATION,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                with self.assertRaises(ProtocolError) as caught:
                    self.interpreter.execute(source, computer=self.computer)
                self.assertEqual(caught.exception.code, expected)

    def test_loop_semantic_episode_power_and_output_budgets(self) -> None:
        with self.assertRaises(ProtocolError) as loops:
            self.interpreter.execute(
                "for i in range(1001):\n    x = i", computer=self.computer
            )
        self.assertEqual(loops.exception.code, ErrorCode.BUDGET_EXHAUSTED)

        semantic = self.interpreter.execute(
            "for i in range(51):\n    computer.query({})", computer=self.computer
        )
        self.assertEqual(
            semantic.failed_operation["error"]["code"],
            ErrorCode.BUDGET_EXHAUSTED.value,
        )

        state = EpisodeState("episode", max_tool_calls=1)
        episode = self.interpreter.execute(
            "for i in range(11):\n    computer.query({})",
            computer=self.computer,
            episode_state=state,
        )
        self.assertEqual(
            episode.failed_operation["error"]["code"],
            ErrorCode.BUDGET_EXHAUSTED.value,
        )

        with self.assertRaises(ProtocolError) as power:
            self.interpreter.execute("2 ** 17", computer=self.computer)
        self.assertEqual(power.exception.code, ErrorCode.BUDGET_EXHAUSTED)

        with self.assertRaises(ProtocolError) as output:
            self.interpreter.execute("emit('x' * 13000)", computer=self.computer)
        self.assertEqual(output.exception.code, ErrorCode.BUDGET_EXHAUSTED)

        with self.assertRaises(ProtocolError) as slicing:
            self.interpreter.execute("[1, 2, 3][0:6000]", computer=self.computer)
        self.assertEqual(slicing.exception.code, ErrorCode.BUDGET_EXHAUSTED)

    def test_source_ast_collection_and_wall_limits_are_typed(self) -> None:
        tiny = SemanticInterpreter(InterpreterLimits(max_source_chars=10))
        with self.assertRaises(ProtocolError) as source:
            tiny.execute("value = 123456789", computer=self.computer)
        self.assertEqual(source.exception.code, ErrorCode.BUDGET_EXHAUSTED)

        tiny_collection = SemanticInterpreter(InterpreterLimits(max_collection_items=3))
        with self.assertRaises(ProtocolError) as collection:
            tiny_collection.execute("list(range(4))", computer=self.computer)
        self.assertEqual(collection.exception.code, ErrorCode.BUDGET_EXHAUSTED)

        for source in ("len(1)", "+'x'", "round('x')"):
            with self.subTest(typed_invalid=source):
                with self.assertRaises(ProtocolError) as invalid:
                    self.interpreter.execute(source, computer=self.computer)
                self.assertEqual(invalid.exception.code, ErrorCode.INVALID_REQUEST)

        class SlowComputer(FakeComputer):
            def query(self, *args, **kwargs):
                time.sleep(0.02)
                return []

        timed = SemanticInterpreter(InterpreterLimits(wall_seconds=0.001))
        timeout = timed.execute("computer.query({})", computer=SlowComputer())
        self.assertEqual(
            timeout.failed_operation["error"]["code"], ErrorCode.TIMEOUT.value
        )

    def test_partial_failure_preserves_prior_applied_operations(self) -> None:
        class FailingComputer(FakeComputer):
            def verify(self, *args, **kwargs):
                raise ProtocolError(ErrorCode.POSTCONDITION_FAILED, "not proven")

        result = self.interpreter.execute(
            "computer.act({})\ncomputer.verify({})",
            computer=FailingComputer(),
        )
        self.assertEqual(result.operation_count, 2)
        self.assertEqual(result.applied_operations, 1)
        self.assertEqual(result.failed_operation["index"], 1)
        self.assertEqual(
            result.failed_operation["error"]["code"],
            ErrorCode.POSTCONDITION_FAILED.value,
        )


if __name__ == "__main__":
    unittest.main()
