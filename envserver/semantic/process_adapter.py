"""Restricted argv-only process execution for semantic episodes.

Production execution is fail-closed unless Bubblewrap is available.  The
worker gets no shell, network namespace, desktop/session bus, harness secrets,
or filesystem outside explicit artifact roots and read-only system runtimes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from .adapters import AdapterActionResult, AdapterContext, AdapterObservation, SemanticAdapter
from .protocol import ErrorCode, ProtocolError, SideEffectState, Status, utc_now


MAX_ARGV = 128
MAX_ARG_CHARS = 16_384
MAX_STDIN_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_TIMEOUT_SECONDS = 300
_SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONNOUSERSITE": "1",
}
_FORBIDDEN_ENV_NAMES = {
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "AT_SPI_BUS_ADDRESS", "SSH_AUTH_SOCK", "BROWSER", "CHROME_REMOTE_DEBUGGING_PORT",
}


@dataclass(frozen=True)
class ProcessExecution:
    argv: tuple[str, ...]
    cwd: str
    stdin: bytes
    timeout_seconds: float


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool


class SandboxBackend(Protocol):
    name: str

    def execute(self, request: ProcessExecution) -> ProcessOutcome:
        ...


def _bounded_output(data: bytes) -> tuple[str, str, bool, int]:
    digest = hashlib.sha256(data).hexdigest()
    if len(data) <= MAX_OUTPUT_BYTES:
        retained = data
        truncated = False
    else:
        half = MAX_OUTPUT_BYTES // 2
        retained = data[:half] + b"\n...[truncated]...\n" + data[-half:]
        truncated = True
    return retained.decode("utf-8", errors="replace"), digest, truncated, len(data)


class BubblewrapBackend:
    name = "bubblewrap"

    def __init__(self, writable_roots: Mapping[str, Path], *, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("bwrap") or ""
        self.writable_roots = {
            guest: path.resolve() for guest, path in writable_roots.items()
        }

    @property
    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).is_file())

    def _command(self, request: ProcessExecution) -> list[str]:
        if not self.available:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                "Bubblewrap is required for process isolation",
                retryable=False,
            )
        command = [
            self.executable,
            "--die-with-parent", "--new-session", "--unshare-all",
            "--clearenv", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                command += ["--ro-bind", system_path, system_path]
        # A tiny conventional set of configuration files is enough for locale,
        # MIME, and certificate-aware local transformations.  There is no DNS
        # or network namespace connectivity.
        for config_path in ("/etc/ssl", "/etc/alternatives", "/etc/mime.types"):
            if Path(config_path).exists():
                command += ["--ro-bind", config_path, config_path]
        created_directories: set[str] = set()
        for guest, root in self.writable_roots.items():
            parents = list(PurePosixPath(guest).parents)
            for parent in reversed(parents[:-1]):
                value = str(parent)
                if value != "/" and value not in created_directories:
                    command += ["--dir", value]
                    created_directories.add(value)
            command += ["--bind", str(root), guest]
        for key, value in _SAFE_ENV.items():
            command += ["--setenv", key, value]
        command += ["--chdir", request.cwd, "--", *request.argv]
        return command

    def execute(self, request: ProcessExecution) -> ProcessOutcome:
        command = self._command(request)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            start_new_session=True,
            close_fds=True,
        )
        try:
            stdout, stderr = process.communicate(
                input=request.stdin,
                timeout=request.timeout_seconds,
            )
            return ProcessOutcome(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return ProcessOutcome(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )


class ProcessAdapter(SemanticAdapter):
    adapter_id = "process.sandbox@1"
    application = "isolated argv worker"
    supported_versions = ("bubblewrap-argv-v1",)
    accepts_entity_target = True
    resources = frozenset({"process.sessions", "process.runs"})
    capabilities = frozenset({"process.exec"})
    resource_schemas = {
        "process.sessions": {"type": "object", "properties": {}, "additionalProperties": False},
        "process.runs": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    action_schemas = {
        "process.exec": {
            "arguments_schema": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array", "minItems": 1, "maxItems": MAX_ARGV,
                        "items": {"type": "string", "maxLength": MAX_ARG_CHARS},
                    },
                    "cwd": {"type": "string", "pattern": "^/"},
                    "stdin": {"type": "string", "maxLength": MAX_STDIN_BYTES},
                    "timeout_seconds": {"type": "number", "minimum": 0.001, "maximum": MAX_TIMEOUT_SECONDS},
                },
                "required": ["argv", "cwd"], "additionalProperties": False,
            },
            "risk": "persistent",
            "idempotent": False,
            "execution_paths": ["native_api"],
        }
    }

    def __init__(
        self,
        writable_roots: Sequence[str | Path] | Mapping[str, str | Path],
        *,
        backend: SandboxBackend | None = None,
        max_history: int = 100,
    ) -> None:
        if not writable_roots:
            raise ValueError("at least one writable root is required")
        if isinstance(writable_roots, Mapping):
            mounts = {str(PurePosixPath(guest)): Path(path).resolve() for guest, path in writable_roots.items()}
        else:
            mounts = {str(PurePosixPath(str(path))): Path(path).resolve() for path in writable_roots}
        for guest in mounts:
            pure = PurePosixPath(guest)
            if not pure.is_absolute() or ".." in pure.parts:
                raise ValueError("process guest roots must be normalized absolute paths")
            protected = tuple(PurePosixPath(value) for value in (
                "/", "/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/dev", "/sys", "/run"
            ))
            if pure == protected[0] or any(
                pure == value or value in pure.parents for value in protected[1:]
            ):
                raise ValueError("process guest roots may not overlap protected system paths")
        guest_paths = [PurePosixPath(value) for value in mounts]
        for index, first in enumerate(guest_paths):
            if any(first in second.parents or second in first.parents for second in guest_paths[index + 1 :]):
                raise ValueError("process guest roots may not overlap")
        self.writable_roots = mounts
        for root in self.writable_roots.values():
            root.mkdir(parents=True, exist_ok=True)
        self.backend = backend or BubblewrapBackend(self.writable_roots)
        self.max_history = max(1, min(int(max_history), 1_000))
        self._manager_ref = "native_process_manager"
        self._history: list[dict[str, Any]] = []
        self._lock = RLock()

    def probe(self) -> Mapping[str, Any]:
        available = getattr(self.backend, "available", True)
        return {
            "ok": bool(available),
            "adapter_id": self.adapter_id,
            "sandbox": self.backend.name,
            "network": "isolated",
            "gui_session": "unavailable",
        }

    def resolve_ref(self, ref: str) -> Mapping[str, Any]:
        if ref == self._manager_ref:
            return {"ref": ref, "kind": "process.manager"}
        with self._lock:
            for record in self._history:
                if record.get("ref") == ref:
                    return dict(record)
        raise ProtocolError(ErrorCode.STALE_REF, "process ref no longer resolves")

    def _safe_cwd(self, value: Any) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "process cwd is invalid")
        guest_path = PurePosixPath(value)
        for guest_root, host_root in self.writable_roots.items():
            root = PurePosixPath(guest_root)
            if guest_path == root or root in guest_path.parents:
                relative = guest_path.relative_to(root)
                host_path = (host_root / Path(*relative.parts)).resolve(strict=False)
                if host_path != host_root and host_root not in host_path.parents:
                    raise ProtocolError(ErrorCode.PERMISSION_DENIED, "process cwd escapes artifact root")
                if not host_path.is_dir():
                    raise ProtocolError(ErrorCode.NOT_FOUND, "process cwd does not exist")
                return str(guest_path)
        raise ProtocolError(ErrorCode.PERMISSION_DENIED, "process cwd is outside artifact roots")

    @staticmethod
    def _argv(value: Any) -> tuple[str, ...]:
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= MAX_ARGV
            or not all(isinstance(item, str) and item and "\x00" not in item and len(item) <= MAX_ARG_CHARS for item in value)
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "argv must be a bounded non-empty string array")
        # Shell metacharacters are valid literal arguments.  They never become
        # syntax because shell=False and no shell executable is inserted.
        return tuple(value)

    def observe(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterObservation:
        with self._lock:
            if context.resource == "process.sessions":
                records = ({
                    "ref": self._manager_ref,
                    "kind": "process.manager",
                    "sandbox": self.backend.name,
                    "network": "isolated",
                    "gui_session": "unavailable",
                    "writable_roots": sorted(self.writable_roots),
                    "advertised_actions": ["process.exec"],
                },)
            elif context.resource == "process.runs":
                records = tuple(dict(record) for record in self._history)
            else:
                raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, context.resource)
            revision = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
            return AdapterObservation(
                items=records,
                provenance=({"source": self.adapter_id, "freshness": "live"},),
                summary={"record_count": len(records)},
                native_revision=f"process_{revision[:20]}",
            )

    def act(self, context: AdapterContext, payload: Mapping[str, Any]) -> AdapterActionResult:
        target = payload.get("target") or {}
        if target.get("ref") != self._manager_ref:
            raise ProtocolError(ErrorCode.STALE_REF, "process manager ref is stale")
        if payload.get("action") != "process.exec":
            raise ProtocolError(ErrorCode.UNSUPPORTED, "unsupported process action")
        arguments = payload.get("arguments") or {}
        if set(arguments) - {"argv", "cwd", "stdin", "timeout_seconds"}:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "process.exec contains unknown arguments")
        argv = self._argv(arguments.get("argv"))
        cwd = self._safe_cwd(arguments.get("cwd"))
        stdin_value = arguments.get("stdin", "")
        if not isinstance(stdin_value, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "process stdin must be text")
        stdin = stdin_value.encode("utf-8")
        if len(stdin) > MAX_STDIN_BYTES:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "process stdin is too large")
        timeout = arguments.get("timeout_seconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "process timeout is invalid")
        request = ProcessExecution(argv, cwd, stdin, float(timeout))
        try:
            outcome = self.backend.execute(request)
        except ProtocolError:
            raise
        except Exception as error:
            # Process creation may fail before user code starts; there is no
            # desktop or network side effect route, but artifact effects could
            # still be unknown after a transport break.
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                f"sandbox worker failed: {type(error).__name__}",
                side_effect_state=SideEffectState.UNKNOWN,
            ) from error
        stdout, stdout_hash, stdout_truncated, stdout_bytes = _bounded_output(outcome.stdout)
        stderr, stderr_hash, stderr_truncated, stderr_bytes = _bounded_output(outcome.stderr)
        record = {
            "ref": f"native_run_{hashlib.sha256(f'{utc_now()}:{argv}'.encode()).hexdigest()[:24]}",
            "kind": "process.run",
            "argv": list(argv),
            "cwd": cwd,
            "exit_code": outcome.exit_code,
            "duration_seconds": round(outcome.duration_seconds, 6),
            "timed_out": outcome.timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "sandbox": self.backend.name,
            "advertised_actions": [],
        }
        with self._lock:
            self._history.append(record)
            self._history = self._history[-self.max_history :]
        if outcome.timed_out:
            raise ProtocolError(
                ErrorCode.TIMEOUT,
                "sandboxed process exceeded its timeout and its process group was killed",
                retryable=False,
                side_effect_state=SideEffectState.UNKNOWN,
                candidates=({
                    "stdout_hash": stdout_hash,
                    "stderr_hash": stderr_hash,
                    "duration_seconds": record["duration_seconds"],
                },),
            )
        return AdapterActionResult(
            changed=True,
            result={"execution_path": "native_api", **{key: value for key, value in record.items() if key != "ref"}},
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            status=Status.OK,
        )

    def close(self) -> None:
        with self._lock:
            self._history.clear()
