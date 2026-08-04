"""Restricted AST interpreter for bounded ``computer.run`` programs.

This is intentionally not ``eval``/``exec`` with filtered globals.  Each AST
node is interpreted directly, values remain primitive and bounded, and the only
semantic side effects are explicit ``computer.query``, ``computer.act``, and
``computer.verify`` calls.  ``computer.run`` is never recursively available.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .protocol import ErrorCode, ProtocolError
from .state import EpisodeState


@dataclass(frozen=True)
class InterpreterLimits:
    max_source_chars: int = 12_000
    max_ast_nodes: int = 2_000
    max_nesting: int = 32
    max_loop_iterations: int = 1_000
    max_semantic_operations: int = 50
    max_collection_items: int = 5_000
    max_string_chars: int = 65_536
    max_emitted_chars: int = 12_000
    wall_seconds: float = 30.0
    max_power_exponent: int = 16


class ComputerOperations(Protocol):
    def query(self, *args: Any, **kwargs: Any) -> Any: ...
    def act(self, *args: Any, **kwargs: Any) -> Any: ...
    def verify(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RunResult:
    value: Any
    output: tuple[Any, ...]
    operation_count: int
    applied_operations: int
    failed_operation: Mapping[str, Any] | None
    variables: Mapping[str, Any]
    ast_nodes: int
    loop_iterations: int
    semantic_operations: int
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        # Exact RunResultSchema shape.  Detailed counters/variables remain
        # server-side diagnostics and are not transported to the model.
        return {
            "value": _json_value(self.value),
            "output": [_json_value(value) for value in self.output],
            "operation_count": self.operation_count,
            "applied_operations": self.applied_operations,
            "failed_operation": (
                dict(self.failed_operation) if self.failed_operation is not None else None
            ),
        }


SAFE_BUILTINS = frozenset(
    {
        "len",
        "range",
        "enumerate",
        "zip",
        "sorted",
        "min",
        "max",
        "sum",
        "round",
        "abs",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "emit",
    }
)
STRING_METHODS = frozenset(
    {
        "lower",
        "casefold",
        "strip",
        "split",
        "replace",
        "startswith",
        "endswith",
        "join",
    }
)
DICT_METHODS = frozenset({"get", "keys", "values", "items"})
LIST_METHODS = frozenset({"append", "extend", "count", "index"})
COMPUTER_METHODS = frozenset({"query", "act", "verify"})


_ALLOWED_NODES = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.If,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.Attribute,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Load,
    ast.Store,
    ast.keyword,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return [_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class SemanticInterpreter:
    def __init__(self, limits: InterpreterLimits | None = None) -> None:
        self.limits = limits or InterpreterLimits()

    def execute(
        self,
        source: str,
        *,
        computer: ComputerOperations,
        episode_state: EpisodeState | None = None,
        variables: Mapping[str, Any] | None = None,
    ) -> RunResult:
        if not isinstance(source, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "run source must be a string")
        if len(source) > self.limits.max_source_chars:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "run source exceeds 12000 characters"
            )
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"invalid run syntax at line {exc.lineno}: {exc.msg}",
            ) from exc
        nodes = list(ast.walk(tree))
        if len(nodes) > self.limits.max_ast_nodes:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "run AST exceeds 2000 nodes")
        self._validate_tree(tree)
        initial = dict(variables or {})
        for name, value in initial.items():
            self._validate_name(name, assignment=True)
            self._validate_value(value)
        overlap = set(initial) & (SAFE_BUILTINS | {"computer"})
        if overlap:
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION,
                f"reserved run names cannot be assigned: {sorted(overlap)!r}",
            )
        runtime = _Runtime(
            interpreter=self,
            computer=computer,
            episode_state=episode_state,
            variables=initial,
            ast_nodes=len(nodes),
        )
        try:
            runtime.run(tree)
            return runtime.result()
        except ProtocolError as error:
            # Syntax/AST/value failures before the first semantic operation are
            # ordinary rejected run requests. Once semantic work has begun,
            # preserve its exact partial side effects and identify the failing
            # operation instead of implying rollback.
            if runtime.semantic_operations == 0:
                raise
            return runtime.result(failed_error=error)

    def _validate_tree(self, tree: ast.AST) -> None:
        def walk(node: ast.AST, depth: int) -> None:
            if depth > self.limits.max_nesting:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED, "run AST nesting exceeds 32"
                )
            if not isinstance(node, _ALLOWED_NODES):
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION,
                    f"run syntax is not allowed: {type(node).__name__}",
                )
            if isinstance(node, ast.Name):
                self._validate_name(node.id, assignment=isinstance(node.ctx, ast.Store))
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("_"):
                    raise ProtocolError(
                        ErrorCode.POLICY_VIOLATION, "private/dunder attributes are forbidden"
                    )
            if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, "expanded dict entries are forbidden"
                )
            if isinstance(node, ast.For) and node.orelse:
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, "for-else is not allowed in computer.run"
                )
            for child in ast.iter_child_nodes(node):
                walk(child, depth + 1)

        walk(tree, 0)

    @staticmethod
    def _validate_name(name: Any, *, assignment: bool = False) -> None:
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "private/dunder names are forbidden")
        if assignment and name in SAFE_BUILTINS | {"computer"}:
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION, f"reserved run name cannot be assigned: {name}"
            )

    def _validate_value(self, value: Any, *, depth: int = 0) -> None:
        if depth > self.limits.max_nesting:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "value nesting exceeds 32")
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int):
            if value.bit_length() > 4096:
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "integer value is too large")
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "non-finite numbers are forbidden")
            return
        if isinstance(value, str):
            if len(value) > self.limits.max_string_chars:
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "string exceeds 64 KiB")
            return
        if isinstance(value, (list, tuple, set)):
            if len(value) > self.limits.max_collection_items:
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items")
            for item in value:
                self._validate_value(item, depth=depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > self.limits.max_collection_items:
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items")
            for key, item in value.items():
                if not isinstance(key, (str, int, float, bool)) and key is not None:
                    raise ProtocolError(
                        ErrorCode.POLICY_VIOLATION, "dict keys must be primitive"
                    )
                self._validate_value(key, depth=depth + 1)
                self._validate_value(item, depth=depth + 1)
            return
        raise ProtocolError(
            ErrorCode.POLICY_VIOLATION,
            f"run value type is not allowed: {type(value).__name__}",
        )


class _Runtime:
    def __init__(
        self,
        *,
        interpreter: SemanticInterpreter,
        computer: ComputerOperations,
        episode_state: EpisodeState | None,
        variables: dict[str, Any],
        ast_nodes: int,
    ) -> None:
        self.owner = interpreter
        self.limits = interpreter.limits
        self.computer = computer
        self.episode_state = episode_state
        self.variables = variables
        self.ast_nodes = ast_nodes
        self.loop_iterations = 0
        self.semantic_operations = 0
        self.applied_operations = 0
        self.emitted: list[Any] = []
        self.last: Any = None
        self.started = time.monotonic()

    def check_time(self) -> None:
        if time.monotonic() - self.started > self.limits.wall_seconds:
            raise ProtocolError(ErrorCode.TIMEOUT, "computer.run exceeded 30 seconds")

    def run(self, tree: ast.Module) -> None:
        for statement in tree.body:
            self._statement(statement)

    def result(self, failed_error: ProtocolError | None = None) -> RunResult:
        if failed_error is None:
            self.check_time()
        public_variables = {
            name: value for name, value in self.variables.items() if not name.startswith("_")
        }
        candidate = {
            "value": _json_value(self.last),
            "emitted": [_json_value(item) for item in self.emitted],
        }
        try:
            encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION, "run output is not serializable"
            ) from exc
        if len(encoded) > self.limits.max_emitted_chars:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "emitted run output exceeds 12000 characters"
            )
        return RunResult(
            value=self.last,
            output=tuple(self.emitted),
            operation_count=self.semantic_operations,
            applied_operations=self.applied_operations,
            failed_operation=(
                {
                    "index": max(0, self.semantic_operations - 1),
                    "error": failed_error.to_dict(),
                }
                if failed_error is not None else None
            ),
            variables=public_variables,
            ast_nodes=self.ast_nodes,
            loop_iterations=self.loop_iterations,
            semantic_operations=self.semantic_operations,
            elapsed_seconds=time.monotonic() - self.started,
        )

    def _statement(self, node: ast.stmt) -> None:
        self.check_time()
        if isinstance(node, ast.Expr):
            self.last = self._expression(node.value)
            return
        if isinstance(node, ast.Assign):
            value = self._expression(node.value)
            for target in node.targets:
                self._assign(target, value)
            self.last = value
            return
        if isinstance(node, ast.If):
            branch = node.body if bool(self._expression(node.test)) else node.orelse
            for statement in branch:
                self._statement(statement)
            return
        if isinstance(node, ast.For):
            iterable = self._finite_iterable(self._expression(node.iter))
            for value in iterable:
                self.loop_iterations += 1
                if self.loop_iterations > self.limits.max_loop_iterations:
                    raise ProtocolError(
                        ErrorCode.BUDGET_EXHAUSTED,
                        "computer.run exceeded 1000 total loop iterations",
                    )
                self.check_time()
                self._assign(node.target, value)
                try:
                    for statement in node.body:
                        self._statement(statement)
                except _Continue:
                    continue
                except _Break:
                    break
            return
        if isinstance(node, ast.Break):
            raise _Break()
        if isinstance(node, ast.Continue):
            raise _Continue()
        raise ProtocolError(
            ErrorCode.POLICY_VIOLATION,
            f"run statement is not allowed: {type(node).__name__}",
        )

    def _expression(self, node: ast.expr) -> Any:
        self.check_time()
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name):
            if node.id == "computer":
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION,
                    "computer may only be used for query/act/verify calls",
                )
            if node.id in SAFE_BUILTINS:
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION,
                    "safe builtins cannot be used as first-class values",
                )
            if node.id not in self.variables:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"unknown run name: {node.id}")
            value = self.variables[node.id]
        elif isinstance(node, ast.List):
            value = [self._expression(item) for item in node.elts]
        elif isinstance(node, ast.Tuple):
            value = tuple(self._expression(item) for item in node.elts)
        elif isinstance(node, ast.Dict):
            value = {
                self._expression(key): self._expression(item)
                for key, item in zip(node.keys, node.values)
            }
        elif isinstance(node, ast.Subscript):
            container = self._expression(node.value)
            index = self._slice(node.slice)
            if not isinstance(container, (list, tuple, dict, str)):
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, "indexing is limited to primitive collections"
                )
            try:
                value = container[index]
            except (IndexError, KeyError, TypeError) as exc:
                raise ProtocolError(ErrorCode.NOT_FOUND, "run index/key was not found") from exc
        elif isinstance(node, ast.BinOp):
            value = self._binary(node)
        elif isinstance(node, ast.UnaryOp):
            operand = self._expression(node.operand)
            if isinstance(node.op, ast.Not):
                value = not bool(operand)
            elif isinstance(node.op, ast.UAdd):
                try:
                    value = operator.pos(operand)
                except TypeError as exc:
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST, "invalid unary operation"
                    ) from exc
            elif isinstance(node.op, ast.USub):
                try:
                    value = operator.neg(operand)
                except TypeError as exc:
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST, "invalid unary operation"
                    ) from exc
            else:  # pragma: no cover - validation already rejects this
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "unary operator is forbidden")
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                value = True
                for entry in node.values:
                    value = self._expression(entry)
                    if not value:
                        break
            else:
                value = False
                for entry in node.values:
                    value = self._expression(entry)
                    if value:
                        break
        elif isinstance(node, ast.Compare):
            value = self._comparison(node)
        elif isinstance(node, ast.Call):
            value = self._call(node)
        elif isinstance(node, ast.Attribute):
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION,
                "attributes may only be called through the explicit allowlist",
            )
        else:  # pragma: no cover - validation already rejects this
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION,
                f"run expression is not allowed: {type(node).__name__}",
            )
        self.owner._validate_value(value)
        return value

    def _binary(self, node: ast.BinOp) -> Any:
        left = self._expression(node.left)
        right = self._expression(node.right)
        functions = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
        }
        if isinstance(node.op, ast.Pow):
            if not isinstance(right, int) or isinstance(right, bool):
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, "power exponent must be an integer"
                )
            if abs(right) > self.limits.max_power_exponent:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED, "power exponent exceeds the bounded limit"
                )
            try:
                return operator.pow(left, right)
            except (ArithmeticError, TypeError) as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid power operation") from exc
        function = functions.get(type(node.op))
        if function is None:
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "binary operator is forbidden")
        try:
            return function(left, right)
        except (ArithmeticError, TypeError) as exc:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid arithmetic operation") from exc

    def _comparison(self, node: ast.Compare) -> bool:
        left = self._expression(node.left)
        functions = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            ast.In: lambda a, b: a in b,
            ast.NotIn: lambda a, b: a not in b,
        }
        for operation, comparator in zip(node.ops, node.comparators):
            right = self._expression(comparator)
            function = functions.get(type(operation))
            if function is None:
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "comparison is forbidden")
            try:
                passed = bool(function(left, right))
            except TypeError as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid comparison") from exc
            if not passed:
                return False
            left = right
        return True

    def _call(self, node: ast.Call) -> Any:
        args = [self._expression(argument) for argument in node.args]
        kwargs: dict[str, Any] = {}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg.startswith("_"):
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, "expanded/private call arguments are forbidden"
                )
            kwargs[keyword.arg] = self._expression(keyword.value)

        if isinstance(node.func, ast.Name):
            if node.func.id not in SAFE_BUILTINS:
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION, f"call is not allowlisted: {node.func.id}"
                )
            try:
                return self._builtin(node.func.id, args, kwargs)
            except ProtocolError:
                raise
            except (ArithmeticError, TypeError, ValueError, OverflowError) as exc:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"invalid {node.func.id} call",
                ) from exc

        if not isinstance(node.func, ast.Attribute):
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "dynamic calls are forbidden")
        name = node.func.attr
        if name.startswith("_"):
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "private methods are forbidden")
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "computer":
            if name == "run" or name not in COMPUTER_METHODS:
                raise ProtocolError(
                    ErrorCode.POLICY_VIOLATION,
                    "computer.run exposes only query, act, and verify",
                )
            return self._computer_call(name, args, kwargs)
        receiver = self._expression(node.func.value)
        return self._method(receiver, name, args, kwargs)

    def _computer_call(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        self.semantic_operations += 1
        if self.semantic_operations > self.limits.max_semantic_operations:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED,
                "computer.run exceeded 50 semantic operations",
            )
        if self.episode_state is not None:
            self.episode_state.consume_operation()
        function = getattr(self.computer, name, None)
        if not callable(function):
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"computer operation is unavailable: {name}",
                missing_capability=name,
            )
        self.check_time()
        try:
            value = function(*args, **kwargs)
        except ProtocolError:
            raise
        except TimeoutError as exc:
            raise ProtocolError(
                ErrorCode.TIMEOUT,
                f"computer.{name} timed out",
                retryable=True,
            ) from exc
        except Exception as exc:
            # Adapter internals are intentionally not reflected into the model.
            raise ProtocolError(
                ErrorCode.INTERNAL_ERROR, f"computer.{name} failed"
            ) from exc
        self.check_time()
        self.owner._validate_value(value)
        if name == "act" and isinstance(value, Mapping):
            if (
                value.get("status") == "applied"
                or value.get("changed") is True
                or value.get("side_effect_state") == "applied"
            ):
                self.applied_operations += 1
        return value

    def _builtin(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if name == "emit":
            if len(args) != 1 or kwargs:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "emit expects one value")
            self.emitted.append(args[0])
            self._check_emitted()
            return args[0]
        if name == "len":
            self._arity(name, args, kwargs, 1)
            return len(args[0])
        if name == "range":
            if kwargs or not 1 <= len(args) <= 3 or not all(
                isinstance(value, int) and not isinstance(value, bool) for value in args
            ):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid range arguments")
            try:
                value = list(range(*args))
            except ValueError as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid range arguments") from exc
            return self._bounded_collection(value)
        if name == "enumerate":
            if kwargs or not 1 <= len(args) <= 2:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid enumerate arguments")
            start = args[1] if len(args) == 2 else 0
            if not isinstance(start, int) or isinstance(start, bool):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "enumerate start must be integer")
            return self._bounded_collection(list(enumerate(self._finite_iterable(args[0]), start)))
        if name == "zip":
            if kwargs:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "zip keywords are forbidden")
            return self._bounded_collection(
                list(zip(*(self._finite_iterable(value) for value in args)))
            )
        if name == "sorted":
            if len(args) != 1 or set(kwargs) - {"reverse"}:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid sorted arguments")
            reverse = kwargs.get("reverse", False)
            if not isinstance(reverse, bool):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "sorted reverse must be bool")
            try:
                return self._bounded_collection(
                    sorted(self._finite_iterable(args[0]), reverse=reverse)
                )
            except TypeError as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "values are not sortable") from exc
        if name in {"min", "max"}:
            if kwargs or not args:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid {name} arguments")
            values = self._finite_iterable(args[0]) if len(args) == 1 else args
            if not values:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{name} of empty collection")
            try:
                return (min if name == "min" else max)(values)
            except TypeError as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "values are not comparable") from exc
        if name == "sum":
            if kwargs or not 1 <= len(args) <= 2:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid sum arguments")
            try:
                return sum(self._finite_iterable(args[0]), args[1] if len(args) == 2 else 0)
            except TypeError as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid sum values") from exc
        if name == "round":
            if kwargs or not 1 <= len(args) <= 2:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid round arguments")
            return round(*args)
        if name == "abs":
            self._arity(name, args, kwargs, 1)
            return abs(args[0])
        if name in {"str", "int", "float", "bool", "list", "dict", "set"}:
            return self._conversion(name, args, kwargs)
        raise ProtocolError(ErrorCode.POLICY_VIOLATION, f"builtin is forbidden: {name}")

    def _conversion(self, name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        functions = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "set": set,
        }
        if name in {"str", "int", "float", "bool", "list", "set"}:
            if kwargs or len(args) > 1:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid {name} arguments")
        elif name == "dict":
            if len(args) > 1 or any(key.startswith("_") for key in kwargs):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid dict arguments")
        try:
            value = functions[name](*args, **kwargs)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid {name} conversion") from exc
        return value

    def _method(
        self, receiver: Any, name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        if isinstance(receiver, str):
            if name not in STRING_METHODS:
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "string method is forbidden")
            try:
                return getattr(receiver, name)(*args, **kwargs)
            except (TypeError, ValueError) as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid string method call") from exc
        if isinstance(receiver, dict):
            if name not in DICT_METHODS or kwargs:
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "dict method is forbidden")
            if name == "get":
                if not 1 <= len(args) <= 2:
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid dict.get call")
                return receiver.get(*args)
            if args:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"dict.{name} takes no arguments")
            return list(getattr(receiver, name)())
        if isinstance(receiver, list):
            if name not in LIST_METHODS or kwargs:
                raise ProtocolError(ErrorCode.POLICY_VIOLATION, "list method is forbidden")
            if name == "append":
                self._arity(name, args, kwargs, 1)
                if len(receiver) >= self.limits.max_collection_items:
                    raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items")
                receiver.append(args[0])
                return None
            if name == "extend":
                self._arity(name, args, kwargs, 1)
                values = self._finite_iterable(args[0])
                if len(receiver) + len(values) > self.limits.max_collection_items:
                    raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items")
                receiver.extend(values)
                return None
            if name == "count":
                self._arity(name, args, kwargs, 1)
                return receiver.count(args[0])
            if name == "index":
                if not 1 <= len(args) <= 3:
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid list.index call")
                try:
                    return receiver.index(*args)
                except ValueError as exc:
                    raise ProtocolError(ErrorCode.NOT_FOUND, "list value was not found") from exc
        raise ProtocolError(
            ErrorCode.POLICY_VIOLATION,
            f"method access is forbidden for {type(receiver).__name__}",
        )

    def _assign(self, target: ast.expr, value: Any) -> None:
        self.owner._validate_value(value)
        if isinstance(target, ast.Name):
            self.owner._validate_name(target.id, assignment=True)
            self.variables[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            values = self._finite_iterable(value)
            if len(values) != len(target.elts):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "unpack assignment length mismatch")
            for child, item in zip(target.elts, values):
                self._assign(child, item)
            return
        if isinstance(target, ast.Subscript):
            container = self._expression(target.value)
            index = self._slice(target.slice)
            try:
                if isinstance(container, list) and isinstance(index, int):
                    container[index] = value
                    return
                if isinstance(container, dict) and not isinstance(index, slice):
                    if index not in container and len(container) >= self.limits.max_collection_items:
                        raise ProtocolError(
                            ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items"
                        )
                    container[index] = value
                    return
            except (IndexError, TypeError) as exc:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid indexed assignment") from exc
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION,
                "indexed assignment is limited to lists and dicts",
            )
        raise ProtocolError(ErrorCode.POLICY_VIOLATION, "assignment target is forbidden")

    def _slice(self, node: ast.expr | ast.slice) -> Any:
        if isinstance(node, ast.Slice):
            lower = self._expression(node.lower) if node.lower is not None else None
            upper = self._expression(node.upper) if node.upper is not None else None
            step = self._expression(node.step) if node.step is not None else None
            for value in (lower, upper, step):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                    raise ProtocolError(ErrorCode.INVALID_REQUEST, "slice bounds must be integers")
            if step == 0:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "slice step cannot be zero")
            bound = self.limits.max_collection_items
            if any(value is not None and abs(value) > bound for value in (lower, upper, step)):
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "slice bound exceeds 5000")
            return slice(lower, upper, step)
        value = self._expression(node)
        if isinstance(value, int) and abs(value) > self.limits.max_collection_items:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "index exceeds bounded range")
        return value

    def _finite_iterable(self, value: Any) -> list[Any]:
        if isinstance(value, dict):
            values = list(value.keys())
        elif isinstance(value, set):
            values = sorted(value, key=repr)
        elif isinstance(value, (list, tuple, str)):
            values = list(value)
        else:
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION, "for-loop input must be a finite primitive collection"
            )
        return self._bounded_collection(values)

    def _bounded_collection(self, values: list[Any]) -> list[Any]:
        if len(values) > self.limits.max_collection_items:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "collection exceeds 5000 items")
        self.owner._validate_value(values)
        return values

    def _check_emitted(self) -> None:
        try:
            encoded = json.dumps(
                [_json_value(value) for value in self.emitted],
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolError(ErrorCode.POLICY_VIOLATION, "emitted value is invalid") from exc
        if len(encoded) > self.limits.max_emitted_chars:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "emitted run output exceeds 12000 characters"
            )

    @staticmethod
    def _arity(
        name: str, args: Sequence[Any], kwargs: Mapping[str, Any], expected: int
    ) -> None:
        if len(args) != expected or kwargs:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"{name} expects {expected} positional arguments"
            )
