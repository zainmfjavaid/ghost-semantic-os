"""HTTP shim around OSWorld's DesktopEnv.

Runs on the OSWorld host (inside the OSWorld venv) and exposes the gym loop
over HTTP so a TypeScript agent harness can drive it:

    POST /episodes            {task_path}          -> {episode_id, instruction, screenshot}
    GET  /episodes/{id}/obs                        -> {screenshot, ...}
    POST /episodes/{id}/step  {command|commands}   -> {screenshot, done}
    POST /episodes/{id}/evaluate                   -> {score}
    DELETE /episodes/{id}                          -> closes the VM

`command` is a pyautogui snippet, exactly OSWorld's native action space — no
translation layer, so the agent is scored on the benchmark's own interface.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import shlex
import signal
import threading
import time
import uuid
from typing import Any, Callable

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import guest_semantic
from semantic.runtime import SemanticRuntime
from semantic.simple_facade import DEFAULT_LIMIT as SIMPLE_READ_DEFAULT_LIMIT
from semantic.simple_facade import SimpleComputerFacade
from semantic.protocol import ProtocolError

# OSWorld's AWS manager installs SIGINT/SIGTERM handlers for instance cleanup.
# Python only permits that on the main thread, and FastAPI runs sync endpoints
# in a worker thread, so the call raises ValueError. Those handlers only exist
# to terminate a half-launched VM on Ctrl-C; this server owns VM teardown via
# DELETE /episodes/{id}, so making the call a no-op off-thread is safe.
_real_signal = signal.signal


def _signal_main_thread_only(signalnum, handler):  # type: ignore[no-untyped-def]
    if threading.current_thread() is threading.main_thread():
        return _real_signal(signalnum, handler)
    return None


signal.signal = _signal_main_thread_only  # type: ignore[assignment]

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("envserver")

app = FastAPI(title="OSWorld env server")

# DesktopEnv is not thread-safe; serialize all access per episode.
_episodes: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

REGION = os.environ.get("AWS_REGION", "us-east-1")

# Browser code execution is for compact inspection, computation, extraction and
# invoking the site's real controls. Rewriting the page can manufacture a DOM
# that satisfies an evaluator without producing a real user-visible state. The
# prompt forbids that, but a validity boundary cannot rely on model compliance.
_DOM_SURGERY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\.(?:innerHTML|outerHTML|className|hidden|disabled)\s*=",
        r"\.style(?:\.[A-Za-z_$][\w$]*|\[['\"][^'\"]+['\"]\])\s*=",
        r"\.(?:remove|removeChild|replaceWith|replaceChildren)\s*\(",
        r"\.classList\.(?:add|remove|replace|toggle)\s*\(",
        r"\.setAttribute\s*\(\s*['\"](?:style|class|hidden|disabled|aria-hidden)['\"]",
    )
)

_NON_BROWSER_JS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brequire\s*\(",
        r"\bprocess\.(?:env|mainModule|binding)\b",
        r"\b(?:child_process|node:fs|node:path|node:os)\b",
    )
)

# Code execution is a first-class computer capability for filesystem, archive,
# repository, conversion and other CLI work. It must not become a second,
# invisible GUI action space: browser and desktop interaction stays observable
# through the semantic tools above. These are capability-level boundaries, not
# task or benchmark vocabulary.
_GUEST_UI_AUTOMATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:xdotool|ydotool|wmctrl)\b",
        r"\b(?:pyautogui|pyatspi|pynput)\b",
        r"\b(?:playwright|selenium|puppeteer)\b",
        r"(?:127\.0\.0\.1|localhost):9222\b",
        r"\b(?:devtools|remote-debugging-port)\b",
    )
)

MAX_GUEST_SCRIPT_CHARS = int(os.environ.get("MAX_GUEST_SCRIPT_CHARS", "12000"))
MAX_GUEST_EXEC_SECONDS = int(os.environ.get("MAX_GUEST_EXEC_SECONDS", "300"))
MAX_GUEST_OUTPUT_CHARS = int(os.environ.get("MAX_GUEST_OUTPUT_CHARS", "12000"))
_GUEST_EXEC_MARKER = "__GHOST_COMPUTER_EXEC_RESULT__"

BLIND_DESKTOP_ACTION_LIMIT = int(os.environ.get("BLIND_DESKTOP_ACTION_LIMIT", "12"))
MAX_CONSECUTIVE_READONLY_JS = int(
    os.environ.get("MAX_CONSECUTIVE_READONLY_JS", "6")
)

_LIBREOFFICE_EXTENSIONS = frozenset({
    ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
})
_UNO_ACCEPT = "--accept=socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext"

SEMANTIC_SETUP_READINESS_TIMEOUT_SECONDS = float(
    os.environ.get("SEMANTIC_SETUP_READINESS_TIMEOUT_SECONDS", "12")
)
SEMANTIC_SETUP_READINESS_POLL_SECONDS = float(
    os.environ.get("SEMANTIC_SETUP_READINESS_POLL_SECONDS", "0.5")
)
SEMANTIC_SETUP_READINESS_STABLE_POLLS = max(
    2, int(os.environ.get("SEMANTIC_SETUP_READINESS_STABLE_POLLS", "2"))
)
_NON_GUI_LAUNCH_EXECUTABLES = frozenset({
    "bash", "dash", "env", "false", "nohup", "python", "python3", "sh",
    "sleep", "socat", "true",
})
_GUI_OPEN_EXTENSION_FAMILIES = {
    **{extension: "libreoffice" for extension in _LIBREOFFICE_EXTENSIONS},
    ".pdf": "document-viewer",
    ".html": "browser",
    ".htm": "browser",
    ".jpg": "image-viewer",
    ".jpeg": "image-viewer",
    ".png": "image-viewer",
    ".gif": "image-viewer",
    ".webp": "image-viewer",
    ".svg": "image-viewer",
    ".mp3": "media-player",
    ".mp4": "media-player",
    ".wav": "media-player",
    ".webm": "media-player",
}


def _launch_executable(command: Any) -> str | None:
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str):
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
    else:
        return None
    if not parts:
        return None
    index = 0
    while index < len(parts):
        executable = os.path.basename(parts[index]).casefold()
        if executable == "env":
            index += 1
            while index < len(parts) and (
                parts[index].startswith("-") or "=" in parts[index]
            ):
                index += 1
            continue
        if executable in {"nohup", "setsid", "dbus-launch"}:
            index += 1
            while index < len(parts) and parts[index].startswith("-"):
                index += 1
            continue
        return executable
    return None


def _gui_setup_families(task: dict[str, Any]) -> frozenset[str]:
    """Infer expected GUI app families only from generic setup mechanics."""

    families: set[str] = set()
    for operation in task.get("config", []):
        if not isinstance(operation, dict):
            continue
        operation_type = str(operation.get("type") or "")
        parameters = operation.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        if operation_type == "chrome_open_tabs":
            families.add("browser")
        elif operation_type == "open":
            suffix = os.path.splitext(str(parameters.get("path") or ""))[1].casefold()
            families.add(_GUI_OPEN_EXTENSION_FAMILIES.get(suffix, f"open:{suffix or 'unknown'}"))
        elif operation_type == "launch":
            executable = _launch_executable(parameters.get("command"))
            if executable and executable not in _NON_GUI_LAUNCH_EXECUTABLES:
                if executable in {"libreoffice", "soffice"}:
                    executable = "libreoffice"
                elif executable in {"code", "code-insiders", "codium"}:
                    executable = "vscode"
                elif executable in {"chrome", "chromium", "chromium-browser", "google-chrome"}:
                    executable = "browser"
                families.add(executable)
    return frozenset(families)


def _expected_gui_surface_count(task: dict[str, Any]) -> int:
    """Count setup-launched windows without collapsing separate documents.

    The family set is useful for capability coverage, but two LibreOffice
    launch operations create two independently actionable document windows.
    Browser launches are the exception: Chrome normally reuses one window, so
    repeated browser setup operations count once.
    """

    launches: list[str] = []
    for operation in task.get("config", []):
        if not isinstance(operation, dict):
            continue
        operation_type = str(operation.get("type") or "")
        parameters = operation.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        family: str | None = None
        if operation_type == "chrome_open_tabs":
            family = "browser"
        elif operation_type == "open":
            suffix = os.path.splitext(str(parameters.get("path") or ""))[1].casefold()
            family = _GUI_OPEN_EXTENSION_FAMILIES.get(suffix)
        elif operation_type == "launch":
            executable = _launch_executable(parameters.get("command"))
            if executable and executable not in _NON_GUI_LAUNCH_EXECUTABLES:
                if executable in {"chrome", "chromium", "chromium-browser", "google-chrome"}:
                    family = "browser"
                else:
                    family = executable
        if family:
            launches.append(family)
    browser_count = 1 if "browser" in launches else 0
    return browser_count + sum(family != "browser" for family in launches)


_READINESS_SURFACE_ROLES = frozenset({"alert", "dialog", "frame", "window"})
_SHELL_SURFACE_NAMES = frozenset({"desktop", "gnome shell", "gnome-shell"})


def _real_surface_state(records: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(records, list):
        return (), ()
    signatures: list[str] = []
    active_signatures: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        role = str(record.get("role") or "").casefold()
        name = str(record.get("name") or "").strip()
        state = record.get("state") or record.get("states") or {}
        if role not in _READINESS_SURFACE_ROLES:
            continue
        if name.casefold() in _SHELL_SURFACE_NAMES:
            continue
        if isinstance(state, dict) and (
            state.get("visible") is False or state.get("showing") is False
        ):
            continue
        identity = record.get("ref") or f"{role}:{name}:{index}"
        signatures.append(str(identity))
        if isinstance(state, dict) and (
            state.get("active") is True or state.get("focused") is True
        ):
            active_signatures.append(str(identity))
    return tuple(sorted(signatures)), tuple(sorted(active_signatures))


def _real_surface_signature(records: Any) -> tuple[str, ...]:
    """Compatibility helper for callers that only need visible identities."""

    return _real_surface_state(records)[0]


def _wait_for_semantic_setup_readiness(
    env: Any,
    semantic_guest: dict[str, Any],
    setup_task: dict[str, Any],
    *,
    timeout_seconds: float = SEMANTIC_SETUP_READINESS_TIMEOUT_SECONDS,
    poll_seconds: float = SEMANTIC_SETUP_READINESS_POLL_SECONDS,
    stable_polls: int = SEMANTIC_SETUP_READINESS_STABLE_POLLS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for setup-launched GUI state without images or model actions.

    This is deliberately fail-open: readiness telemetry explains a timeout or
    guest error, while episode creation remains available for model recovery.
    """

    expected = max(
        len(_gui_setup_families(setup_task)),
        _expected_gui_surface_count(setup_task),
    )
    metadata: dict[str, Any] = {
        "status": "not_required" if expected == 0 else "waiting",
        "expected_gui_surfaces": expected,
        "observed_gui_surfaces": 0,
        "observed_active_gui_surfaces": 0,
        "required_stable_polls": max(2, stable_polls),
        "stable_polls": 0,
        "polls": 0,
        "waited_ms": 0,
        "fail_open": True,
    }
    if expected == 0:
        return metadata

    started = clock()
    deadline = started + max(0.0, timeout_seconds)
    prior_signature: tuple[str, ...] | None = None
    successful_polls = 0
    errors = 0
    while True:
        metadata["polls"] += 1
        try:
            response = guest_semantic.request(
                env,
                semantic_guest,
                "POST",
                "/v1/query",
                {
                    "resource": "ui.surfaces",
                    "parameters": {},
                    "where": {},
                    "fields": [],
                    "order_by": [],
                    "cursor": None,
                    "limit": 100,
                    "internal_offset": 0,
                },
            )
            if not response.get("ok"):
                raise RuntimeError("semantic guest readiness query failed")
            result = response.get("result") or {}
            signature, active_signature = _real_surface_state(result.get("records"))
            successful_polls += 1
            metadata["observed_gui_surfaces"] = len(signature)
            metadata["observed_active_gui_surfaces"] = len(active_signature)
            if len(signature) >= expected and active_signature:
                metadata["stable_polls"] = (
                    int(metadata["stable_polls"]) + 1
                    if signature == prior_signature
                    else 1
                )
                prior_signature = signature
                if metadata["stable_polls"] >= metadata["required_stable_polls"]:
                    metadata["status"] = "ready"
                    break
            else:
                metadata["stable_polls"] = 0
                prior_signature = None
        except Exception:
            errors += 1
            metadata["stable_polls"] = 0
            prior_signature = None

        now = clock()
        if now >= deadline:
            metadata["status"] = "timeout" if successful_polls else "unavailable"
            break
        sleep(min(max(0.0, poll_seconds), max(0.0, deadline - now)))

    metadata["waited_ms"] = max(0, int(round((clock() - started) * 1_000)))
    if errors:
        metadata["query_errors"] = errors
    return metadata


_ELECTRON_ACCESSIBILITY = "--force-renderer-accessibility"
_VSCODE_EXECUTABLES = frozenset({
    "code", "code-insiders", "code-oss", "codium", "vscodium",
})


def _semantic_task_setup(task: dict[str, Any]) -> dict[str, Any]:
    """Add generic semantic app launch integration before task setup.

    This transformation sees only setup operation types and file extensions.
    It does not inspect task instructions, evaluators, or expected values.
    """

    prepared = copy.deepcopy(task)
    for operation in prepared.get("config", []):
        if not isinstance(operation, dict):
            continue
        parameters = operation.get("parameters")
        if not isinstance(parameters, dict):
            continue
        if operation.get("type") == "open":
            path = parameters.get("path")
            suffix = os.path.splitext(str(path or ""))[1].casefold()
            if suffix in _LIBREOFFICE_EXTENSIONS:
                operation["type"] = "launch"
                operation["parameters"] = {
                    "command": [
                        "libreoffice", _UNO_ACCEPT, "--norestore", "--nodefault",
                        "--nolockcheck", str(path),
                    ],
                }
        elif operation.get("type") == "launch":
            command = parameters.get("command")
            if (
                isinstance(command, list)
                and command
                and os.path.basename(str(command[0])) in {"libreoffice", "soffice"}
                and _UNO_ACCEPT not in command
            ):
                command.insert(1, _UNO_ACCEPT)
            if (
                isinstance(command, list)
                and command
                and isinstance(command[0], str)
                and os.path.basename(command[0]).casefold() in _VSCODE_EXECUTABLES
                and _ELECTRON_ACCESSIBILITY not in command
            ):
                # Electron consumes process flags before the original VS Code
                # arguments. Keep argv-only setup exact and never reinterpret
                # a shell string or wrapper command.
                command.insert(1, _ELECTRON_ACCESSIBILITY)
    return prepared


def _dom_surgery_reason(code: str) -> str | None:
    for pattern in _DOM_SURGERY_PATTERNS:
        if pattern.search(code):
            return pattern.pattern
    return None


def _non_browser_js_reason(code: str) -> str | None:
    for pattern in _NON_BROWSER_JS_PATTERNS:
        if pattern.search(code):
            return pattern.pattern
    return None


def _guest_ui_automation_reason(script: str) -> str | None:
    for pattern in _GUEST_UI_AUTOMATION_PATTERNS:
        if pattern.search(script):
            return pattern.pattern
    return None


def _bounded_execution_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    # Preserve both the beginning (command context) and the end (usually the
    # useful error/summary) without letting an accidental verbose command flood
    # the model's remaining context.
    head = max(1, limit * 2 // 3)
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... {omitted} characters omitted ...\n{text[-tail:]}"


def _guest_exec_program(
    script: str,
    timeout_seconds: int,
    working_dir: str | None,
    language: str = "bash",
) -> str:
    """Build a Python wrapper for OSWorld's long-standing /execute endpoint.

    Some published desktop images predate /run_bash_script even when the host
    controller package contains that method. /execute is the stable primitive
    already used for every pyautogui action, so launch a bounded Bash child
    through it and emit one machine-readable envelope. A new process group
    ensures timeouts cannot leave grandchildren running in the guest.
    """
    argv = (
        ["/bin/bash", "--noprofile", "--norc", "-c", f"set -o pipefail\n{script}"]
        if language == "bash"
        else ["/usr/bin/env", "python3", "-c", script]
    )
    return "\n".join((
        "import json as _ghost_json",
        "import os as _ghost_os",
        "import signal as _ghost_signal",
        "import subprocess as _ghost_subprocess",
        "_ghost_payload = None",
        "_ghost_process = None",
        "try:",
        "    _ghost_process = _ghost_subprocess.Popen(",
        f"        {argv!r},",
        f"        cwd={working_dir!r}, stdout=_ghost_subprocess.PIPE,",
        "        stderr=_ghost_subprocess.PIPE, text=True, start_new_session=True,",
        "    )",
        "    try:",
        f"        _ghost_stdout, _ghost_stderr = _ghost_process.communicate(timeout={timeout_seconds})",
        "        _ghost_payload = {",
        "            'status': 'success' if _ghost_process.returncode == 0 else 'error',",
        "            'output': _ghost_stdout, 'error': _ghost_stderr,",
        "            'returncode': _ghost_process.returncode,",
        "        }",
        "    except _ghost_subprocess.TimeoutExpired:",
        "        _ghost_os.killpg(_ghost_process.pid, _ghost_signal.SIGKILL)",
        "        _ghost_stdout, _ghost_stderr = _ghost_process.communicate()",
        "        _ghost_payload = {",
        "            'status': 'error', 'output': _ghost_stdout,",
        f"            'error': _ghost_stderr + '\\nTimed out after {timeout_seconds} seconds',",
        "            'returncode': -1,",
        "        }",
        "except Exception as _ghost_error:",
        "    _ghost_payload = {",
        "        'status': 'error', 'output': '', 'error': str(_ghost_error),",
        "        'returncode': -1,",
        "    }",
        f"print({_GUEST_EXEC_MARKER!r} + _ghost_json.dumps(_ghost_payload))",
    ))


def _decode_guest_exec_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("guest /execute returned no structured response")
    output = str(result.get("output") or "")
    # The child command can legitimately print the wrapper process list. That
    # text contains our marker inside a long command line, and searching for the
    # final substring then lands inside the JSON payload's escaped stdout. The
    # trusted wrapper envelope is instead the final physical line that STARTS
    # with the marker; the wrapper prints it only after the child exits.
    envelope = next((
        line[len(_GUEST_EXEC_MARKER):]
        for line in reversed(output.splitlines())
        if line.startswith(_GUEST_EXEC_MARKER)
    ), None)
    if envelope is None:
        outer_error = str(result.get("error") or result.get("message") or "")
        raise RuntimeError(
            "guest /execute did not return the computer-exec envelope"
            + (f": {outer_error}" if outer_error else "")
        )
    decoded = json.loads(envelope.strip())
    if not isinstance(decoded, dict):
        raise RuntimeError("guest computer-exec envelope was not an object")
    return decoded


def _is_blind_desktop_command(command: str) -> bool:
    """True for keyboard/text fallbacks that do not name a semantic target."""
    return (
        "pyautogui.press(" in command
        or "pyautogui.hotkey(" in command
        or "pyautogui.typewrite(" in command
    )


class CreateEpisode(BaseModel):
    task_path: str
    screen_size: tuple[int, int] = (1920, 1080)
    # Browser tasks can be driven through Chrome DevTools Protocol instead of
    # pixel coordinates. See web_provider.py for why and for the honesty caveat.
    web: bool = False
    # Set-of-Marks: overlay numbered boxes on interactive elements and expose a
    # numbered element list, so the agent can act by element index instead of
    # grounding raw pixel coordinates. Uses OSWorld's own published helpers.
    som: bool = False
    # Semantic-desktop mode compiles the same exhaustive typed graph but keeps
    # screenshots plain and does not expose numbered visual marks. This is the
    # non-browser counterpart to the hybrid Chrome runtime.
    semantic_only: bool = False
    # Explicit runtime identity. Legacy clients may omit it; semantic runtime
    # clients must disable capture at construction.
    runtime: str = "hybrid-v15"
    require_screenshot: bool = True
    max_tool_calls: int = 60
    # The harness never consumes the reset screenshot; it asks for an explicit
    # screenshot as its first desktop action when needed.
    initial_observation: bool = True


class StepRequest(BaseModel):
    command: str | None = None
    commands: list[str] | None = None
    pause: float = 1.0


class ComputerExecRequest(BaseModel):
    script: str
    language: str = "bash"
    timeout_seconds: int = 30
    working_dir: str | None = None


class SimpleReadRequest(BaseModel):
    query: str | None = None
    within: str | None = None
    cursor: str | None = None
    limit: int = SIMPLE_READ_DEFAULT_LIMIT


class SimpleClickRequest(BaseModel):
    element: str


class SimpleTypeRequest(BaseModel):
    element: str
    text: str


JPEG_QUALITY = int(os.environ.get("SCREENSHOT_JPEG_QUALITY", "70"))
SEMANTIC_RUNTIMES = frozenset({
    "semantic-v1", "semantic-plus-v1", "semantic-simple-v1",
})
# A single web page can expose hundreds of accessibility nodes. Sending them all
# floods the context and buries the useful ones, so the list is capped. The
# screenshot still shows every mark, and the agent can scroll or re-observe.
MAX_ELEMENTS = int(os.environ.get("MAX_SOM_ELEMENTS", "90"))


def _compress(png_bytes: bytes) -> tuple[bytes, str] | None:
    """Re-encode a screenshot as JPEG at full resolution, or return None.

    Two things matter here.

    Size: a 1920x1080 PNG is ~1.6MB and the harness keeps every screenshot in
    the conversation, so raw frames pile up fast. JPEG cuts each one ~8x.
    Resolution is deliberately preserved so click coordinates still map 1:1
    onto the real screen.

    Validity: roughly 1 capture in 400 comes back empty or truncated. Passing
    those bytes through poisons the conversation permanently — the model API
    rejects the whole request with 'Could not process image', and because the
    bad frame stays in history, every later turn fails too. The episode dies in
    a way that looks exactly like the agent giving up. So an unusable frame is
    dropped here and the caller sends text instead of a broken image.
    """
    if not png_bytes:
        logger.warning("screenshot was empty; dropping frame")
        return None
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes))
        image.verify()  # catches truncated/corrupt data before we trust it
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        encoded = buffer.getvalue()
        if not encoded:
            logger.warning("screenshot re-encoded to zero bytes; dropping frame")
            return None
        return encoded, "image/jpeg"
    except Exception:
        logger.exception("screenshot unusable; dropping frame rather than sending it")
        return None


def _encode(
    obs: dict[str, Any] | None,
    entry: dict[str, Any] | None = None,
    *,
    compare_change: bool = True,
) -> dict[str, Any]:
    if obs is None:
        return {}
    payload: dict[str, Any] = {}
    shot = obs.get("screenshot")
    # Did anything meaningful change? Measured trace data: ~half of all agent
    # actions sit in streaks of 3+ identical tool calls, runs up to 21. The agent
    # cannot perceive that an action did nothing, so it repeats.
    #
    # Exact hashing is NOT usable here: a real desktop always has something
    # ticking (the GNOME clock, a caret, a cursor), so pixel-identical frames
    # essentially never occur and the check would silently never fire. Compare
    # a downscaled grayscale version with a tolerance instead, so a clock digit
    # changing does not count as "the screen changed" but a real UI change does.
    if compare_change and entry is not None and shot:
        signature = _frame_signature(shot)
        previous = entry.get("last_signature")
        if signature is not None and previous is not None:
            if _frames_equivalent(previous, signature):
                entry["unchanged_streak"] = entry.get("unchanged_streak", 0) + 1
                payload["screen_unchanged"] = True
                payload["unchanged_streak"] = entry["unchanged_streak"]
            else:
                entry["unchanged_streak"] = 0
        if signature is not None:
            entry["last_signature"] = signature
    if entry is not None and entry.get("som"):
        semantic = _extract_semantic_elements(obs.get("accessibility_tree"))
        semantic_records = semantic[2] if semantic is not None else []
        entry["semantic_elements"] = semantic_records
        entry["semantic_snapshot"] = entry.get("semantic_snapshot", 0) + 1
        snapshot = entry["semantic_snapshot"]
        # Plain semantic mode intentionally hides visual mark numbers and raw
        # coordinates, but unlabeled sibling controls still need a unique legal
        # identity. References are opaque, snapshot-scoped capabilities backed
        # by the private typed record. They never expose bounds or tree paths.
        semantic_refs: dict[str, dict[str, Any]] = {}
        for ordinal, record in enumerate(semantic_records, start=1):
            ref = f"ax-{snapshot:x}-{ordinal:x}"
            record["ref"] = ref
            semantic_refs[ref] = record
        entry["semantic_refs"] = semantic_refs
        semantic_surface = bool(entry.get("web") or entry.get("semantic_only"))
        if semantic_surface:
            marked = (
                (shot, semantic[0], semantic[1])
                if semantic is not None else None
            )
            entry["marks_are_semantic"] = semantic is not None
        else:
            # The legacy numbered overlay and the exhaustive semantic graph use
            # different filters and therefore different index spaces. Preserve
            # the overlay for visual/numbered fallbacks, but keep the typed
            # records independently; named actions execute the selected record
            # directly and never assume it shares a mark index.
            marked = _apply_som(shot, obs.get("accessibility_tree"))
            entry["marks_are_semantic"] = False
        if marked is not None:
            tagged_shot, elements, marks = marked
            entry["marks"] = marks
            entry["som_elements"] = [
                line for line in elements.splitlines()
                if re.match(r"^\s*\d+\s", line)
            ]
            if semantic_surface:
                # Hybrid browser runs use the a11y tree as a semantic lookup
                # backend, not as another visual-grounding problem. The same is
                # true for semantic-only desktop runs. Numbering a 150-control
                # frame made Qwen transcribe 122/125 as 1225. Preserve the plain
                # screenshot and keep all indices private to the server.
                payload["desktop_accessibility_ready"] = True
                # A screenshot visually exposes the active application and
                # document title, but a text-forward model otherwise receives
                # only a boolean saying an accessibility snapshot exists. Give
                # it the same compact surface identity without flooding the
                # result with every control. This prevents provenance errors
                # such as acting on a guessed file instead of the open source
                # artifact; controls still require desktop_find.
                surface_records = []
                seen_surfaces: set[tuple[str, str]] = set()
                for record in semantic_records:
                    role = _normalize_semantic(record.get("role")).replace("_", "-")
                    name = str(record.get("name") or record.get("text") or "").strip()
                    key = (role, name)
                    if role not in _SEMANTIC_OWNER_ROLES or not name or key in seen_surfaces:
                        continue
                    seen_surfaces.add(key)
                    surface_records.append({
                        "role": record.get("role") or "",
                        "name": name,
                        "states": record.get("states") or [],
                        "context": record.get("context") or [],
                    })
                    if len(surface_records) >= 12:
                        break
                if surface_records:
                    payload["desktop_surfaces"] = surface_records
            else:
                shot = tagged_shot
                lines = elements.split("\n")
                if len(lines) > MAX_ELEMENTS:
                    payload["elements"] = "\n".join(lines[:MAX_ELEMENTS]) + (
                        f"\n... ({len(lines) - MAX_ELEMENTS} more elements not listed; "
                        "use desktop_find to search the complete accessibility list; "
                        "all remain numbered on the screenshot and clickable by index)"
                    )
                else:
                    payload["elements"] = elements
            payload["element_count"] = len(marks)
        else:
            entry["marks"] = []
            entry["som_elements"] = []
            payload["elements_unavailable"] = True
    if shot is not None:
        compressed = _compress(shot)
        if compressed is None:
            payload["screenshot_unavailable"] = True
        else:
            data, media_type = compressed
            payload["screenshot"] = base64.b64encode(data).decode("ascii")
            payload["media_type"] = media_type
            payload["screenshot_bytes"] = len(data)
    tree = obs.get("accessibility_tree")
    if tree:
        payload["accessibility_tree_chars"] = len(tree)
    return payload


# Frame comparison tuning.
#
# The naive approach (global tolerance on a small thumbnail) fails badly at real
# resolution: a ticked checkbox is under one cell at 64x64, so it reads as "no
# change" — which would tell the agent its action did nothing when it worked.
# That is the harmful direction, so instead:
#   * mask the desktop top bar, which is the one reliably-dynamic region (clock),
#   * compare at higher resolution so small controls survive downscaling,
#   * keep the tolerance tight, because a false "changed" is harmless (we simply
#     stay quiet) while a false "unchanged" actively misleads.
FRAME_GRID = int(os.environ.get("FRAME_GRID", "128"))
FRAME_CELL_THRESHOLD = int(os.environ.get("FRAME_CELL_THRESHOLD", "10"))
FRAME_DIFF_TOLERANCE = float(os.environ.get("FRAME_DIFF_TOLERANCE", "0.0002"))
TOP_BAR_FRACTION = float(os.environ.get("TOP_BAR_FRACTION", "0.04"))


def _frame_signature(image_bytes: bytes) -> list[tuple[int, int, int]] | None:
    """Downscaled RGB fingerprint used for tolerant frame comparison.

    RGB, not grayscale: a pale dialog over a pale background can differ by ~5 in
    luminance while differing by ~45 in the blue channel. Grayscale silently
    misses those, which would make the detector tell the agent "nothing changed"
    when a dialog had in fact just opened — the most harmful way this can fail.
    """
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Drop the top bar (system clock lives there and ticks constantly).
        cut = int(img.height * TOP_BAR_FRACTION)
        img = img.crop((0, cut, img.width, img.height)).resize((FRAME_GRID, FRAME_GRID))
        return list(img.getdata())
    except Exception:
        return None


def _frames_equivalent(a, b) -> bool:
    if len(a) != len(b):
        return False
    # A cell counts as different if ANY channel moves beyond compression noise.
    t = FRAME_CELL_THRESHOLD
    differing = sum(
        1 for p, q in zip(a, b)
        if abs(p[0] - q[0]) > t or abs(p[1] - q[1]) > t or abs(p[2] - q[2]) > t
    )
    return (differing / len(a)) <= FRAME_DIFF_TOLERANCE


def _apply_som(png_bytes: bytes | None, a11y_tree: str | None):
    """Overlay numbered marks on interactive elements (OSWorld's own helper).

    Returns (tagged_png, element_list_text, marks) or None when the tree is
    missing/unparseable. `marks` are [x, y, w, h] in real screen coordinates,
    kept server-side so the agent can act by index and never handle pixels.
    """
    if not png_bytes or not a11y_tree:
        return None
    try:
        import sys
        import xml.etree.ElementTree as ET

        if "/home/ubuntu/OSWorld" not in sys.path:
            sys.path.insert(0, "/home/ubuntu/OSWorld")
        # Import the SoM primitives directly. Going through mm_agents.agent drags in
        # the reference agent's LLM-client dependencies (backoff, openai, ...) which
        # this server has no need for and does not install.
        from mm_agents.accessibility_tree_wrap.heuristic_retrieve import (
            draw_bounding_boxes, filter_nodes,
        )

        nodes = filter_nodes(ET.fromstring(a11y_tree), platform="ubuntu", check_image=True)
        if not nodes:
            return None
        marks, _drew, element_list, tagged_png = draw_bounding_boxes(nodes, png_bytes)
        if not marks:
            return None
        return tagged_png, element_list, marks
    except Exception:
        logger.exception("set-of-marks tagging failed; falling back to plain screenshot")
        return None


def _node_attribute(node, local_name: str) -> str | None:
    for key, value in node.attrib.items():
        if key == local_name or key.endswith("}" + local_name):
            return value
    return None


def _integer_pair(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _clean_a11y_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().replace("\t", " ")


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].replace("_", "-")


def _semantic_node_states(node) -> list[str]:
    states = []
    for key, value in node.attrib.items():
        if "/ns/state}" not in key or value.casefold() != "true":
            continue
        states.append(_xml_local_name(key))
    return sorted(set(states))


def _semantic_node_actions(node) -> list[str]:
    actions = []
    for key in node.attrib:
        if "/ns/action}" not in key:
            continue
        local = _xml_local_name(key)
        if local.endswith("-desc"):
            actions.append(local[:-5])
    return sorted(set(actions))


_INTERACTIVE_ROLE_PARTS = (
    "button", "item", "link", "entry", "textbox", "searchbox",
    "combo-box", "check-box", "radio", "tab", "menu", "slider",
    "scroll-bar", "spin", "tree", "option", "icon",
)

_SEMANTIC_OWNER_ROLES = {
    "application", "frame", "dialog", "window", "alert",
}


def _semantic_record_line(record: dict[str, Any]) -> str:
    return "\t".join((
        str(record["index"]),
        str(record["role"]),
        str(record["name"]),
        str(record["text"]),
        str(record["value"]),
        ",".join(record["states"]),
        ",".join(record["actions"]),
        " > ".join(record["context"]),
    ))


def _extract_semantic_elements(
    a11y_tree: str | None,
) -> tuple[str, list[list[int]], list[dict[str, Any]]] | None:
    """Compile the live accessibility hierarchy into private typed targets.

    OSWorld's drawing helper intentionally omits a node when its crop is one
    flat color. That is useful for reducing visual overlays, but wrong for a
    semantic executor. This path also deliberately avoids OSWorld's role
    allowlist: platform roles, state, hierarchy and advertised actions are data,
    not task policy. Private path/bounds records stay server-side; the model
    receives only the normalized semantic fields returned by desktop_find.
    """
    if not a11y_tree:
        return None
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(a11y_tree)
        marks: list[list[int]] = []
        records: list[dict[str, Any]] = []
        lines = ["index\trole\tname\ttext\tvalue\tstate\tactions\tcontext"]

        def visit(node, path: list[int], ancestors: list[str]) -> None:
            role = _clean_a11y_text(_xml_local_name(node.tag))
            name = _clean_a11y_text(node.get("name"))
            text = _clean_a11y_text(node.text)
            value = _clean_a11y_text(_node_attribute(node, "value"))
            states = _semantic_node_states(node)
            actions = _semantic_node_actions(node)
            coords = _integer_pair(_node_attribute(node, "screencoord"))
            size = _integer_pair(_node_attribute(node, "size"))
            visible = "visible" in states and "showing" in states
            bounded = (
                coords is not None and size is not None
                and coords[0] >= 0 and coords[1] >= 0
                and size[0] > 0 and size[1] > 0
            )
            has_semantics = bool(name or text or value or actions)
            if visible and bounded and has_semantics:
                role_key = role.casefold().replace("_", "-")
                actionable = bool(
                    actions
                    or set(states) & {
                        "editable", "focusable", "checkable", "expandable",
                        "selectable", "multi-selectable",
                    }
                    or any(part in role_key for part in _INTERACTIVE_ROLE_PARTS)
                )
                bounds = [coords[0], coords[1], size[0], size[1]]
                owner_context = [
                    descriptor for descriptor in ancestors
                    if descriptor.partition(":")[0].casefold() in _SEMANTIC_OWNER_ROLES
                ]
                # Keep the owning app/window even for deeply nested controls. The
                # last-four-only representation dropped that information exactly
                # where it was needed to distinguish identical controls in two
                # windows. Preserve owners plus nearby structural context, without
                # exposing the private tree path used by the executor.
                public_context = list(dict.fromkeys(owner_context + ancestors[-4:]))
                record: dict[str, Any] = {
                    "index": len(records) + 1,
                    "role": role,
                    "name": name,
                    "text": text,
                    "value": value,
                    "states": states,
                    "actions": actions,
                    "bounds": bounds,
                    "path": list(path),
                    "context": public_context,
                    "actionable": actionable,
                }
                records.append(record)
                marks.append(bounds)
                lines.append(_semantic_record_line(record))

            descriptor = f"{role}:{name or text or value}" if (name or text or value) else role
            next_ancestors = ancestors + ([descriptor[:160]] if descriptor else [])
            for child_index, child in enumerate(node):
                visit(child, path + [child_index], next_ancestors)

        for root_child_index, root_child in enumerate(root):
            visit(root_child, [root_child_index], [])
        if not marks:
            return None
        return "\n".join(lines), marks, records
    except Exception:
        logger.exception("semantic accessibility extraction failed")
        return None


def _pinned(entry: dict[str, Any], fn, *args):
    """Run fn on the episode's own thread.

    Playwright's sync API is bound to the thread that created it, and FastAPI
    serves each request from an arbitrary threadpool thread. With concurrency
    above 1 that raises "greenlet.error: Cannot switch to a different thread"
    and the episode 500s. Pinning every call for an episode to one dedicated
    thread fixes it. This is not local-only: the CDP web path on the real VM
    runs Playwright the same way, so the queue's concurrency-5 web arms would
    have hit exactly this.
    """
    return entry["pool"].submit(fn, *args).result()


@app.post("/episodes")
def create_episode(request: CreateEpisode) -> dict[str, Any]:
    strict_semantic = request.runtime in SEMANTIC_RUNTIMES
    if not 1 <= request.max_tool_calls <= 100:
        raise HTTPException(status_code=400, detail="max_tool_calls must be between 1 and 100")
    if request.runtime not in {
        "vision-v15", "hybrid-v15", "semantic-v1", "semantic-plus-v1",
        "semantic-simple-v1",
        "semantic-visual-v1",
    }:
        raise HTTPException(status_code=400, detail="unknown runtime")
    if request.runtime == "semantic-visual-v1":
        raise HTTPException(
            status_code=409,
            detail={"code": "policy_violation", "message": "visual sidecar is not enabled"},
        )
    if strict_semantic and request.require_screenshot:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "semantic runtimes require require_screenshot=false",
            },
        )
    if strict_semantic and request.initial_observation:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "semantic runtimes cannot return a legacy initial observation",
            },
        )
    with open(request.task_path) as handle:
        task = json.load(handle)
    episode_id = uuid.uuid4().hex[:12]
    logger.info("creating episode %s for %s", episode_id, request.task_path)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ep-{episode_id}")

    def _build():
        env = None
        try:
            env = DesktopEnv(
                provider_name="aws",
                region=REGION,
                action_space="pyautogui",
                headless=True,
                screen_size=tuple(request.screen_size),
                require_a11y_tree=request.som,
                require_screenshot=request.require_screenshot,
            )
            semantic_guest = (
                guest_semantic.bootstrap(env, episode_id) if strict_semantic else None
            )
            setup_task = _semantic_task_setup(task) if strict_semantic else task
            observation = env.reset(task_config=setup_task)
            setup_readiness = (
                _wait_for_semantic_setup_readiness(env, semantic_guest, setup_task)
                if strict_semantic and semantic_guest is not None
                else None
            )
            return env, observation, semantic_guest, setup_readiness
        except Exception:
            # A reset can fail after the provider has already allocated a real
            # nested VM. Before this guard the request returned 500 without ever
            # registering an episode, leaving no ID that later cleanup could
            # target and silently consuming fleet capacity.
            if env is not None:
                try:
                    env.close()
                except Exception:
                    logger.warning(
                        "failed to close partially built episode %s",
                        episode_id,
                        exc_info=True,
                    )
            raise

    try:
        env, obs, semantic_guest, setup_readiness = pool.submit(_build).result()
    except Exception:
        pool.shutdown(wait=False)
        raise
    with _lock:
        _episodes[episode_id] = {
            "env": env, "task": task, "steps": 0,
            "runtime": request.runtime,
            "strict_semantic": strict_semantic,
            "semantic_guest": semantic_guest,
            "setup_readiness": setup_readiness,
            "som": request.som, "semantic_only": request.semantic_only,
            "marks": [],
            "web": request.web, "web_provider": None, "web_elements": [],
            "pool": pool,
        }
    entry = _episodes[episode_id]
    if strict_semantic:
        try:
            capability_response = _pinned(
                entry,
                guest_semantic.request,
                env,
                semantic_guest,
                "GET",
                "/v1/capabilities",
                None,
            )
            if not capability_response.get("ok"):
                raise RuntimeError("semantic guest capability probe failed")
            capability_result = capability_response.get("result") or {}
            guest_capabilities = capability_result.get("records") or []
            if not isinstance(guest_capabilities, list) or not guest_capabilities:
                raise RuntimeError("semantic guest advertised no capabilities")

            def _guest_request(method, path, payload=None):
                return _pinned(
                    entry,
                    guest_semantic.request,
                    env,
                    semantic_guest,
                    method,
                    path,
                    dict(payload) if payload is not None else None,
                )

            semantic_adapters = []
            from semantic.research_adapter import PublicResearchAdapter
            from semantic.remaining_apps import create_remaining_application_adapters

            semantic_adapters.append(PublicResearchAdapter())
            semantic_adapters.extend(
                create_remaining_application_adapters(_guest_request)
            )
            vm_ip = getattr(env, "vm_ip", None)
            if vm_ip:
                from semantic.browser_adapter import AsyncBrowserAdapter

                initial_active_url = None
                for setup in task.get("config", []):
                    if setup.get("type") == "chrome_open_tabs":
                        urls = setup.get("parameters", {}).get("urls_to_open") or []
                        if urls:
                            initial_active_url = urls[-1]
                browser_adapter = AsyncBrowserAdapter(
                    vm_ip,
                    port=int(getattr(env, "chromium_port", 9222) or 9222),
                    initial_active_url=initial_active_url,
                )
                semantic_adapters.append(browser_adapter)
                from semantic.chrome_adapter import ChromeSemanticAdapter
                semantic_adapters.append(ChromeSemanticAdapter(
                    browser_adapter, _guest_request
                ))

            entry["semantic_runtime"] = SemanticRuntime(
                episode_id=episode_id,
                runtime_name=request.runtime,
                max_tool_calls=request.max_tool_calls,
                guest_request=_guest_request,
                guest_capabilities=guest_capabilities,
                adapters=semantic_adapters,
                representation_gaps=({
                    "capability": "visual-composition-judgment",
                    "status": "representation_gap",
                    "available_in": "semantic-visual-v1",
                },),
            )
            if request.runtime == "semantic-simple-v1":
                entry["simple_facade"] = SimpleComputerFacade(
                    entry["semantic_runtime"]
                )
        except Exception:
            with _lock:
                _episodes.pop(episode_id, None)
            try:
                _pinned(entry, guest_semantic.shutdown, env, semantic_guest)
            finally:
                env.close()
                pool.shutdown(wait=False)
            raise
    browser_clock = None
    if request.web and not strict_semantic:
        try:
            clock_envelope = json.loads(_web(entry).run_js(
                """(() => {
                  const now = new Date();
                  return {
                    text: now.toString(),
                    iso: now.toISOString(),
                    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    offsetMinutes: now.getTimezoneOffset()
                  };
                })()"""
            ))
            if clock_envelope.get("ok"):
                browser_clock = clock_envelope.get("result")
        except Exception:
            logger.warning("could not read browser clock for %s", episode_id, exc_info=True)
            # The first clock probe races Chrome startup on some task resets.
            # Never retain a provider whose initial connection failed: a later
            # model action must get a fresh pool/Playwright lifecycle.
            failed_provider = entry.get("web_provider")
            entry["web_provider"] = None
            if failed_provider is not None:
                failed_provider.retire()
    return {
        "episode_id": episode_id,
        "instruction": task["instruction"],
        "task_id": task.get("id"),
        "domain": request.task_path.rstrip("/").split("/")[-2],
        "runtime": request.runtime,
        "semantic_protocol_version": "1.0" if strict_semantic else None,
        "screenshots_captured": int(getattr(env, "screenshot_capture_count", 0)),
        **({
            "semantic_guest_bundle_hash": semantic_guest["bundle_hash"],
            "semantic_guest_version": semantic_guest.get("agent_version"),
            "setup_readiness": setup_readiness,
            "environment_identity": {
                "outer_provider": os.environ.get("OSWORLD_OUTER_PROVIDER", "unknown"),
                "outer_vm_name": os.environ.get("OSWORLD_OUTER_VM_NAME", "unknown"),
                "nested_guest_machine_id": semantic_guest.get("guest_machine_id"),
                "guest_os_release_hash": semantic_guest.get("guest_os_release_hash"),
                "guest_platform": semantic_guest.get("guest_platform"),
                "guest_image_digest": os.environ.get("OSWORLD_GUEST_IMAGE_DIGEST", "unknown"),
                "display_identity": semantic_guest.get("display_identity"),
                "semantic_guest_bundle_hash": semantic_guest["bundle_hash"],
            },
        } if semantic_guest else {}),
        **({"browser_clock": browser_clock} if browser_clock else {}),
        **(_encode(obs, entry) if request.initial_observation else {}),
    }


def _get(episode_id: str) -> dict[str, Any]:
    with _lock:
        entry = _episodes.get(episode_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown episode {episode_id}")
    return entry


def _reject_simple_runtime_route(entry: dict[str, Any], route: str) -> None:
    """Fail closed when a simple episode reaches a non-simple action surface."""

    if entry.get("runtime") != "semantic-simple-v1":
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "policy_violation",
            "message": (
                f"{route} is disabled for semantic-simple-v1; use only simple "
                "computer tools, semantic state, evaluation, or episode cleanup"
            ),
        },
    )


@app.get("/episodes/{episode_id}/obs")
def get_obs(episode_id: str) -> dict[str, Any]:
    entry = _get(episode_id)
    if entry.get("strict_semantic"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "legacy observations are disabled for semantic runtimes",
            },
        )
    env: DesktopEnv = entry["env"]
    # An explicit observation is the required recovery from a long sequence of
    # unnamed keyboard actions. Clear only the blind-strategy telemetry: the
    # screenshot/accessibility refresh does not claim that machine state
    # changed, but it does give the agent a current state from which to plan.
    entry["blind_desktop_streak"] = 0
    return {
        "steps": entry["steps"],
        **_encode(_pinned(entry, env._get_obs), entry, compare_change=False),
    }


@app.post("/episodes/{episode_id}/semantic")
def semantic_operation(episode_id: str, request: dict[str, Any]) -> dict[str, Any]:
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/semantic")
    runtime = entry.get("semantic_runtime")
    if not entry.get("strict_semantic") or runtime is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "semantic protocol is available only in semantic runtimes",
            },
        )
    return runtime.dispatch(request)


@app.post("/episodes/{episode_id}/semantic/complete")
def semantic_complete(episode_id: str, request: dict[str, Any]) -> dict[str, Any]:
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/semantic/complete")
    runtime = entry.get("semantic_runtime")
    if not entry.get("strict_semantic") or runtime is None:
        raise HTTPException(status_code=409, detail={"code": "policy_violation"})
    return runtime.complete(request).to_dict()


@app.get("/episodes/{episode_id}/semantic/state")
def semantic_state(episode_id: str) -> dict[str, Any]:
    entry = _get(episode_id)
    runtime = entry.get("semantic_runtime")
    if not entry.get("strict_semantic") or runtime is None:
        raise HTTPException(status_code=409, detail={"code": "policy_violation"})
    screenshots = int(getattr(entry["env"], "screenshot_capture_count", 0))
    return runtime.state_summary(screenshots_captured=screenshots)


def _simple_facade(episode_id: str) -> SimpleComputerFacade:
    entry = _get(episode_id)
    if entry.get("runtime") != "semantic-simple-v1":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "simple computer tools require semantic-simple-v1",
            },
        )
    facade = entry.get("simple_facade")
    if not isinstance(facade, SimpleComputerFacade):
        raise HTTPException(
            status_code=503,
            detail={"code": "adapter_unavailable", "message": "simple facade unavailable"},
        )
    return facade


def _simple_result(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except ProtocolError as error:
        return {"ok": False, "error": error.to_dict()}
    except Exception as error:
        logger.exception("simple computer operation failed")
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": f"{type(error).__name__}: {error}",
                "retryable": False,
                "side_effect_state": "unknown",
            },
        }


@app.post("/episodes/{episode_id}/simple/read")
def simple_read(episode_id: str, request: SimpleReadRequest) -> dict[str, Any]:
    facade = _simple_facade(episode_id)
    return _simple_result(lambda: facade.read(
        query=request.query,
        within=request.within,
        cursor=request.cursor,
        limit=request.limit,
    ))


@app.post("/episodes/{episode_id}/simple/click")
def simple_click(episode_id: str, request: SimpleClickRequest) -> dict[str, Any]:
    facade = _simple_facade(episode_id)
    return _simple_result(lambda: facade.click(request.element))


@app.post("/episodes/{episode_id}/simple/type")
def simple_type(episode_id: str, request: SimpleTypeRequest) -> dict[str, Any]:
    facade = _simple_facade(episode_id)
    return _simple_result(lambda: facade.type_text(request.element, request.text))


@app.post("/episodes/{episode_id}/step")
def step(episode_id: str, request: StepRequest) -> dict[str, Any]:
    entry = _get(episode_id)
    if entry.get("strict_semantic"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "raw pyautogui steps are disabled for semantic runtimes",
            },
        )
    env: DesktopEnv = entry["env"]
    commands = request.commands or ([request.command] if request.command else [])
    if not commands:
        raise HTTPException(status_code=400, detail="command or commands required")
    # Track unnamed keyboard streaks. A keyboard-only application can
    # legitimately need a long sequence, so the checkpoint does not terminate
    # the task or permanently forbid keys. It withholds only the first action
    # after the bound and returns a fresh screenshot/accessibility snapshot;
    # the model can then continue from current state. This prevents a blind
    # plan from consuming the entire episode without a grounding checkpoint.
    blind_streak = 0
    blind_batch = bool(entry.get("web") and entry.get("som") and all(
        _is_blind_desktop_command(command) for command in commands
    ))
    if blind_batch and entry.get("blind_desktop_streak", 0) >= BLIND_DESKTOP_ACTION_LIMIT:
        previous_streak = int(entry.get("blind_desktop_streak", 0))
        obs = _pinned(entry, env._get_obs)
        entry["blind_desktop_streak"] = 0
        entry["recent_commands"] = []
        entry["repeat_count"] = 0
        return {
            "steps": entry["steps"],
            "done": False,
            "blind_action_streak": previous_streak,
            "errors": [
                f"Grounding checkpoint: {previous_streak} consecutive keyboard/text "
                "actions already ran without a named semantic target. This action "
                "was not executed. A fresh desktop snapshot is attached; inspect it "
                "and use a named target when available before continuing."
            ],
            **_encode(obs, entry, compare_change=False),
        }
    if blind_batch:
        blind_streak = entry.get("blind_desktop_streak", 0) + len(commands)
        entry["blind_desktop_streak"] = blind_streak
    else:
        entry["blind_desktop_streak"] = 0
    # Exact-repeat detection: same command issued back to back.
    history = entry.setdefault("recent_commands", [])
    repeat_count = 0
    if commands and history and history[-1] == commands[0]:
        repeat_count = entry.get("repeat_count", 0) + 1
    entry["repeat_count"] = repeat_count
    history.append(commands[-1])
    del history[:-5]
    obs: dict[str, Any] | None = None
    done = False
    errors: list[str] = []
    for command in commands:
        try:
            obs, _reward, done, _info = _pinned(entry, env.step, command, request.pause)
            entry["steps"] += 1
        except Exception as error:  # a bad pyautogui snippet must not kill the episode
            errors.append(f"{type(error).__name__}: {error}")
            break
    payload: dict[str, Any] = {"steps": entry["steps"], "done": done, **_encode(obs, entry)}
    if blind_streak > BLIND_DESKTOP_ACTION_LIMIT:
        payload["blind_action_streak"] = blind_streak
    if repeat_count:
        payload["repeated_action"] = repeat_count
    if errors:
        payload["errors"] = errors
    return payload


@app.post("/episodes/{episode_id}/exec")
def computer_exec(episode_id: str, request: ComputerExecRequest) -> dict[str, Any]:
    """Run bounded, traced CLI work inside the episode's Ubuntu guest.

    This is deliberately separate from DesktopEnv.step: it supports real
    computer-side code execution while refusing hidden browser/GUI automation.
    The visible UI remains controlled and observed through the semantic action
    spaces, and one exec request consumes one episode step.
    """
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/exec")
    script = request.script.strip()
    if not script:
        raise HTTPException(status_code=400, detail="script must not be empty")
    if len(script) > MAX_GUEST_SCRIPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"script exceeds {MAX_GUEST_SCRIPT_CHARS} characters",
        )
    language = request.language.strip().casefold()
    if language not in {"bash", "python"}:
        raise HTTPException(
            status_code=400,
            detail="language must be either 'bash' or 'python'",
        )
    if not 1 <= request.timeout_seconds <= MAX_GUEST_EXEC_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"timeout_seconds must be between 1 and {MAX_GUEST_EXEC_SECONDS}",
        )
    if request.working_dir and not request.working_dir.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="working_dir must be an absolute guest path",
        )
    forbidden = _guest_ui_automation_reason(script)
    if forbidden:
        raise HTTPException(
            status_code=400,
            detail=(
                "computer_exec cannot automate browser or desktop UI invisibly "
                f"(matched {forbidden}); use web_* or desktop_* tools"
            ),
        )

    env: DesktopEnv = entry["env"]
    runner = getattr(getattr(env, "controller", None), "execute_python_command", None)
    if not callable(runner):
        raise HTTPException(
            status_code=409,
            detail="this desktop environment does not expose guest code execution",
        )

    result: dict[str, Any] | None = None
    execution_error: str | None = None
    try:
        outer_result = _pinned(
            entry,
            runner,
            _guest_exec_program(
                script, request.timeout_seconds, request.working_dir or "/home/user",
                language,
            ),
        )
        result = _decode_guest_exec_result(outer_result)
    except Exception as exc:
        execution_error = f"{type(exc).__name__}: {exc}"
    finally:
        entry["steps"] += 1
        entry["blind_desktop_streak"] = 0

    if result is None and execution_error is None:
        execution_error = "guest execution returned no result"
    payload: dict[str, Any] = {"steps": entry["steps"], "done": False}
    if execution_error:
        payload["errors"] = [execution_error]
        return payload

    assert result is not None
    stdout_limit = MAX_GUEST_OUTPUT_CHARS * 2 // 3
    stderr_limit = MAX_GUEST_OUTPUT_CHARS - stdout_limit
    public_result = {
        "status": result.get("status"),
        "returncode": result.get("returncode"),
        "stdout": _bounded_execution_text(result.get("output"), stdout_limit),
        "stderr": _bounded_execution_text(result.get("error"), stderr_limit),
    }
    payload["result"] = (
        "Guest command finished. Inspect visible browser/desktop state separately "
        "if the command was expected to affect it. If this changed a file currently "
        "open in an application, reload or reopen it before the final UI save so a "
        "stale in-memory copy cannot overwrite the disk edit.\n"
        + json.dumps(public_result, ensure_ascii=False, separators=(",", ":"))
    )
    return payload


def _elements_signature(els: list[dict]) -> tuple:
    """Cheap fingerprint of the actionable page state."""
    return tuple((e.get("tag"), e.get("role"), e.get("type"), e.get("label"),
                  e.get("value"), e.get("checked"), e.get("selected"),
                  e.get("expanded"), e.get("pressed"), e.get("active_descendant"),
                  e.get("disabled"),
                  e.get("href")) for e in els)


def _web(entry: dict[str, Any]):
    """Lazily attach a CDP provider to this episode's VM."""
    if entry.get("web_provider") is None:
        from web_provider import WebProvider

        vm_ip = getattr(entry["env"], "vm_ip", None)
        if not vm_ip:
            raise HTTPException(status_code=409, detail="episode has no VM ip")
        # Use the port DesktopEnv itself talks to, rather than assuming one.
        port = int(getattr(entry["env"], "chromium_port", 9222) or 9222)
        initial_active_url = None
        for setup in entry.get("task", {}).get("config", []):
            if setup.get("type") != "chrome_open_tabs":
                continue
            urls = setup.get("parameters", {}).get("urls_to_open") or []
            if urls:
                # OSWorld opens configured tabs in order; the final URL is the
                # visible tab. Playwright's context.pages order is not a focus
                # signal and previously sent bookmark actions to the wrong tab.
                initial_active_url = urls[-1]
        entry["web_provider"] = WebProvider(
            vm_ip,
            port=port,
            initial_active_url=initial_active_url,
        )
    return entry["web_provider"]


def _launch_cdp_browser_for_navigation(entry: dict[str, Any], url: str) -> None:
    """Launch Chrome with the benchmark's mapped CDP port for web_navigate.

    Cross-application tasks often start in LibreOffice and leave Chrome closed.
    Clicking Ubuntu's ordinary dock icon launches Chrome without a debugging
    endpoint, making the advertised web tools permanently unusable. Navigation
    already means "open this URL in the browser", so starting a CDP-enabled
    browser is part of that general tool contract rather than an extra hidden
    task action. We do this only after a real CDP attach failed and only for the
    idempotent navigate operation; clicks and submissions are never retried.
    """
    env: DesktopEnv = entry["env"]
    controller = getattr(env, "setup_controller", None)
    launcher = getattr(controller, "_launch_setup", None)
    if not callable(launcher):
        raise RuntimeError("desktop environment cannot launch a CDP browser")

    # Docker maps the guest's canonical 9222 port to env.chromium_port on the
    # host. Passing the host-side mapped port inside the guest is a subtle but
    # fatal error when concurrent episodes receive 9223, 9224, ... .
    launcher(
        [
            "google-chrome",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--disable-session-crashed-bubble",
            url or "about:blank",
        ],
        # The subsequent WebProvider attach has its own bounded readiness wait.
        # SetupController's older wait path can block for 120 seconds when an
        # ordinary non-CDP Chrome process owns the profile, even though that
        # process can never gain a debugging port after launch.
        wait_for_cdp=False,
    )
    entry["web_provider"] = None
    entry.pop("web_elements", None)


def _guest_chrome_running(entry: dict[str, Any]) -> bool | None:
    """Return whether a Chrome executable already runs in the episode guest.

    A second `google-chrome --remote-debugging-port=...` invocation is handed to
    the existing process and cannot retrofit CDP. Detect that state before a
    doomed launch. `None` means the best-effort probe itself was unavailable;
    navigation may still try the normal launch path in that case.

    Inspect only argv[0] under /proc. Looking for "chrome" in complete command
    lines would match this probe's own Python wrapper and report a false hit.
    """
    env: DesktopEnv = entry["env"]
    runner = getattr(getattr(env, "controller", None), "execute_python_command", None)
    if not callable(runner):
        return None
    script = """python3 - <<'PY'
import glob, os
names = {'chrome', 'google-chrome', 'chromium', 'chromium-browser'}
found = False
for path in glob.glob('/proc/[0-9]*/cmdline'):
    try:
        first = open(path, 'rb').read().split(b'\\0', 1)[0].decode(errors='ignore')
    except (OSError, IndexError):
        continue
    base = os.path.basename(first)
    if base in names or first.endswith('/opt/google/chrome/chrome'):
        found = True
        break
print('yes' if found else 'no')
PY"""
    try:
        outer_result = _pinned(
            entry,
            runner,
            _guest_exec_program(script, 5, None),
        )
        result = _decode_guest_exec_result(outer_result)
    except Exception:
        return None
    if int(result.get("returncode", -1)) != 0:
        return None
    answer = str(result.get("output") or "").strip().splitlines()
    return bool(answer and answer[-1] == "yes")


class WebAction(BaseModel):
    action: str      # elements/click/type/navigate/text/search/read_pages/scroll/find/frames/js/actions
    # Compact mode: cap the listing and skip re-sending it after an action.
    # Context growth is the dominant cost, and the full list is re-sent on every
    # later turn, so echoing it after each click can cost more than the extra
    # call it saves. Tested as an arm rather than assumed.
    compact: bool = False
    index: int | None = None
    text: str | None = None
    url: str | None = None
    direction: str | None = None
    amount: int | None = None
    query: str | None = None
    code: str | None = None
    actions: list[dict[str, Any]] | None = None
    queries: list[str] | None = None
    urls: list[str] | None = None
    result_limit: int | None = None
    text_limit: int | None = None
    frame: int | None = None
    expect_change: bool = False
    # Text-only DOM arms have an explicit screenshot tool. Avoid capturing,
    # compressing and transferring a full VM frame that the client will drop.
    observe: bool = True


@app.post("/episodes/{episode_id}/web")
def web_action(episode_id: str, request: WebAction) -> dict[str, Any]:
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/web")
    if entry.get("runtime") == "semantic-plus-v1" and request.observe:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_violation",
                "message": "semantic-plus-v1 web actions require observe=false",
            },
        )
    provider = _web(entry)
    payload: dict[str, Any] = {}
    if request.action not in ("elements", "text", "frames", "tabs"):
        entry["blind_desktop_streak"] = 0
        entry["recent_commands"] = []
        entry["repeat_count"] = 0
    if request.action != "js":
        # "Consecutive" means consecutive program calls. A deliberate switch
        # to the element/text/action interface is exactly the behaviour this
        # guard is intended to encourage, so it clears the inspection streak.
        entry["web_readonly_js_streak"] = 0

    # Repeat detection for the WEB path.
    #
    # This existed only on /step (the pyautogui endpoint), so in browser-only
    # mode -- the configuration every result so far was produced in -- the
    # anti-loop signal was entirely inactive. The agent could issue the same
    # web_click twenty times and never be told. Loop-to-budget-exhaustion is
    # the single largest measured failure mode (6-7 of 12 episodes), so the
    # safety net was off over exactly the failure it was built for.
    #
    # Keyed on the full request so "click 3, click 4, click 3" is not mistaken
    # for repetition, and only for actions that are supposed to change something
    # -- re-listing elements or re-reading text is legitimate polling.
    if request.action in ("click", "type", "navigate", "scroll", "actions") or (
        request.action == "js" and request.expect_change
    ):
        signature = (
            request.action, request.index, request.text, request.url,
            request.direction, request.frame, request.code,
            json.dumps(request.actions, sort_keys=True) if request.actions else None,
        )
        if signature == entry.get("last_web_action"):
            entry["web_repeat"] = entry.get("web_repeat", 0) + 1
        else:
            entry["web_repeat"] = 0
        entry["last_web_action"] = signature
        if entry["web_repeat"]:
            payload["repeated_action"] = entry["web_repeat"]

    limit = 60 if request.compact else None
    chars = 48 if request.compact else 80

    def _describe(els):
        return provider.describe(els, limit, chars)

    try:
        if request.action == "elements":
            els = provider.elements()
            entry["web_elements"] = els
            payload["web_elements"] = _describe(els)
            payload["web_element_count"] = len(els)
        elif request.action == "navigate":
            payload["result"] = provider.navigate(request.url or "about:blank")
            # A new document is a new plan. Do not carry stale no-op pressure
            # from the previous page into its first valid action.
            entry["web_nochange"] = 0
            entry["web_readonly_js_streak"] = 0
            entry["web_elements"] = provider.elements()
            payload["web_elements"] = _describe(entry["web_elements"])
        elif request.action == "text":
            payload["page_text"] = provider.page_text(query=request.query)
        elif request.action == "search":
            payload["result"] = provider.search(
                request.queries or [], request.result_limit or 5,
            )
        elif request.action == "read_pages":
            payload["result"] = provider.read_pages(
                request.urls or [], request.text_limit or 2500,
            )
        elif request.action == "frames":
            frames = provider.frames()
            payload["result"] = "\n".join(
                f"{f['index']}\t{f['name']}\t{f['title']}\t{f['url'][:120]}"
                for f in frames
            ) or "no scriptable frames"
        elif request.action == "js":
            if request.expect_change:
                entry["web_readonly_js_streak"] = 0
            else:
                entry["web_readonly_js_streak"] = (
                    entry.get("web_readonly_js_streak", 0) + 1
                )
            non_browser_runtime = _non_browser_js_reason(request.code or "")
            surgery = _dom_surgery_reason(request.code or "")
            readonly_limit_reached = (
                not request.expect_change
                and entry.get("web_readonly_js_streak", 0)
                > MAX_CONSECUTIVE_READONLY_JS
            )
            if readonly_limit_reached:
                # Exact-action warnings were not enough: weaker models varied
                # selectors/scripts and spent as many as fourteen consecutive
                # calls rediscovering the same page. This capability-level gate
                # still permits six distinct inspections, then requires a real
                # strategy transition (read/list/navigate/action/desktop/exec)
                # before more JavaScript. No task, site, selector or evaluator
                # vocabulary participates in the decision.
                entry["web_readonly_js_streak"] = MAX_CONSECUTIVE_READONLY_JS
                payload["readonly_js_streak"] = MAX_CONSECUTIVE_READONLY_JS
                payload["errors"] = [
                    f"Strategy gate: {MAX_CONSECUTIVE_READONLY_JS} consecutive "
                    "read-only browser programs already ran without a causal "
                    "action. This program was not executed. Use web_read, "
                    "web_elements, navigation, a real web/desktop action, or "
                    "bounded computer_exec to change strategy before inspecting "
                    "with JavaScript again. Do not mark a read-only script as "
                    "expect_change merely to bypass the gate."
                ]
            elif non_browser_runtime:
                payload["errors"] = [
                    "Non-browser JavaScript rejected: web_js runs inside the current "
                    "page, not Node.js or a shell. require(), process, filesystem and "
                    "child-process APIs are unavailable. Use DOM helpers or the "
                    "semantic desktop controls offered by the harness."
                ]
            elif surgery:
                payload["errors"] = [
                    "DOM surgery rejected: browser code may inspect state and invoke "
                    "real controls, but it may not hide, remove, force-enable or "
                    "rewrite page UI. Use a fresh indexed or semantic action."
                ]
            else:
                before = provider.elements() if request.expect_change else None
                payload["result"] = provider.run_js(
                    request.code or "null",
                    request.frame if request.frame is not None else 0,
                )
                if entry.get("web_readonly_js_streak", 0) > 2:
                    payload["readonly_js_streak"] = entry["web_readonly_js_streak"]
            if request.expect_change and "result" in payload:
                entry["web_elements"] = provider.elements()
                if _elements_signature(entry["web_elements"]) == _elements_signature(before or []):
                    entry["web_nochange"] = entry.get("web_nochange", 0) + 1
                    payload["web_no_change"] = entry["web_nochange"]
                else:
                    entry["web_nochange"] = 0
                if request.compact:
                    payload["web_elements_note"] = (
                        f"page now has {len(entry['web_elements'])} interactive elements; "
                        "call web_elements to see them (indices have moved)"
                    )
                else:
                    payload["web_elements"] = _describe(entry["web_elements"])
        elif request.action == "actions":
            entry["web_readonly_js_streak"] = 0
            mutates = any(
                str(action.get("op") or "") != "wait"
                for action in (request.actions or [])
            )
            before = provider.elements() if mutates else None
            payload["result"] = provider.run_actions(
                request.actions or [],
                request.frame if request.frame is not None else 0,
            )
            entry["web_elements"] = provider.elements()
            if mutates and _elements_signature(entry["web_elements"]) == _elements_signature(before or []):
                entry["web_nochange"] = entry.get("web_nochange", 0) + 1
                payload["web_no_change"] = entry["web_nochange"]
            elif mutates:
                entry["web_nochange"] = 0
            if request.compact:
                payload["web_elements_note"] = (
                    f"page now has {len(entry['web_elements'])} interactive elements; "
                    "call web_elements to see them (indices have moved)"
                )
            else:
                payload["web_elements"] = _describe(entry["web_elements"])
        elif request.action == "tabs":
            payload["tabs"] = provider.tabs()
            payload["result"] = "\n".join(
                f"{t['index']}{' *active' if t['active'] else ''}\t{t['title']}\t{t['url'][:80]}"
                for t in payload["tabs"]) or "no open tabs"
        elif request.action == "switch_tab":
            if request.index is None:
                raise HTTPException(status_code=400, detail="tab index required")
            payload["result"] = provider.switch_tab(request.index)
            entry["web_elements"] = provider.elements()
            payload["web_elements"] = _describe(entry["web_elements"])
        elif request.action == "close_tab":
            if request.index is None:
                raise HTTPException(status_code=400, detail="tab index required")
            payload["result"] = provider.close_tab(request.index)
        elif request.action == "find":
            # Search the FULL element set, not the truncated display list, then
            # make the matches the active index space so click/type can target
            # them. Without this a control past the display cap is unreachable
            # no matter how many times the agent re-lists.
            allels = provider.elements()
            hits = provider.find(request.query or "", allels)
            entry["web_elements"] = hits
            payload["web_elements"] = _describe(hits)
            payload["web_element_count"] = len(hits)
            payload["result"] = (
                f"{len(hits)} of {len(allels)} interactive elements match "
                f"{request.query!r}. Indices below refer to these matches."
                if hits else
                f"No interactive element matches {request.query!r}. It may not be on the "
                f"page yet, or it may be plain text rather than a control — try "
                f"web_read, or a shorter search term.")
        elif request.action == "scroll":
            payload["result"] = provider.scroll(request.direction or "down",
                                                request.amount or 3)
            entry["web_elements"] = provider.elements()
            payload["web_elements"] = _describe(entry["web_elements"])
        elif request.action in ("click", "type"):
            entry["web_readonly_js_streak"] = 0
            els = entry.get("web_elements") or []
            if not els:
                els = provider.elements()
                entry["web_elements"] = els
            if request.index is None or not (0 <= request.index < len(els)):
                raise HTTPException(
                    status_code=400,
                    detail=f"index out of range (0..{max(0, len(els) - 1)})",
                )
            if request.action == "click":
                payload["result"] = provider.click(request.index, els)
            else:
                payload["result"] = provider.type_into(request.index, request.text or "", els)
            # (element list already refreshed above for change detection)
            # DOM-level no-op detection. A pixel diff can miss a click that did
            # nothing (or fire on an unrelated animation); comparing the
            # interactive-element list before and after says directly whether
            # the action changed anything actionable.
            before_sig = _elements_signature(els)
            entry["web_elements"] = provider.elements()
            if _elements_signature(entry["web_elements"]) == before_sig:
                entry["web_nochange"] = entry.get("web_nochange", 0) + 1
                payload["web_no_change"] = entry["web_nochange"]
            else:
                entry["web_nochange"] = 0
            if request.compact:
                payload["web_elements_note"] = (
                    f"page now has {len(entry['web_elements'])} interactive elements; "
                    "call web_elements to see them (indices have moved)")
            else:
                payload["web_elements"] = _describe(entry["web_elements"])
        else:
            raise HTTPException(status_code=400, detail=f"unknown action {request.action}")
    except HTTPException:
        raise
    except IndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        cdp_unavailable = "could not reach Chrome over CDP" in str(exc)
        if request.action == "navigate" and cdp_unavailable:
            # web_navigate is idempotent, so it is the one browser mutation we
            # may safely recover and retry. Never replay a click, form submit,
            # download, or arbitrary JavaScript after an uncertain failure.
            if entry.get("web_provider") is provider:
                entry["web_provider"] = None
            try:
                provider.retire()
            except Exception:
                pass
            chrome_running = _guest_chrome_running(entry)
            if chrome_running is True:
                payload["errors"] = [
                    "Chrome is already running without a CDP endpoint, and a running "
                    "browser cannot gain one retroactively. Do not repeat web tools. "
                    "Use desktop_find and named desktop controls for the current "
                    "browser. If DOM tools are required, close Chrome through those "
                    "named controls, then call web_navigate again."
                ]
                return payload
            try:
                target = request.url or "about:blank"
                _launch_cdp_browser_for_navigation(entry, target)
                provider = _web(entry)
                payload["result"] = provider.navigate(target)
                entry["web_elements"] = provider.elements()
                payload["web_elements"] = _describe(entry["web_elements"])
                payload["web_provider_recovered"] = True
                entry["web_nochange"] = 0
                entry["web_readonly_js_streak"] = 0
            except Exception as recovery_exc:
                payload["errors"] = [
                    "Chrome is not reachable through CDP and a safe navigation "
                    "launch did not recover it. Do not repeat web tools. Use "
                    "desktop_find(query='Chrome') and the named desktop controls "
                    "for the currently running browser. Recovery error: "
                    f"{type(recovery_exc).__name__}: {recovery_exc}"
                ]
        elif cdp_unavailable:
            payload["errors"] = [
                "Chrome is not reachable through CDP. Do not repeat this web "
                "action. If no browser is open, use web_navigate with the required "
                "URL so the harness can safely launch one. If Chrome is already "
                "open without CDP, use desktop_find and named desktop controls."
            ]
        elif getattr(exc, "provider_stalled", False):
            # A timed-out request otherwise leaves the provider's sole,
            # Playwright-bound thread occupied. Retire that connection and
            # lazily attach a fresh one on the next model action. Do not retry
            # this action automatically: clicks and submissions are not
            # idempotent, so an invisible retry could duplicate user intent.
            if entry.get("web_provider") is provider:
                entry["web_provider"] = None
            entry.pop("web_elements", None)
            try:
                provider.retire()
            except Exception:
                pass
            payload["web_provider_recovered"] = True
            payload["errors"] = [f"{type(exc).__name__}: {exc}"]
        else:
            payload["errors"] = [f"{type(exc).__name__}: {exc}"]
    entry["steps"] += 1
    obs = None
    if request.observe:
        try:
            obs = _pinned(entry, entry["env"]._get_obs)
        except Exception:
            pass
    payload.update({"steps": entry["steps"], **_encode(obs, entry)})
    return payload


class ElementAction(BaseModel):
    index: int
    action: str = "click"          # click | double_click | right_click | type | focus
    text: str | None = None
    pause: float = 1.0


class ElementFind(BaseModel):
    query: str = ""
    role: str | None = None
    state: str | None = None
    context: str | None = None


class ElementMatch(BaseModel):
    ref: str | None = None
    query: str = ""
    role: str | None = None
    state: str | None = None
    context: str | None = None
    action: str = "click"  # click | type | focus/hover
    text: str | None = None
    pause: float = 1.0


def _normalize_semantic(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _public_semantic_record(record: dict[str, Any]) -> str:
    public = {
        "ref": record.get("ref") or "",
        "role": record.get("role") or "",
        "name": record.get("name") or "",
        "text": record.get("text") or "",
        "value": record.get("value") or "",
        "states": record.get("states") or [],
        "actions": record.get("actions") or [],
        "context": record.get("context") or [],
    }
    return json.dumps(public, ensure_ascii=False, separators=(",", ":"))


def _same_semantic_ref_target(
    previous: dict[str, Any], current: dict[str, Any],
) -> bool:
    """Prove that a snapshot ref still names the same accessibility node."""
    return (
        list(previous.get("path") or []) == list(current.get("path") or [])
        and _normalize_semantic(previous.get("role"))
        == _normalize_semantic(current.get("role"))
        and _normalize_semantic(previous.get("name"))
        == _normalize_semantic(current.get("name"))
        and [_normalize_semantic(value) for value in previous.get("context") or []]
        == [_normalize_semantic(value) for value in current.get("context") or []]
    )


def _semantic_match_rank(record: dict[str, Any], query: str) -> int | None:
    needle = _normalize_semantic(query)
    if not needle:
        return 0
    primary = [
        _normalize_semantic(record.get(field))
        for field in ("name", "text", "value")
        if record.get(field)
    ]
    if any(value == needle for value in primary):
        return 0
    if any(value.startswith(needle) for value in primary):
        return 1
    if any(needle in value for value in primary):
        return 2
    secondary = [
        _normalize_semantic(record.get("role")),
        *(_normalize_semantic(value) for value in record.get("states") or []),
        *(_normalize_semantic(value) for value in record.get("actions") or []),
        *(_normalize_semantic(value) for value in record.get("context") or []),
    ]
    if any(needle in value for value in secondary):
        return 3
    return None


def _deduplicate_semantic_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse parallel wrappers only when they describe the same physical target.

    Identical labels at different bounds are genuinely ambiguous and must remain
    separate. The previous string-only dedupe silently chose the first one.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        label = tuple(
            _normalize_semantic(record.get(field))
            for field in ("name", "text", "value")
        )
        key = (tuple(record.get("bounds") or []), label)
        groups.setdefault(key, []).append(record)

    deduplicated: list[dict[str, Any]] = []
    for group in groups.values():
        if len(group) == 1:
            deduplicated.extend(group)
            continue
        advertised = [record for record in group if record.get("actions")]
        if len(advertised) == 1:
            # A leaf advertising an actual platform action plus one or more
            # passive same-bound wrappers is one executable affordance.
            deduplicated.extend(advertised)
            continue
        public_shapes = {
            (
                _normalize_semantic(record.get("role")),
                tuple(record.get("states") or []),
                tuple(record.get("actions") or []),
            )
            for record in group
        }
        if len(public_shapes) == 1:
            # Byte-equivalent provider duplicates have no disambiguating signal.
            deduplicated.append(max(group, key=lambda record: len(record.get("path") or [])))
            continue
        # Multiple actionable nodes or distinct roles/states at the same bounds
        # remain ambiguous. Do not silently guess that one is a wrapper.
        deduplicated.extend(group)
    return deduplicated


def _matching_semantic_records(
    entry: dict[str, Any],
    query: str,
    role: str | None = None,
    state: str | None = None,
    context: str | None = None,
) -> list[dict[str, Any]]:
    if not any(_normalize_semantic(value) for value in (query, role, state, context)):
        raise HTTPException(
            status_code=400, detail="query, role, state or context required",
        )
    role_needle = _normalize_semantic(role).replace("_", "-")
    state_needle = _normalize_semantic(state).replace("_", "-")
    context_needle = _normalize_semantic(context)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for record in entry.get("semantic_elements") or []:
        record_role = _normalize_semantic(record.get("role")).replace("_", "-")
        if role_needle and role_needle not in record_role:
            continue
        record_states = {
            _normalize_semantic(value).replace("_", "-")
            for value in record.get("states") or []
        }
        if state_needle and state_needle not in record_states:
            continue
        record_context = " ".join(
            _normalize_semantic(value) for value in record.get("context") or []
        )
        if context_needle and context_needle not in record_context:
            continue
        rank = _semantic_match_rank(record, query)
        if rank is not None:
            ranked.append((rank, record))
    ranked.sort(key=lambda item: (
        item[0],
        0 if item[1].get("actionable") else 1,
        len(item[1].get("bounds") or []),
        item[1].get("index", 0),
    ))
    return _deduplicate_semantic_records([record for _, record in ranked])


def _listed_semantic_records(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a compact, useful overview without requiring a guessed label.

    A semantic interface is not actually observable if the caller must already
    know a control's name before it can read the tree. Rank focused/current
    surface controls first, then other actionable controls, while preserving
    passive app/window/dialog rows that explain which surfaces exist.
    """
    records = _deduplicate_semantic_records(
        list(entry.get("semantic_elements") or []),
    )
    focused_owners = {
        context
        for record in records
        if "focused" in (record.get("states") or [])
        for context in (record.get("context") or [])
        if context.partition(":")[0].casefold() in _SEMANTIC_OWNER_ROLES
    }

    def rank(record: dict[str, Any]) -> tuple[int, int, int]:
        states = set(record.get("states") or [])
        contexts = set(record.get("context") or [])
        role = _normalize_semantic(record.get("role")).replace("_", "-")
        is_surface = role in _SEMANTIC_OWNER_ROLES
        shares_focus_owner = bool(focused_owners & contexts)
        if "focused" in states:
            tier = 0
        elif shares_focus_owner and record.get("actionable"):
            tier = 1
        elif shares_focus_owner or is_surface:
            tier = 2
        elif record.get("actionable"):
            tier = 3
        else:
            tier = 4
        return (tier, 0 if record.get("actions") else 1, record.get("index", 0))

    return sorted(records, key=rank)


def _refresh_semantic_snapshot(entry: dict[str, Any]) -> str | None:
    """Refresh private labels/bounds before resolving a semantic action."""
    try:
        obs = _pinned(entry, entry["env"]._get_obs)
    except Exception as exc:
        entry["marks"] = []
        entry["som_elements"] = []
        entry["semantic_elements"] = []
        entry["semantic_refs"] = {}
        return f"Accessibility snapshot refresh failed: {type(exc).__name__}: {exc}"
    if not obs or not obs.get("accessibility_tree"):
        entry["marks"] = []
        entry["som_elements"] = []
        entry["semantic_elements"] = []
        entry["semantic_refs"] = {}
        return "Accessibility snapshot is unavailable; stale targets were discarded."
    _encode(obs, entry, compare_change=False)
    if not entry.get("semantic_elements"):
        return "Accessibility snapshot contained no visible semantic targets."
    return None


@app.post("/episodes/{episode_id}/element/find")
def element_find(episode_id: str, request: ElementFind) -> dict[str, Any]:
    """Search the complete current accessibility list and re-ground strategy."""
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/element/find")
    # desktop_find is an explicit semantic observation, so it satisfies the
    # same grounding checkpoint as a screenshot even if no target is selected.
    entry["blind_desktop_streak"] = 0
    refresh_error = _refresh_semantic_snapshot(entry)
    if refresh_error:
        return {"steps": entry["steps"], "errors": [refresh_error]}
    has_filter = any(
        _normalize_semantic(value)
        for value in (request.query, request.role, request.state, request.context)
    )
    matches = (
        _matching_semantic_records(
            entry, request.query, request.role, request.state, request.context,
        )
        if has_filter else _listed_semantic_records(entry)
    )
    shown = min(30, len(matches))
    listing_header = (
        f"Matching live desktop accessibility controls (showing {shown} of "
        f"{len(matches)})."
        + (
            " Add query/role/state/context filters to search the complete snapshot."
            if len(matches) > shown else ""
        )
    )
    return {
        "steps": entry["steps"],
        "candidate_count": len(matches),
        "semantic_snapshot": entry.get("semantic_snapshot"),
        "result": (
            listing_header + "\n"
            + "\n".join(_public_semantic_record(record) for record in matches[:30])
            if matches
            else "No current desktop accessibility control matches the supplied "
            "query/role/state/context filters."
        ),
    }


@app.post("/episodes/{episode_id}/element/match")
def element_match(episode_id: str, request: ElementMatch) -> dict[str, Any]:
    """Act on one uniquely named desktop control from the current a11y frame."""
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/element/match")
    previous_ref = None
    if request.ref:
        previous_ref = (entry.get("semantic_refs") or {}).get(request.ref)
        if previous_ref is None:
            return {
                "steps": entry["steps"],
                "errors": [
                    "Desktop accessibility reference is unknown or stale. Call "
                    "desktop_find again and use a ref from the current snapshot."
                ],
            }
    refresh_error = _refresh_semantic_snapshot(entry)
    if refresh_error:
        return {"steps": entry["steps"], "errors": [refresh_error]}
    if previous_ref is not None:
        matches = [
            record for record in entry.get("semantic_elements") or []
            if _same_semantic_ref_target(previous_ref, record)
        ]
        if len(matches) != 1:
            return {
                "steps": entry["steps"],
                "errors": [
                    "Desktop accessibility reference became stale after the UI "
                    "changed. No action was taken; call desktop_find again."
                ],
            }
    else:
        matches = _matching_semantic_records(
            entry, request.query, request.role, request.state, request.context,
        )
        if matches:
            best_rank = _semantic_match_rank(matches[0], request.query)
            matches = [
                record for record in matches
                if _semantic_match_rank(record, request.query) == best_rank
            ]
    if len(matches) != 1:
        return {
            "steps": entry["steps"],
            "errors": [
                (
                    f"Desktop semantic target is ambiguous ({len(matches)} matches). "
                    "Add an exact label, role, state or context filter before acting."
                    if matches else
                    "Desktop semantic target was not found in the current frame. "
                    "Re-observe the current application state."
                )
            ],
            "result": (
                "\n".join(_public_semantic_record(record) for record in matches[:30])
                if matches else None
            ),
        }
    action = request.action
    if action not in ("click", "type", "focus", "hover"):
        raise HTTPException(status_code=400, detail="action must be click, type or hover")
    selected = matches[0]
    payload = _execute_desktop_target(
        entry=entry,
        target_index=int(selected["index"]),
        bounds=list(selected["bounds"]),
        semantic_record=selected,
        action="focus" if action == "hover" else action,
        text=request.text,
        pause=request.pause,
    )
    verb = {
        "click": "clicked",
        "type": "typed into",
        "focus": "hovered over",
        "hover": "hovered over",
    }[action]
    payload["result"] = f"{verb} desktop control: {_public_semantic_record(selected)}"
    return payload


def _execute_desktop_target(
    *,
    entry: dict[str, Any],
    target_index: int,
    bounds: list[int],
    semantic_record: dict[str, Any] | None,
    action: str,
    text: str | None,
    pause: float,
) -> dict[str, Any]:
    """Execute one already-resolved desktop target against its own live record."""
    entry["blind_desktop_streak"] = 0
    entry["recent_commands"] = []
    entry["repeat_count"] = 0
    x, y, w, h = bounds
    cx, cy = int(x + w / 2), int(y + h / 2)
    env: DesktopEnv = entry["env"]

    def native_or_pointer_command(
        pointer_command: str,
        *,
        focus_only: bool = False,
    ) -> str:
        """Prefer AT-SPI capability, with current real-input bounds as fallback."""
        if not semantic_record or not semantic_record.get("path"):
            return pointer_command
        advertised = semantic_record.get("actions") or []
        if not focus_only and not advertised:
            return pointer_command
        path = [int(index) for index in semantic_record["path"]]
        expected_role = _normalize_semantic(semantic_record.get("role")).replace("-", " ")
        expected_name = str(semantic_record.get("name") or "")
        role_key = _normalize_semantic(semantic_record.get("role")).replace("_", "-")
        transient_descriptors = []
        for descriptor in semantic_record.get("context") or []:
            owner_role, separator, owner_name = str(descriptor).partition(":")
            owner_role = _normalize_semantic(owner_role)
            if owner_role in {"alert", "dialog"}:
                transient_descriptors.append((
                    owner_role,
                    owner_name if separator else "",
                ))
        transient_owner = bool(transient_descriptors)
        expected_transient_role, expected_transient_name = (
            transient_descriptors[-1] if transient_descriptors else ("", "")
        )
        verify_transient_dismissal = (
            action == "click" and "button" in role_key and transient_owner
        )
        if verify_transient_dismissal:
            # Chromium's transient accessibility subtree can reorder between
            # snapshot and execution. A previously resolved child-index path
            # may then name the sibling Cancel button; its disappearance looks
            # like success even though the requested action never occurred.
            # Use the target's current real-input bounds first, then re-resolve
            # the exact role/name/owning dialog globally and invoke that live
            # object directly. No stale path participates in this branch.
            transient_script = "\n".join((
                "import pyatspi",
                "import time as _semantic_time",
                "def _semantic_normalize(_semantic_value):",
                "    return ' '.join(str(_semantic_value or '').casefold().replace('-', ' ').split())",
                "def _semantic_find_transient_target():",
                "    try:",
                "        _semantic_stack = [(pyatspi.Registry.getDesktop(0), '', '')]",
                "        _semantic_seen = 0",
                "        while _semantic_stack and _semantic_seen < 5000:",
                "            _semantic_current, _semantic_owner_role, _semantic_owner_name = _semantic_stack.pop()",
                "            _semantic_seen += 1",
                "            _semantic_current_role = _semantic_normalize(_semantic_current.getRoleName())",
                "            _semantic_current_name = str(_semantic_current.name or '')",
                "            if _semantic_current_role in ('alert', 'dialog'):",
                "                _semantic_owner_role = _semantic_current_role",
                "                _semantic_owner_name = _semantic_current_name",
                "            if (",
                f"                _semantic_current_role == {expected_role!r}",
                f"                and (not {expected_name!r} or _semantic_current_name == {expected_name!r})",
                f"                and _semantic_owner_role == {expected_transient_role!r}",
                f"                and (not {expected_transient_name!r} or _semantic_owner_name == {expected_transient_name!r})",
                "            ):",
                "                return _semantic_current",
                "            try:",
                "                for _semantic_child_index in range(_semantic_current.childCount - 1, -1, -1):",
                "                    _semantic_stack.append((_semantic_current[_semantic_child_index], _semantic_owner_role, _semantic_owner_name))",
                "            except Exception:",
                "                pass",
                "    except Exception:",
                "        pass",
                "    return None",
                "def _semantic_activate_live(_semantic_target):",
                "    try:",
                "        _semantic_actions = _semantic_target.queryAction()",
                "        _semantic_names = [' '.join(_semantic_actions.getName(i).casefold().replace('-', ' ').split()) for i in range(_semantic_actions.nActions)]",
                "        for _semantic_preferred in ('click', 'press', 'activate', 'open', 'select', 'toggle', 'check'):",
                "            _semantic_matches = [i for i, name in enumerate(_semantic_names) if _semantic_preferred in name]",
                "            if _semantic_matches:",
                "                return bool(_semantic_actions.doAction(_semantic_matches[0]))",
                "    except Exception:",
                "        pass",
                "    return False",
                "_semantic_pointer_first = True",
                pointer_command,
                "_semantic_time.sleep(0.6)",
                "_semantic_after_pointer = _semantic_find_transient_target()",
                "if _semantic_after_pointer is not None:",
                "    _semantic_activate_live(_semantic_after_pointer)",
                "    _semantic_time.sleep(0.6)",
                "_semantic_after_native = _semantic_find_transient_target()",
                "if _semantic_after_native is not None:",
                "    try:",
                "        _semantic_after_native.queryComponent().grabFocus()",
                "    except Exception:",
                "        pass",
                "    pyautogui.press('enter')",
                "    _semantic_time.sleep(0.6)",
                "if _semantic_find_transient_target() is not None:",
                "    raise RuntimeError('transient desktop control did not dismiss')",
            ))
            return f"exec({transient_script!r})"
        script = "\n".join((
            "import pyatspi",
            *(
                (
                    "import time as _semantic_time",
                    "def _semantic_normalize(_semantic_value):",
                    "    return ' '.join(str(_semantic_value or '').casefold().replace('-', ' ').split())",
                    "def _semantic_find_transient_target():",
                    "    try:",
                    "        _semantic_stack = [(pyatspi.Registry.getDesktop(0), '', '')]",
                    "        _semantic_seen = 0",
                    "        while _semantic_stack and _semantic_seen < 5000:",
                    "            _semantic_current, _semantic_owner_role, _semantic_owner_name = _semantic_stack.pop()",
                    "            _semantic_seen += 1",
                    "            _semantic_current_role = _semantic_normalize(_semantic_current.getRoleName())",
                    "            _semantic_current_name = str(_semantic_current.name or '')",
                    "            if _semantic_current_role in ('alert', 'dialog'):",
                    "                _semantic_owner_role = _semantic_current_role",
                    "                _semantic_owner_name = _semantic_current_name",
                    "            if (",
                    f"                _semantic_current_role == {expected_role!r}",
                    f"                and (not {expected_name!r} or _semantic_current_name == {expected_name!r})",
                    f"                and _semantic_owner_role == {expected_transient_role!r}",
                    f"                and (not {expected_transient_name!r} or _semantic_owner_name == {expected_transient_name!r})",
                    "            ):",
                    "                return _semantic_current",
                    "            try:",
                    "                for _semantic_child_index in range(_semantic_current.childCount - 1, -1, -1):",
                    "                    _semantic_stack.append((_semantic_current[_semantic_child_index], _semantic_owner_role, _semantic_owner_name))",
                    "            except Exception:",
                    "                pass",
                    "    except Exception:",
                    "        pass",
                    "    return None",
                )
                if verify_transient_dismissal else ()
            ),
            "_semantic_ok = False",
            "try:",
            "    _semantic_node = pyatspi.Registry.getDesktop(0)",
            f"    for _semantic_index in {path!r}:",
            "        _semantic_node = _semantic_node[_semantic_index]",
            "    _semantic_role = ' '.join(_semantic_node.getRoleName().casefold().replace('-', ' ').split())",
            f"    if _semantic_role != {expected_role!r}:",
            "        raise RuntimeError('semantic role changed')",
            f"    if {expected_name!r} and _semantic_node.name != {expected_name!r}:",
            "        raise RuntimeError('semantic name changed')",
            *(
                (
                    "    _semantic_node.queryComponent().grabFocus()",
                    "    _semantic_ok = True",
                )
                if focus_only else
                (
                    "    _semantic_actions = _semantic_node.queryAction()",
                    "    _semantic_names = [' '.join(_semantic_actions.getName(i).casefold().replace('-', ' ').split()) for i in range(_semantic_actions.nActions)]",
                    "    for _semantic_preferred in ('click', 'press', 'activate', 'open', 'select', 'toggle', 'check'):",
                    "        _semantic_matches = [i for i, name in enumerate(_semantic_names) if _semantic_preferred in name]",
                    "        if _semantic_matches:",
                    # AT-SPI doAction returns False when an application exposes
                    # an action name but declines to execute it. Only suppress
                    # the current-bounds real-input fallback when the platform
                    # confirms that the native action actually ran.
                    "            _semantic_ok = bool(_semantic_actions.doAction(_semantic_matches[0]))",
                    "            break",
                )
            ),
            # Chromium sometimes returns True from doAction without dispatching
            # the confirmation-button press. For a button owned by a transient
            # dialog/alert, successful activation must at least remove or replace
            # that exact live target. Requery once after a short grace period;
            # if the same target remains, use the current-bounds pointer fallback.
            # Limit this to transient buttons so persistent page controls are
            # never replayed merely because they stay visible after a valid click.
            *(
                (
                    "    if _semantic_ok:",
                    "        _semantic_time.sleep(0.35)",
                    "        _semantic_same_transient_target = (_semantic_find_transient_target() is not None)",
                    "        if _semantic_same_transient_target:",
                    "            _semantic_ok = False",
                )
                if verify_transient_dismissal else ()
            ),
            "except Exception:",
            "    _semantic_ok = False",
            "if not _semantic_ok:",
            f"    {pointer_command}",
            # A transient target that survived the native action must also be
            # verified after the pointer fallback. Chromium can expose correct
            # bounds while a focus/default-button quirk still swallows that
            # click. Focused Enter is the final generic activation path; if the
            # same dialog target remains after it, surface a truthful error
            # instead of claiming success and poisoning the agent's state.
            *(
                (
                    "    _semantic_time.sleep(0.35)",
                    "    _semantic_after_pointer = _semantic_find_transient_target()",
                    "    _semantic_pointer_target_remains = (_semantic_after_pointer is not None)",
                    "    if _semantic_pointer_target_remains:",
                    "        try:",
                    "            _semantic_after_pointer.queryComponent().grabFocus()",
                    "        except Exception:",
                    "            pass",
                    "        pyautogui.press('enter')",
                    "        _semantic_time.sleep(0.35)",
                    "        _semantic_enter_target_remains = (_semantic_find_transient_target() is not None)",
                    "        if _semantic_enter_target_remains:",
                    "            raise RuntimeError('transient desktop control did not dismiss')",
                )
                if verify_transient_dismissal else ()
            ),
        ))
        return f"exec({script!r})"

    if action == "type":
        focus = native_or_pointer_command(
            f"pyautogui.click({cx}, {cy})", focus_only=True,
        )
        command = (
            f"{focus}; pyautogui.hotkey('ctrl','a'); pyautogui.press('delete'); "
            f"pyautogui.typewrite({(text or '')!r}, interval=0.02)"
        )
    elif action == "double_click":
        command = f"import pyautogui; pyautogui.click({cx}, {cy}, clicks=2)"
    elif action == "right_click":
        command = f"import pyautogui; pyautogui.click({cx}, {cy}, button='right')"
    elif action == "focus":
        command = f"import pyautogui; pyautogui.moveTo({cx}, {cy})"
    else:
        command = native_or_pointer_command(f"pyautogui.click({cx}, {cy})")

    obs = None
    error = None
    try:
        obs, _reward, done, _info = _pinned(entry, env.step, command, pause)
        entry["steps"] += 1
    except Exception as exc:
        done = False
        error = f"{type(exc).__name__}: {exc}"
    payload: dict[str, Any] = {
        "steps": entry["steps"], "done": done,
        "acted_on": {
            "index": target_index, "x": cx, "y": cy,
            "execution": (
                "native-accessibility-with-pointer-fallback"
                if semantic_record and action in ("click", "type")
                else "pointer"
            ),
        },
        **_encode(obs, entry),
    }
    if error:
        payload["errors"] = [error]
    return payload


@app.post("/episodes/{episode_id}/element")
def element_action(episode_id: str, request: ElementAction) -> dict[str, Any]:
    """Act on a Set-of-Marks element by index.

    The agent picks a number off the annotated screenshot; the server resolves
    it to the element's real screen coordinates. This removes pixel grounding
    from the model's job entirely, which is the dominant failure mode for
    smaller models.
    """
    entry = _get(episode_id)
    _reject_simple_runtime_route(entry, "/element")
    marks = entry.get("marks") or []
    if not marks:
        raise HTTPException(status_code=409, detail="no marks available; take a screenshot first")
    # OSWorld's Set-of-Marks helper labels the first element `1`, while the
    # Python list of private bounding boxes is zero-based. The old code used
    # the visible label directly as a list offset, so every semantic action hit
    # the following element while claiming it had clicked the requested one.
    if not (1 <= request.index <= len(marks)):
        raise HTTPException(
            status_code=400,
            detail=f"element {request.index} out of range (1..{len(marks)})",
        )
    semantic_records = entry.get("semantic_elements") or []
    semantic_record = (
        semantic_records[request.index - 1]
        if entry.get("marks_are_semantic") and request.index <= len(semantic_records)
        else None
    )
    return _execute_desktop_target(
        entry=entry,
        target_index=request.index,
        bounds=list(marks[request.index - 1]),
        semantic_record=semantic_record,
        action=request.action,
        text=request.text,
        pause=request.pause,
    )


@app.post("/episodes/{episode_id}/evaluate")
def evaluate(episode_id: str) -> dict[str, Any]:
    entry = _get(episode_id)
    env: DesktopEnv = entry["env"]
    try:
        score = _pinned(entry, env.evaluate)
    except Exception as error:
        logger.exception("evaluation failed for %s", episode_id)
        return {"score": 0.0, "error": f"{type(error).__name__}: {error}"}
    return {"score": float(score), "steps": entry["steps"]}


@app.delete("/episodes/{episode_id}")
def close_episode(episode_id: str) -> dict[str, Any]:
    """Remove the episode, then tear it down.

    The registry entry is popped FIRST: teardown used to be able to hang, and
    while it hung the episode still counted as open and its browser stayed
    resident. Popping first means a slow teardown can never masquerade as a
    live episode.
    """
    entry = _get(episode_id)
    with _lock:
        _episodes.pop(episode_id, None)
    errors = []
    semantic_runtime = entry.get("semantic_runtime")
    if semantic_runtime:
        try:
            semantic_runtime.close()
        except Exception as exc:
            errors.append(f"semantic_runtime: {type(exc).__name__}")
            logger.warning("closing semantic runtime for %s failed", episode_id, exc_info=True)
    semantic_guest = entry.get("semantic_guest")
    if semantic_guest:
        try:
            _pinned(entry, guest_semantic.shutdown, entry["env"], semantic_guest)
        except Exception as exc:
            errors.append(f"semantic_guest: {type(exc).__name__}")
            logger.warning("closing semantic guest for %s failed", episode_id, exc_info=True)
    for label, shut in (("web_provider", entry.get("web_provider")),
                        ("env", entry.get("env"))):
        if not shut:
            continue
        try:
            shut.close()
        except Exception as exc:  # never let one leak block the other
            errors.append(f"{label}: {type(exc).__name__}")
            logger.warning("closing %s for %s failed", label, episode_id, exc_info=True)
    pool = entry.get("pool")
    if pool:
        pool.shutdown(wait=False)
    return {"closed": episode_id, **({"errors": errors} if errors else {})}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "episodes": list(_episodes),
        "semantic_protocol_version": "1.0",
        "server_runtime_hash": os.environ.get("OSWORLD_SERVER_RUNTIME_HASH", "unknown"),
        "guest_bundle_hash": guest_semantic.bundle_hash(),
    }
