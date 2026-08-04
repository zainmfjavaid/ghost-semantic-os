"""Bootstrap and proxy the versioned semantic daemon inside an OSWorld guest."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

_MARKER = "__GHOST_SEMANTIC__="
_DEFAULT_PORT = 8765
# Linux limits each argv string to 128 KiB even when ARG_MAX is larger.
# Keep ordinary daemon requests on the lower-overhead ``python -c`` route,
# but leave enough headroom for the controller's injected wrapper and the
# terminating NUL. Larger private requests must travel in a script body.
_MAX_INLINE_REQUEST_SCRIPT_BYTES = 96 * 1024
_GUEST_AGENT_ROOT = Path(__file__).resolve().parents[1] / "guest_agent"
_BUNDLE_FILES = (
    "chrome_cdp_launcher.py",
    "semantic_agent.py",
    "native_app_bridges.py",
    "package_installer.py",
)

_PACKAGE_HELPER = "/usr/local/libexec/ghost-semantic-install-package"
_PACKAGE_SUDOERS = "/etc/sudoers.d/ghost-semantic-package-installer"


def _bundle_contents() -> dict[str, bytes]:
    return {
        relative: (_GUEST_AGENT_ROOT / relative).read_bytes()
        for relative in _BUNDLE_FILES
    }


def _bundle_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        encoded_name = relative.encode("utf-8")
        content = files[relative]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class GuestSemanticError(RuntimeError):
    def __init__(self, message: str, *, code: str = "adapter_unavailable"):
        super().__init__(message)
        self.code = code


def _decode_envelope(response: Any) -> dict[str, Any]:
    output = str((response or {}).get("output") or "")
    marker = next(
        (line[len(_MARKER):] for line in reversed(output.splitlines()) if line.startswith(_MARKER)),
        None,
    )
    if marker is None:
        error = str(
            (response or {}).get("error")
            or (response or {}).get("message")
            or ""
        )
        raise GuestSemanticError(
            "guest semantic command returned no trusted envelope"
            + (f": {error[:500]}" if error else ""),
        )
    try:
        payload = json.loads(marker)
    except json.JSONDecodeError as error:
        raise GuestSemanticError("guest semantic command returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise GuestSemanticError("guest semantic command envelope was not an object")
    return payload


def _execute(env: Any, code: str) -> dict[str, Any]:
    runner = getattr(getattr(env, "controller", None), "execute_python_command", None)
    if not callable(runner):
        raise GuestSemanticError("guest controller has no execute_python_command")
    return _decode_envelope(runner(code))


def _run_script(env: Any, code: str) -> dict[str, Any]:
    """Run a large bootstrap body without placing it in a process argv.

    The released semantic bundle is larger than Linux's per-argument limit.
    OSWorld's ordinary execute_python_command route wraps source in
    ``python -c``, so a valid bundle is rejected by the guest with E2BIG before
    Python starts. The long-standing /run_python route transfers source in the
    HTTP body and executes a temporary file with the guest's system Python.
    It is private bootstrap transport and is never exposed as a model tool.
    """
    runner = getattr(getattr(env, "controller", None), "run_python_script", None)
    if not callable(runner):
        raise GuestSemanticError("guest controller has no run_python_script")
    return _decode_envelope(runner(code))


def _remote_request_code(
    *, token: str, port: int, method: str, path: str, payload: dict[str, Any] | None = None,
) -> str:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else b""
    return f"""
import json, urllib.request, urllib.error
url = 'http://127.0.0.1:{port}{path}'
body = {body!r}
request = urllib.request.Request(
    url,
    data=(body if {payload is not None!r} else None),
    method={method!r},
    headers={{
        'Authorization': 'Bearer ' + {token!r},
        'Content-Type': 'application/json',
    }},
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise RuntimeError('semantic response exceeds 1 MiB')
        result = json.loads(raw)
except urllib.error.HTTPError as error:
    result = json.loads(error.read(1048576) or b'{{}}')
print({_MARKER!r} + json.dumps(result, separators=(',', ':')))
"""


def bootstrap(env: Any, episode_id: str) -> dict[str, Any]:
    """Install and start the daemon before OSWorld task setup runs."""
    client_password = getattr(env, "client_password", None)
    if not isinstance(client_password, str) or not client_password:
        raise GuestSemanticError("guest semantic bootstrap has no guest privilege credential")
    bundle = _bundle_contents()
    bundle_hash = _bundle_digest(bundle)
    token = secrets.token_urlsafe(32)
    encoded = {
        relative: base64.b64encode(content).decode("ascii")
        for relative, content in bundle.items()
    }
    # episode_id is used only for a private directory name. It is generated by
    # the harness and carries no OSWorld task or evaluator identity.
    remote_root = f"/tmp/ghost-semantic-{episode_id}"
    remote_script = f"{remote_root}/semantic_agent.py"
    launch_code = f"""
import base64, hashlib, json, os, pathlib, pwd, re, shutil, stat, subprocess, sys, time, urllib.request
root = pathlib.Path({remote_root!r})
root.mkdir(mode=0o700, parents=True, exist_ok=True)
script = pathlib.Path({remote_script!r})
encoded_files = {encoded!r}
decoded_files = {{name: base64.b64decode(content) for name, content in encoded_files.items()}}
digest = hashlib.sha256()
for name in sorted(decoded_files):
    encoded_name = name.encode('utf-8')
    content = decoded_files[name]
    digest.update(len(encoded_name).to_bytes(4, 'big'))
    digest.update(encoded_name)
    digest.update(len(content).to_bytes(8, 'big'))
    digest.update(content)
if digest.hexdigest() != {bundle_hash!r}:
    raise RuntimeError('local aggregate bundle hash mismatch before write')
for name, content in decoded_files.items():
    destination = root / name
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(content)
    destination.chmod(
        0o700 if name in {{'semantic_agent.py', 'chrome_cdp_launcher.py'}} else 0o600
    )
verify = hashlib.sha256()
for name in sorted(decoded_files):
    encoded_name = name.encode('utf-8')
    content = (root / name).read_bytes()
    verify.update(len(encoded_name).to_bytes(4, 'big'))
    verify.update(encoded_name)
    verify.update(len(content).to_bytes(8, 'big'))
    verify.update(content)
if verify.hexdigest() != {bundle_hash!r}:
    raise RuntimeError('guest aggregate bundle hash mismatch after write')

# Make every ordinary desktop-mediated HTTP(S) launch of an installed Chrome
# family browser debuggable. Apps such as LibreOffice and Thunderbird resolve
# links through these desktop IDs, bypassing OSWorld's explicit Chrome setup
# commands. A per-user entry with the same ID takes precedence over the system
# entry without changing the user's selected default browser.
launcher = root / 'chrome_cdp_launcher.py'
applications = pathlib.Path.home() / '.local' / 'share' / 'applications'
applications.mkdir(mode=0o700, parents=True, exist_ok=True)
desktop_specs = (
    ('google-chrome.desktop', 'google-chrome', 'Google Chrome'),
    ('google-chrome-stable.desktop', 'google-chrome-stable', 'Google Chrome'),
    ('chromium.desktop', 'chromium', 'Chromium'),
    ('chromium-browser.desktop', 'chromium-browser', 'Chromium'),
)
default_browser = ''
xdg_settings = shutil.which('xdg-settings')
if xdg_settings is not None:
    default_probe = subprocess.run(
        [xdg_settings, 'get', 'default-web-browser'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=5, check=False,
    )
    if default_probe.returncode == 0:
        default_browser = default_probe.stdout.strip()
installed_browser_entries = []
for desktop_id, alias, display_name in desktop_specs:
    system_entry = pathlib.Path('/usr/share/applications') / desktop_id
    if (
        not system_entry.is_file()
        and shutil.which(alias) is None
        and default_browser != desktop_id
    ):
        continue
    destination = applications / desktop_id
    content = (
        '[Desktop Entry]\\n'
        'Version=1.0\\n'
        'Type=Application\\n'
        'Name=' + display_name + '\\n'
        'GenericName=Web Browser\\n'
        'Exec=' + str(launcher) + ' ' + alias + ' %U\\n'
        'Terminal=false\\n'
        'StartupNotify=true\\n'
        'MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\\n'
    )
    destination.write_text(content, encoding='utf-8')
    destination.chmod(0o600)
    installed_browser_entries.append(desktop_id)
desktop_database = shutil.which('update-desktop-database')
if desktop_database is not None and installed_browser_entries:
    database_update = subprocess.run(
        [desktop_database, str(applications)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, timeout=10, check=False,
    )
    if database_update.returncode != 0:
        raise RuntimeError(
            'semantic browser desktop integration failed: '
            + database_update.stderr.strip()[:300]
        )

# Install a root-owned package helper and a single sudo capability for it. The
# bootstrap credential is consumed through subprocess stdin before the policy
# loop. It is not copied into the installed bundle, argv/environment, returned
# state, or daemon; the controller's transient script transport removes itself.
username = pwd.getpwuid(os.getuid()).pw_name
if re.fullmatch(r'[a-z_][a-z0-9_-]{{0,31}}', username) is None:
    raise RuntimeError('guest username cannot be represented safely in sudoers')
helper_source = root / 'package_installer.py'
sudoers_source = root / 'package-installer.sudoers'
sudoers_source.write_text(
    username + ' ALL=(root) NOPASSWD: {_PACKAGE_HELPER} *\\n',
    encoding='utf-8',
)
sudoers_source.chmod(0o600)

def privileged(argv):
    completed = subprocess.run(
        ['/usr/bin/sudo', '-S', '-p', '', '--', *argv],
        input={client_password!r} + '\\n', text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        env={{'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'}},
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[:500]
        raise RuntimeError('privileged semantic bootstrap failed: ' + detail)

syntax = subprocess.run(
    ['/usr/sbin/visudo', '-cf', str(sudoers_source)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
)
if syntax.returncode != 0:
    raise RuntimeError('generated package sudoers rule is invalid')
privileged(['/usr/bin/install', '-d', '-o', 'root', '-g', 'root', '-m', '0755',
            str(pathlib.Path({_PACKAGE_HELPER!r}).parent)])
privileged(['/usr/bin/install', '-o', 'root', '-g', 'root', '-m', '0755',
            str(helper_source), {_PACKAGE_HELPER!r}])
privileged(['/usr/bin/install', '-o', 'root', '-g', 'root', '-m', '0440',
            str(sudoers_source), {_PACKAGE_SUDOERS!r}])

helper_stat = pathlib.Path({_PACKAGE_HELPER!r}).stat()
sudoers_stat = pathlib.Path({_PACKAGE_SUDOERS!r}).stat()
if (helper_stat.st_uid, helper_stat.st_gid, stat.S_IMODE(helper_stat.st_mode)) != (0, 0, 0o755):
    raise RuntimeError('package helper ownership or mode is unsafe')
if (sudoers_stat.st_uid, sudoers_stat.st_gid, stat.S_IMODE(sudoers_stat.st_mode)) != (0, 0, 0o440):
    raise RuntimeError('package sudoers ownership or mode is unsafe')
if pathlib.Path({_PACKAGE_HELPER!r}).read_bytes() != helper_source.read_bytes():
    raise RuntimeError('installed package helper differs from the signed bundle')

# Prove the NOPASSWD path without installing anything. The helper rejects the
# deliberately invalid uppercase name with EX_USAGE after sudo authorizes it.
probe = subprocess.run(
    ['/usr/bin/sudo', '-n', '--', {_PACKAGE_HELPER!r}, 'INVALID'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
)
if probe.returncode != 64 or 'exactly one validated Debian package name' not in probe.stderr:
    raise RuntimeError('package helper NOPASSWD probe failed')

environment = os.environ.copy()
environment['GHOST_SEMANTIC_TOKEN'] = {token!r}
environment['GHOST_SEMANTIC_PORT'] = {str(_DEFAULT_PORT)!r}
environment['GHOST_SEMANTIC_BUNDLE_HASH'] = {bundle_hash!r}
log = open(root / 'agent.log', 'ab', buffering=0)
process = subprocess.Popen(
    [sys.executable, str(script)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
    env=environment, start_new_session=True, close_fds=True,
)
(root / 'agent.pid').write_text(str(process.pid), encoding='ascii')
health = None
for _ in range(80):
    if process.poll() is not None:
        raise RuntimeError('guest semantic agent exited during startup')
    try:
        request = urllib.request.Request(
            'http://127.0.0.1:{_DEFAULT_PORT}/v1/health',
            headers={{'Authorization': 'Bearer ' + {token!r}}},
        )
        with urllib.request.urlopen(request, timeout=1) as response:
            health = json.loads(response.read())
        break
    except Exception:
        time.sleep(0.25)
if health is None:
    process.terminate()
    raise RuntimeError('guest semantic agent health timeout')
print({_MARKER!r} + json.dumps({{
    'ok': True,
    'pid': process.pid,
    'bundle_hash': {bundle_hash!r},
    'browser_desktop_entries': installed_browser_entries,
    'health': health,
}}, separators=(',', ':')))
"""
    launched = _run_script(env, launch_code)
    if not launched.get("ok"):
        raise GuestSemanticError("guest semantic bootstrap did not report success")
    health_payload = launched.get("health") or {}
    remote_health = health_payload.get("result") or {}
    if remote_health.get("bundle_hash") != bundle_hash:
        raise GuestSemanticError("guest semantic health returned wrong bundle hash")
    return {
        "token": token,
        "port": _DEFAULT_PORT,
        "bundle_hash": bundle_hash,
        "agent_version": remote_health.get("agent_version"),
        "guest_machine_id": remote_health.get("guest_machine_id"),
        "guest_os_release_hash": remote_health.get("guest_os_release_hash"),
        "guest_platform": remote_health.get("guest_platform"),
        "display_identity": remote_health.get("display_identity"),
        "browser_desktop_entries": launched.get("browser_desktop_entries") or [],
        "guest_pid": launched.get("pid"),
        "remote_root": remote_root,
    }


def request(
    env: Any, state: dict[str, Any], method: str, path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = _remote_request_code(
        token=str(state["token"]),
        port=int(state["port"]),
        method=method,
        path=path,
        payload=payload,
    )
    # Blob chunks are base64 inside JSON, so a valid daemon request can easily
    # exceed Linux's per-argument limit when execute_python_command wraps it in
    # ``python -c``. The script-body route is the same private transport used
    # for the semantic bootstrap and does not expose bytes to the model.
    if len(code.encode("utf-8")) > _MAX_INLINE_REQUEST_SCRIPT_BYTES:
        response = _run_script(env, code)
    else:
        response = _execute(env, code)
    return response


def shutdown(env: Any, state: dict[str, Any]) -> None:
    try:
        request(env, state, "POST", "/v1/shutdown", {})
    except Exception:
        # The nested desktop teardown is the final containment boundary.
        pass


def bundle_hash() -> str:
    return _bundle_digest(_bundle_contents())


def identity() -> dict[str, Any]:
    return {
        "bundle_paths": [
            os.fspath(_GUEST_AGENT_ROOT / relative) for relative in _BUNDLE_FILES
        ],
        "bundle_hash": bundle_hash(),
    }
