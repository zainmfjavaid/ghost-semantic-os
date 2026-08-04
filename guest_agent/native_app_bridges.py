#!/usr/bin/env python3
"""Task-agnostic native semantic bridges for the OSWorld guest.

This module is deliberately standalone.  The guest daemon can route
``/v1/adapters/<adapter-id>/<operation>`` requests to
:func:`dispatch_native_app_request` without giving the model a shell, display
socket, coordinate, key, screenshot, task JSON, or evaluator handle.

Only fixed native interfaces are used:

* MPRIS over the session D-Bus for VLC/media playback;
* Poppler command-line utilities and optional pypdf for PDF structure;
* deterministic file parsers plus fixed ffprobe/exiftool/tesseract invocations;
* an argv-only bubblewrap worker with no network or desktop/session bus.

Unavailable native functionality is reported as a typed
``representation_gap``.  It never falls back to UI automation.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote


BRIDGE_VERSION = "1.0.0-alpha.1"
MAX_TEXT = 2_000
MAX_RECORDS = 5_000
MAX_OUTPUT_BYTES = 128 * 1024
MAX_STDIN_BYTES = 1024 * 1024
MAX_ARGV = 256


def _bounded_text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value if value is not None else "")[:limit]


def _json_value(value: Any) -> Any:
    """Convert D-Bus/native scalar containers into bounded JSON values."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, bytes):
        return value[: MAX_TEXT // 2].hex()
    if isinstance(value, Mapping):
        return {
            _bounded_text(key, 256): _json_value(item)
            for key, item in list(value.items())[:1_000]
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in list(value)[:1_000]]
    return _bounded_text(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BridgeError(Exception):
    """A typed error which preserves mutation uncertainty on the wire."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        side_effect_state: str = "none",
        missing_capability: str | None = None,
        candidates: Sequence[Mapping[str, Any]] = (),
        suggested_resource: str | None = "system.health",
    ) -> None:
        super().__init__(message)
        if side_effect_state not in {"none", "applied", "unknown"}:
            raise ValueError("invalid side-effect state")
        self.code = code
        self.message = _bounded_text(message)
        self.retryable = bool(retryable)
        self.side_effect_state = side_effect_state
        self.missing_capability = missing_capability
        self.candidates = [dict(item) for item in candidates[:20]]
        self.suggested_resource = suggested_resource

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "side_effect_state": self.side_effect_state,
            "missing_capability": self.missing_capability,
            "candidates": self.candidates,
            "recovery": {
                "allowed_operations": ["computer.query"],
                "suggested_resource": self.suggested_resource,
            },
        }


def _gap(capability: str, message: str) -> BridgeError:
    return BridgeError(
        "representation_gap",
        message,
        missing_capability=capability,
        side_effect_state="none",
    )


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool = False


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


def run_fixed_command(
    argv: Sequence[str],
    *,
    stdin: bytes = b"",
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Execute an already-constructed argv array; never invoke a shell."""

    if not argv or not all(
        isinstance(item, str) and item and "\x00" not in item for item in argv
    ):
        raise BridgeError("invalid_request", "command argv is invalid")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, min(float(timeout), 300.0)),
            shell=False,
            check=False,
            env=(dict(env) if env is not None else None),
        )
    except FileNotFoundError as error:
        raise BridgeError(
            "adapter_unavailable",
            f"required native program is unavailable: {Path(argv[0]).name}",
            retryable=False,
        ) from error
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            tuple(argv),
            -1,
            bytes(error.stdout or b""),
            bytes(error.stderr or b""),
            time.monotonic() - started,
            True,
        )
    return CommandResult(
        tuple(argv),
        int(completed.returncode),
        bytes(completed.stdout),
        bytes(completed.stderr),
        time.monotonic() - started,
        False,
    )


def _default_guest_roots() -> tuple[Path, ...]:
    # These are guest artifact/workspace roots, never harness-host paths.
    values = (
        "/home/oai/share",
        "/home/oai/Desktop",
        "/home/user/work",
        "/home/user/Desktop",
        "/tmp/ghost-semantic",
    )
    return tuple(Path(value) for value in values)


class GuestPathPolicy:
    def __init__(self, roots: Sequence[str | Path] | None = None) -> None:
        # Explicit roots are dependency-injected for conformance tests or a
        # differently laid-out guest image.  The production default always
        # rejects well-known macOS host prefixes before containment checks.
        self._reject_host_prefixes = roots is None
        selected = roots if roots is not None else _default_guest_roots()
        self.roots = tuple(Path(root).resolve(strict=False) for root in selected)
        if not self.roots:
            raise ValueError("at least one guest root is required")

    def resolve(
        self,
        raw: Any,
        *,
        must_exist: bool = False,
        directory: bool | None = None,
    ) -> Path:
        if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw:
            raise BridgeError("invalid_request", "path must be an absolute guest path")
        if self._reject_host_prefixes and raw.startswith(("/Users/", "/Volumes/", "/private/")):
            raise BridgeError("permission_denied", "host paths are unavailable")
        path = Path(raw).resolve(strict=False)
        if not any(path == root or root in path.parents for root in self.roots):
            raise BridgeError("permission_denied", "path is outside guest artifact roots")
        if must_exist and not path.exists():
            raise BridgeError("not_found", "guest path does not exist")
        if directory is True and path.exists() and not path.is_dir():
            raise BridgeError("invalid_request", "guest path is not a directory")
        if directory is False and path.exists() and not path.is_file():
            raise BridgeError("invalid_request", "guest path is not a file")
        return path


class RefStore:
    """Opaque, stable-within-episode native references."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._by_key: dict[str, str] = {}
        self._values: dict[str, Any] = {}
        self._lock = threading.RLock()

    def put(self, key: str, value: Any) -> str:
        safe_key = _digest({"key": key})
        with self._lock:
            ref = self._by_key.get(safe_key)
            if ref is None:
                ref = f"{self.prefix}_{secrets.token_urlsafe(18)}"
                self._by_key[safe_key] = ref
            self._values[ref] = value
            return ref

    def get(self, ref: Any) -> Any:
        if not isinstance(ref, str):
            raise BridgeError("invalid_request", "target ref must be a string")
        with self._lock:
            if ref not in self._values:
                raise BridgeError("stale_ref", "native entity ref no longer resolves")
            return self._values[ref]

    def resolve(self, ref: str) -> Mapping[str, Any]:
        value = self.get(ref)
        if isinstance(value, Mapping):
            return {"ref": ref, **dict(value)}
        return {"ref": ref, "kind": type(value).__name__}


class NativeBridge:
    adapter_id = "native@1"
    execution_path = "native_api"

    def __init__(self) -> None:
        self.refs = RefStore(self.adapter_id.split("@", 1)[0].replace("-", "_"))
        self._closed = False

    def health(self) -> Mapping[str, Any]:
        return {
            "ok": True,
            "adapter_id": self.adapter_id,
            "bridge_version": BRIDGE_VERSION,
            "execution_path": self.execution_path,
        }

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise _gap("query", f"{self.adapter_id} does not represent this query")

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise _gap("action", f"{self.adapter_id} does not represent this action")

    def revision(self, _payload: Mapping[str, Any]) -> str:
        return f"native_{_digest(self._revision_state())[:20]}"

    def _revision_state(self) -> Any:
        return {"adapter": self.adapter_id, "closed": self._closed}

    def resolve_ref(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.refs.resolve(payload.get("ref"))

    def close(self) -> None:
        self._closed = True

    def ensure_open(self) -> None:
        if self._closed:
            raise BridgeError("adapter_unavailable", "native bridge is closed")


class MprisBackend(Protocol):
    def players(self) -> Sequence[Mapping[str, Any]]: ...
    def call(self, bus_name: str, interface: str, method: str, *args: Any) -> Any: ...
    def set_property(self, bus_name: str, name: str, value: Any) -> None: ...
    def track_list(self, bus_name: str) -> Sequence[Mapping[str, Any]]: ...


class SystemMprisBackend:
    """MPRIS2 through python-dbus, loaded lazily inside the Linux guest."""

    ROOT = "/org/mpris/MediaPlayer2"

    def _dbus(self):
        try:
            import dbus  # type: ignore

            return dbus
        except Exception as error:
            raise BridgeError(
                "adapter_unavailable", "python-dbus is unavailable", retryable=True
            ) from error

    def _bus(self):
        dbus = self._dbus()
        try:
            return dbus.SessionBus()
        except Exception as error:
            raise BridgeError(
                "adapter_unavailable", "MPRIS session bus is unavailable", retryable=True
            ) from error

    def _object(self, bus_name: str):
        try:
            return self._bus().get_object(bus_name, self.ROOT)
        except Exception as error:
            raise BridgeError(
                "adapter_unavailable", "MPRIS player disappeared", retryable=True
            ) from error

    def players(self) -> Sequence[Mapping[str, Any]]:
        dbus = self._dbus()
        bus = self._bus()
        try:
            names = bus.list_names()
        except Exception as error:
            raise BridgeError("adapter_unavailable", "cannot list MPRIS players") from error
        records = []
        for name in sorted(str(item) for item in names):
            if not name.startswith("org.mpris.MediaPlayer2."):
                continue
            try:
                obj = bus.get_object(name, self.ROOT)
                props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
                root = _json_value(props.GetAll("org.mpris.MediaPlayer2"))
                player = _json_value(props.GetAll("org.mpris.MediaPlayer2.Player"))
                records.append({"bus_name": name, "root": root, "player": player})
            except Exception:
                continue
        return records

    def call(self, bus_name: str, interface: str, method: str, *args: Any) -> Any:
        dbus = self._dbus()
        obj = self._object(bus_name)
        try:
            return _json_value(getattr(dbus.Interface(obj, interface), method)(*args))
        except Exception as error:
            error_name = ""
            try:
                error_name = str(error.get_dbus_name())
            except Exception:
                pass
            if error_name.endswith(("UnknownMethod", "UnknownInterface")):
                raise BridgeError(
                    "unsupported",
                    f"MPRIS method is unavailable: {interface}.{method}",
                    missing_capability=method,
                ) from error
            # A lost reply cannot establish whether the player applied the
            # method.  Never label that state as safe to replay.
            raise BridgeError(
                "uncertain",
                f"MPRIS method result is unknown: {interface}.{method}",
                side_effect_state="unknown",
            ) from error

    def set_property(self, bus_name: str, name: str, value: Any) -> None:
        dbus = self._dbus()
        obj = self._object(bus_name)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        try:
            if name == "Volume":
                value = dbus.Double(float(value), variant_level=1)
            elif name == "Shuffle":
                value = dbus.Boolean(bool(value), variant_level=1)
            elif name == "LoopStatus":
                value = dbus.String(str(value), variant_level=1)
            props.Set("org.mpris.MediaPlayer2.Player", name, value)
        except Exception as error:
            error_name = ""
            try:
                error_name = str(error.get_dbus_name())
            except Exception:
                pass
            if error_name.endswith(("UnknownProperty", "PropertyReadOnly", "UnknownInterface")):
                raise BridgeError(
                    "unsupported", f"MPRIS property is not writable: {name}"
                ) from error
            raise BridgeError(
                "uncertain",
                f"MPRIS property result is unknown: {name}",
                side_effect_state="unknown",
            ) from error

    def track_list(self, bus_name: str) -> Sequence[Mapping[str, Any]]:
        dbus = self._dbus()
        obj = self._object(bus_name)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        try:
            track_ids = list(props.Get("org.mpris.MediaPlayer2.TrackList", "Tracks"))
            iface = dbus.Interface(obj, "org.mpris.MediaPlayer2.TrackList")
            metadata = list(iface.GetTracksMetadata(track_ids))
        except Exception as error:
            raise _gap("playlist", "the active player does not expose an MPRIS track list") from error
        return [
            {"track_id": str(track), "metadata": _json_value(meta)}
            for track, meta in zip(track_ids, metadata)
        ]


class MprisBridge(NativeBridge):
    PLAYER = "org.mpris.MediaPlayer2.Player"
    TRACKLIST = "org.mpris.MediaPlayer2.TrackList"

    def __init__(
        self,
        adapter_id: str,
        *,
        backend: MprisBackend | None = None,
        vlc_only: bool = False,
        paths: GuestPathPolicy | None = None,
    ) -> None:
        super().__init__()
        self.adapter_id = adapter_id
        self.refs = RefStore(adapter_id.split("@", 1)[0].replace("-", "_"))
        self.backend = backend or SystemMprisBackend()
        self.vlc_only = vlc_only
        self.paths = paths or GuestPathPolicy()

    def _players(self) -> list[dict[str, Any]]:
        self.ensure_open()
        players = [dict(item) for item in self.backend.players()]
        if self.vlc_only:
            players = [
                item for item in players
                if "vlc" in str(item.get("bus_name", "")).casefold()
                or "vlc" in str(item.get("root", {}).get("Identity", "")).casefold()
            ]
        return players

    def health(self) -> Mapping[str, Any]:
        players = self._players()
        return {
            **super().health(),
            "player_count": len(players),
            "application_running": bool(players),
        }

    def _player_record(self, native: Mapping[str, Any]) -> dict[str, Any]:
        root = dict(native.get("root") or {})
        player = dict(native.get("player") or {})
        bus_name = str(native["bus_name"])
        ref = self.refs.put(f"player:{bus_name}", {"type": "player", "bus_name": bus_name})
        metadata = dict(player.get("Metadata") or {})
        return {
            "ref": ref,
            "kind": "media.player",
            "identity": _bounded_text(root.get("Identity") or bus_name),
            "desktop_entry": _bounded_text(root.get("DesktopEntry")),
            "playback_status": _bounded_text(player.get("PlaybackStatus")),
            "position_seconds": float(player.get("Position", 0) or 0) / 1_000_000,
            "duration_seconds": float(metadata.get("mpris:length", 0) or 0) / 1_000_000,
            "volume": float(player.get("Volume", 0) or 0),
            "loop": _bounded_text(player.get("LoopStatus")),
            "shuffle": bool(player.get("Shuffle", False)),
            "title": _bounded_text(metadata.get("xesam:title")),
            "artists": _json_value(metadata.get("xesam:artist", [])),
            "album": _bounded_text(metadata.get("xesam:album")),
            "url": _bounded_text(metadata.get("xesam:url"), 4_096),
            "track_id": _bounded_text(metadata.get("mpris:trackid"), 4_096),
            "can_seek": bool(player.get("CanSeek", False)),
            "can_control": bool(player.get("CanControl", False)),
            "advertised_actions": [
                "play", "pause", "stop", "seek", "set_volume",
                "set_loop", "set_shuffle",
            ],
            "source": "mpris_dbus",
            "freshness": "live",
        }

    def _one_player(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        target = payload.get("target") or {}
        if isinstance(target, Mapping) and target.get("ref"):
            resolved = dict(self.refs.get(target["ref"]))
            if resolved.get("type") not in {"player", "track"}:
                raise BridgeError("stale_ref", "target is not an MPRIS entity")
            bus_name = str(resolved["bus_name"])
            for native in self._players():
                if native.get("bus_name") == bus_name:
                    return native, resolved
            raise BridgeError("stale_ref", "MPRIS player is no longer available")
        players = self._players()
        if not players:
            raise BridgeError("not_found", "no matching MPRIS player is running", retryable=True)
        if len(players) > 1:
            raise BridgeError(
                "ambiguous",
                "multiple MPRIS players are running",
                candidates=[self._player_record(item) for item in players[:10]],
            )
        return players[0], {"type": "player", "bus_name": players[0]["bus_name"]}

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resource = str(payload.get("resource") or "")
        players = self._players()
        if resource in {"media.players", "media.playback", "media.current", "vlc.playback", "vlc.media"}:
            records = [self._player_record(item) for item in players]
        elif resource == "vlc.playlist":
            if not players:
                raise BridgeError("not_found", "VLC is not running", retryable=True)
            if len(players) != 1:
                raise BridgeError("ambiguous", "multiple VLC players are running")
            records = []
            bus_name = str(players[0]["bus_name"])
            for index, track in enumerate(self.backend.track_list(bus_name)):
                metadata = dict(track.get("metadata") or {})
                track_id = str(track.get("track_id") or metadata.get("mpris:trackid") or index)
                ref = self.refs.put(
                    f"track:{bus_name}:{track_id}",
                    {"type": "track", "bus_name": bus_name, "track_id": track_id},
                )
                records.append({
                    "ref": ref,
                    "kind": "vlc.playlist_entry",
                    "position": index,
                    "title": _bounded_text(metadata.get("xesam:title")),
                    "artists": _json_value(metadata.get("xesam:artist", [])),
                    "url": _bounded_text(metadata.get("xesam:url"), 4_096),
                    "advertised_actions": ["remove_playlist_entry"],
                    "source": "mpris_dbus",
                    "freshness": "live",
                })
        elif resource in {
            "vlc.audio_tracks", "vlc.subtitle_tracks", "vlc.preferences", "vlc.equalizer"
        }:
            raise _gap(resource, f"MPRIS does not expose {resource}")
        else:
            raise BridgeError("unknown_resource", f"unknown MPRIS resource: {resource}")
        return {
            "records": records[:MAX_RECORDS],
            "total": len(records),
            "truncated": len(records) > MAX_RECORDS,
            "revision": f"mpris_{_digest(records)[:20]}",
            "execution_path": "native_api",
        }

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise BridgeError("invalid_request", "action arguments must be an object")
        native, target = self._one_player(payload)
        bus_name = str(native["bus_name"])
        player_props = dict(native.get("player") or {})
        if action in {"play", "pause", "stop"}:
            self.backend.call(bus_name, self.PLAYER, action.title())
        elif action == "seek":
            position = arguments.get("position_seconds")
            if isinstance(position, bool) or not isinstance(position, (int, float)) or position < 0:
                raise BridgeError("invalid_request", "seek requires a non-negative position_seconds")
            metadata = dict(player_props.get("Metadata") or {})
            track_id = metadata.get("mpris:trackid")
            if not track_id:
                raise _gap("seek", "the active player has no current MPRIS track id")
            self.backend.call(
                bus_name, self.PLAYER, "SetPosition", track_id, int(position * 1_000_000)
            )
        elif action == "set_volume":
            volume = arguments.get("volume")
            if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 2:
                raise BridgeError("invalid_request", "volume must be between 0 and 2")
            self.backend.set_property(bus_name, "Volume", float(volume))
        elif action == "set_loop":
            modes = {"none": "None", "track": "Track", "playlist": "Playlist"}
            mode = arguments.get("mode")
            if mode not in modes:
                raise BridgeError("invalid_request", "loop mode is invalid")
            self.backend.set_property(bus_name, "LoopStatus", modes[str(mode)])
        elif action == "set_shuffle":
            enabled = arguments.get("enabled")
            if not isinstance(enabled, bool):
                raise BridgeError("invalid_request", "shuffle enabled must be boolean")
            self.backend.set_property(bus_name, "Shuffle", enabled)
        elif action == "add_playlist_entry":
            path = self.paths.resolve(arguments.get("path"), must_exist=True, directory=False)
            uri = "file://" + quote(str(path))
            self.backend.call(
                bus_name,
                self.TRACKLIST,
                "AddTrack",
                uri,
                "/org/mpris/MediaPlayer2/TrackList/NoTrack",
                False,
            )
        elif action == "remove_playlist_entry":
            if target.get("type") != "track":
                raise BridgeError("invalid_request", "remove requires a playlist-entry ref")
            self.backend.call(bus_name, self.TRACKLIST, "RemoveTrack", target["track_id"])
        elif action in {
            "reorder_playlist", "select_audio_track", "select_subtitle_track",
            "set_equalizer", "save_preferences",
        }:
            raise _gap(action, f"the native MPRIS interface does not expose {action}")
        else:
            raise BridgeError("unsupported", f"unsupported MPRIS action: {action}")
        revision = self.revision({})
        return {
            "status": "applied",
            "changed": True,
            "execution_path": "native_api",
            "revision": revision,
            "delta": {"action": action},
        }

    def _revision_state(self) -> Any:
        return [self._player_record(item) for item in self._players()]


class PdfBackend:
    def __init__(self, runner: CommandRunner = run_fixed_command) -> None:
        self.runner = runner

    def _require_ok(self, result: CommandResult, operation: str) -> bytes:
        if result.timed_out:
            raise BridgeError("timeout", f"{operation} timed out", retryable=True)
        if result.returncode != 0:
            raise BridgeError(
                "failed",
                f"{operation} failed: {_bounded_text(result.stderr.decode('utf-8', 'replace'))}",
            )
        return result.stdout

    def info(self, path: Path) -> Mapping[str, Any]:
        raw = self._require_ok(
            self.runner(["pdfinfo", "-enc", "UTF-8", str(path)], timeout=20),
            "pdfinfo",
        ).decode("utf-8", "replace")
        result: dict[str, Any] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                result[key.strip().casefold().replace(" ", "_")] = value.strip()
        if "pages" in result:
            try:
                result["pages"] = int(result["pages"])
            except ValueError:
                pass
        return result

    def text(self, path: Path, page: int | None = None) -> str:
        argv = ["pdftotext", "-enc", "UTF-8", "-layout"]
        if page is not None:
            argv.extend(["-f", str(page), "-l", str(page)])
        argv.extend([str(path), "-"])
        return self._require_ok(self.runner(argv, timeout=30), "pdftotext").decode(
            "utf-8", "replace"
        )

    def open_evince(self, path: Path, page: int | None = None) -> subprocess.Popen[bytes]:
        executable = shutil.which("evince")
        if executable is None:
            raise BridgeError("adapter_unavailable", "Evince is unavailable")
        argv = [executable]
        if page is not None:
            argv.extend([f"--page-label={page}"])
        argv.append(str(path))
        try:
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
            )
        except OSError as error:
            raise BridgeError("adapter_unavailable", "could not launch Evince") from error

    def fill_form_field(self, path: Path, name: str, value: str) -> None:
        """Update one named AcroForm field through pypdf and atomic replace."""

        try:
            from pypdf import PdfReader, PdfWriter  # type: ignore
        except Exception as error:
            raise _gap("fill_form_field", "PDF form edits require the optional pypdf writer") from error
        reader = PdfReader(str(path))
        fields = reader.get_fields() or {}
        if name not in fields:
            raise BridgeError("stale_ref", "PDF form field no longer exists")
        writer = PdfWriter()
        try:
            writer.clone_document_from_reader(reader)
            for page in writer.pages:
                writer.update_page_form_field_values(
                    page, {name: value}, auto_regenerate=False
                )
        except Exception as error:
            raise BridgeError("unsupported", "PDF writer cannot update this form field") from error
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".pdf", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                writer.write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            # Parse the staged artifact before replacing the source.
            PdfReader(str(temporary))
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class PdfEvinceBridge(NativeBridge):
    adapter_id = "pdf-evince@1"

    def __init__(
        self,
        *,
        backend: PdfBackend | None = None,
        paths: GuestPathPolicy | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or PdfBackend()
        self.paths = paths or GuestPathPolicy()
        self.active_path: Path | None = None
        self._owned_processes: list[subprocess.Popen[bytes]] = []

    def health(self) -> Mapping[str, Any]:
        available = all(shutil.which(program) is not None for program in ("pdfinfo", "pdftotext"))
        if self.backend.__class__ is PdfBackend and not available:
            raise BridgeError("adapter_unavailable", "Poppler utilities are unavailable")
        return {**super().health(), "poppler": True, "evince": shutil.which("evince") is not None}

    def _path(self, payload: Mapping[str, Any], *, required: bool = True) -> Path | None:
        scope = payload.get("scope") or {}
        parameters = payload.get("parameters") or {}
        raw = None
        if isinstance(scope, Mapping):
            raw = scope.get("path")
        if raw is None and isinstance(parameters, Mapping):
            raw = parameters.get("path")
        if raw is None:
            raw = self.active_path
        if raw is None:
            if required:
                raise BridgeError("not_found", "no PDF path or active PDF document")
            return None
        return self.paths.resolve(str(raw), must_exist=True, directory=False)

    def _document(self, path: Path) -> dict[str, Any]:
        info = dict(self.backend.info(path))
        stat = path.stat()
        ref = self.refs.put(f"document:{path}", {"type": "document", "path": str(path)})
        return {
            "ref": ref,
            "kind": "pdf.document",
            "name": path.name,
            "path": str(path),
            "page_count": int(info.get("pages", 0) or 0),
            "size": stat.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "advertised_actions": ["open", "save_copy", "export"],
            "source": "poppler_parser",
            "freshness": "live",
        }

    def _optional_pypdf(self, path: Path, resource: str) -> list[dict[str, Any]]:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as error:
            raise _gap(resource, f"{resource} requires the optional pypdf parser") from error
        reader = PdfReader(str(path))
        records: list[dict[str, Any]] = []
        if resource == "pdf.forms":
            for name, field in (reader.get_fields() or {}).items():
                ref = self.refs.put(f"form:{path}:{name}", {"type": "form", "path": str(path), "name": name})
                records.append({
                    "ref": ref, "kind": "pdf.form", "name": _bounded_text(name),
                    "value": _json_value(field.get("/V")), "field_type": _bounded_text(field.get("/FT")),
                    "advertised_actions": ["fill_form_field"], "source": "pypdf", "freshness": "artifact",
                })
        elif resource in {"pdf.links", "pdf.annotations"}:
            for page_index, page in enumerate(reader.pages):
                for index, annotation_ref in enumerate(page.get("/Annots") or []):
                    annotation = annotation_ref.get_object()
                    subtype = str(annotation.get("/Subtype") or "")
                    is_link = subtype == "/Link"
                    if (resource == "pdf.links") != is_link:
                        continue
                    uri = ""
                    action = annotation.get("/A")
                    if action:
                        uri = _bounded_text(action.get("/URI"), 4_096)
                    ref = self.refs.put(
                        f"annotation:{path}:{page_index}:{index}",
                        {"type": "annotation", "path": str(path), "page": page_index + 1},
                    )
                    records.append({
                        "ref": ref, "kind": "pdf.link" if is_link else "pdf.annotation",
                        "page": page_index + 1, "subtype": subtype.removeprefix("/"),
                        "uri": uri, "contents": _bounded_text(annotation.get("/Contents")),
                        "advertised_actions": [], "source": "pypdf", "freshness": "artifact",
                    })
        return records

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resource = str(payload.get("resource") or "")
        if resource == "pdf.documents" and self._path(payload, required=False) is None:
            manager_ref = self.refs.put("manager", {"type": "manager"})
            records = [{
                "ref": manager_ref, "kind": "pdf.manager", "name": "PDF documents",
                "advertised_actions": ["open"], "source": "poppler_parser", "freshness": "live",
            }]
        else:
            path = self._path(payload)
            assert path is not None
            document = self._document(path)
            info = dict(self.backend.info(path))
            if resource == "pdf.documents":
                records = [document]
            elif resource == "pdf.metadata":
                records = [{
                    "ref": self.refs.put(f"metadata:{path}", {"type": "metadata", "path": str(path)}),
                    "kind": "pdf.metadata", "path": str(path), "fields": _json_value(info),
                    "advertised_actions": [], "source": "poppler_parser", "freshness": "artifact",
                }]
            elif resource == "pdf.pages":
                count = int(info.get("pages", 0) or 0)
                records = [{
                    "ref": self.refs.put(f"page:{path}:{page}", {"type": "page", "path": str(path), "page": page}),
                    "kind": "pdf.page", "page": page,
                    "advertised_actions": [], "source": "poppler_parser", "freshness": "artifact",
                } for page in range(1, count + 1)]
            elif resource == "pdf.text":
                parameters = payload.get("parameters") or {}
                page = parameters.get("page") if isinstance(parameters, Mapping) else None
                pages = [int(page)] if page is not None else range(1, int(info.get("pages", 0) or 0) + 1)
                records = []
                for number in pages:
                    text = self.backend.text(path, number)
                    records.append({
                        "ref": self.refs.put(f"text:{path}:{number}", {"type": "text", "path": str(path), "page": number}),
                        "kind": "pdf.text", "page": number, "text": _bounded_text(text),
                        "advertised_actions": [], "source": "poppler_parser", "freshness": "artifact",
                    })
            elif resource in {"pdf.links", "pdf.annotations", "pdf.forms"}:
                records = self._optional_pypdf(path, resource)
            elif resource == "pdf.save_state":
                records = [{
                    "ref": document["ref"], "kind": "pdf.save_state", "path": str(path),
                    "exists": True, "sha256": document["sha256"], "size": document["size"],
                    "advertised_actions": ["save_copy", "export"],
                    "source": "filesystem", "freshness": "artifact",
                }]
            elif resource == "pdf.sections":
                raise _gap(resource, "PDF logical sections are not reliably encoded in the artifact")
            elif resource == "pdf.selection":
                raise _gap(resource, "Evince does not expose live text selection through a stable native API")
            else:
                raise BridgeError("unknown_resource", f"unknown PDF resource: {resource}")
        return {
            "records": records[:MAX_RECORDS], "total": len(records),
            "truncated": len(records) > MAX_RECORDS,
            "revision": f"pdf_{_digest(records)[:20]}", "execution_path": "native_api",
        }

    def _target_path(self, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> Path | None:
        target = payload.get("target") or {}
        if isinstance(target, Mapping) and target.get("ref"):
            resolved = self.refs.get(target["ref"])
            if isinstance(resolved, Mapping) and resolved.get("path"):
                return self.paths.resolve(resolved["path"], must_exist=True, directory=False)
        raw = arguments.get("path")
        if raw is not None:
            return self.paths.resolve(raw, must_exist=True, directory=False)
        return self.active_path

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise BridgeError("invalid_request", "action arguments must be an object")
        source = self._target_path(payload, arguments)
        delta: dict[str, Any] = {"action": action}
        if action == "open":
            source = self.paths.resolve(arguments.get("path"), must_exist=True, directory=False)
            process = self.backend.open_evince(source)
            self._owned_processes.append(process)
            self.active_path = source
            delta["path"] = str(source)
        elif action == "save_copy":
            if source is None:
                raise BridgeError("not_found", "no source PDF")
            destination = self.paths.resolve(arguments.get("path"), must_exist=False, directory=False)
            _atomic_bytes(destination, source.read_bytes())
            delta["path"] = str(destination)
        elif action == "export":
            if source is None:
                raise BridgeError("not_found", "no source PDF")
            format_name = str(arguments.get("format") or "").casefold()
            if format_name not in {"txt", "text", "plain"}:
                raise _gap("export", "strict PDF export currently supports deterministic text only")
            destination = self.paths.resolve(arguments.get("path"), must_exist=False, directory=False)
            _atomic_bytes(destination, self.backend.text(source).encode("utf-8"))
            delta["path"] = str(destination)
        elif action == "fill_form_field":
            if source is None:
                raise BridgeError("not_found", "no source PDF")
            target = payload.get("target") or {}
            resolved = self.refs.get(target.get("ref")) if isinstance(target, Mapping) else None
            if not isinstance(resolved, Mapping) or resolved.get("type") != "form":
                raise BridgeError("invalid_request", "fill_form_field requires a form-field ref")
            value = arguments.get("value")
            if not isinstance(value, str):
                raise BridgeError("invalid_request", "form value must be text")
            self.backend.fill_form_field(source, str(resolved["name"]), value)
            delta["field"] = str(resolved["name"])
        elif action in {"go_to_page", "follow_link", "add_annotation", "print"}:
            raise _gap(action, f"Evince/Poppler does not expose a truthful native {action} route")
        else:
            raise BridgeError("unsupported", f"unsupported PDF action: {action}")
        return {
            "status": "applied", "changed": True, "execution_path": "native_api",
            "revision": self.revision({}), "delta": delta,
        }

    def _revision_state(self) -> Any:
        if self.active_path and self.active_path.exists():
            stat = self.active_path.stat()
            return {"path": str(self.active_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        return {"path": None}

    def close(self) -> None:
        for process in self._owned_processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        super().close()


def _image_dimensions(path: Path) -> tuple[int, int, str]:
    with path.open("rb") as stream:
        head = stream.read(32)
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            width, height = struct.unpack(">II", head[16:24])
            return width, height, "png"
        if head[:6] in {b"GIF87a", b"GIF89a"} and len(head) >= 10:
            width, height = struct.unpack("<HH", head[6:10])
            return width, height, "gif"
        if head.startswith(b"\xff\xd8"):
            stream.seek(2)
            while True:
                marker_start = stream.read(1)
                if not marker_start:
                    break
                if marker_start != b"\xff":
                    continue
                marker = stream.read(1)
                while marker == b"\xff":
                    marker = stream.read(1)
                if marker in {b"\xd8", b"\xd9"}:
                    continue
                length_raw = stream.read(2)
                if len(length_raw) != 2:
                    break
                length = struct.unpack(">H", length_raw)[0]
                if marker and marker[0] in {
                    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                }:
                    payload = stream.read(5)
                    if len(payload) == 5:
                        height, width = struct.unpack(">HH", payload[1:5])
                        return width, height, "jpeg"
                    break
                stream.seek(max(0, length - 2), os.SEEK_CUR)
    raise _gap("media.dimensions", "file format has no deterministic built-in dimension parser")


class MediaBackend:
    def __init__(self, runner: CommandRunner = run_fixed_command) -> None:
        self.runner = runner

    def ffprobe(self, path: Path) -> Mapping[str, Any]:
        result = self.runner([
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-of", "json", str(path),
        ], timeout=30)
        if result.timed_out:
            raise BridgeError("timeout", "ffprobe timed out", retryable=True)
        if result.returncode != 0:
            raise BridgeError("unsupported", "ffprobe could not parse the media artifact")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except Exception as error:
            raise BridgeError("internal_error", "ffprobe returned invalid JSON") from error
        if not isinstance(value, Mapping):
            raise BridgeError("internal_error", "ffprobe returned a non-object")
        return value

    def exif(self, path: Path) -> Mapping[str, Any]:
        result = self.runner(["exiftool", "-json", "--", str(path)], timeout=30)
        if result.timed_out:
            raise BridgeError("timeout", "exiftool timed out", retryable=True)
        if result.returncode != 0:
            raise BridgeError("unsupported", "exiftool could not parse the artifact")
        try:
            values = json.loads(result.stdout.decode("utf-8"))
            return dict(values[0]) if isinstance(values, list) and values and isinstance(values[0], Mapping) else {}
        except Exception as error:
            raise BridgeError("internal_error", "exiftool returned invalid JSON") from error

    def ocr(self, path: Path) -> str:
        result = self.runner(["tesseract", str(path), "stdout", "--psm", "3"], timeout=60)
        if result.timed_out:
            raise BridgeError("timeout", "deterministic OCR timed out", retryable=True)
        if result.returncode != 0:
            raise BridgeError("unsupported", "deterministic OCR could not parse the artifact")
        return result.stdout.decode("utf-8", "replace")

    def edit_metadata(self, path: Path, fields: Mapping[str, Any]) -> None:
        if not fields or len(fields) > 100:
            raise BridgeError("invalid_request", "metadata fields must be a non-empty bounded object")
        arguments = ["exiftool", "-overwrite_original"]
        for name, value in fields.items():
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]{0,63}", name) is None:
                raise BridgeError("invalid_request", "metadata field name is invalid")
            if isinstance(value, (Mapping, list, tuple)):
                raise BridgeError("invalid_request", "metadata values must be scalar")
            arguments.append(f"-{name}={_bounded_text(value, 16_384)}")
        arguments.extend(["--", str(path)])
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.runner(arguments, timeout=60)
        if result.timed_out:
            raise BridgeError(
                "uncertain", "metadata edit timed out", side_effect_state="unknown"
            )
        if result.returncode != 0:
            after_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if after_hash != before_hash:
                raise BridgeError(
                    "uncertain",
                    "metadata edit failed after the artifact changed",
                    side_effect_state="unknown",
                )
            raise BridgeError(
                "postcondition_failed", "metadata edit failed", side_effect_state="none"
            )

    def transcode(self, source: Path, destination: Path) -> None:
        before_hash = (
            hashlib.sha256(destination.read_bytes()).hexdigest()
            if destination.is_file() else None
        )
        result = self.runner([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), str(destination),
        ], timeout=120)
        if result.timed_out:
            raise BridgeError("uncertain", "media conversion timed out", side_effect_state="unknown")
        if result.returncode != 0:
            after_hash = (
                hashlib.sha256(destination.read_bytes()).hexdigest()
                if destination.is_file() else None
            )
            if after_hash != before_hash:
                raise BridgeError(
                    "uncertain",
                    "media conversion failed after the destination changed",
                    side_effect_state="unknown",
                )
            raise BridgeError("postcondition_failed", "media conversion failed")


class MediaMetadataBridge(NativeBridge):
    adapter_id = "media-metadata@1"

    def __init__(
        self,
        *,
        backend: MediaBackend | None = None,
        paths: GuestPathPolicy | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or MediaBackend()
        self.paths = paths or GuestPathPolicy()
        self._known_paths: set[Path] = set()

    def _path(self, payload: Mapping[str, Any]) -> Path | None:
        scope = payload.get("scope") or {}
        parameters = payload.get("parameters") or {}
        raw = scope.get("path") if isinstance(scope, Mapping) else None
        if raw is None and isinstance(parameters, Mapping):
            raw = parameters.get("path")
        if raw is None:
            target = payload.get("target") or {}
            if isinstance(target, Mapping) and target.get("ref"):
                value = self.refs.get(target["ref"])
                raw = value.get("path") if isinstance(value, Mapping) else None
        if raw is None:
            return None
        path = self.paths.resolve(raw, must_exist=True, directory=False)
        self._known_paths.add(path)
        return path

    def _base_record(self, path: Path, kind: str) -> dict[str, Any]:
        ref = self.refs.put(f"file:{path}", {"type": "file", "path": str(path)})
        stat = path.stat()
        return {
            "ref": ref, "kind": kind, "name": path.name, "path": str(path),
            "size": stat.st_size, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "advertised_actions": ["edit_metadata", "convert", "resize", "crop", "export"],
            "source": "deterministic_artifact_parser", "freshness": "artifact",
            "visual_derivation": "deterministic",
        }

    def _pillow_analysis(self, path: Path, resource: str) -> Mapping[str, Any]:
        try:
            from PIL import Image  # type: ignore
        except Exception as error:
            raise _gap(resource, f"{resource} requires the deterministic Pillow parser") from error
        with Image.open(path) as image:
            if resource == "media.palette":
                colors = image.convert("RGB").resize((64, 64)).getcolors(64 * 64) or []
                colors.sort(reverse=True)
                return {"colors": [{"count": count, "rgb": list(rgb)} for count, rgb in colors[:32]]}
            histogram = image.convert("RGB").histogram()
            return {"channels": {"red": histogram[:256], "green": histogram[256:512], "blue": histogram[512:768]}}

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resource = str(payload.get("resource") or "")
        path = self._path(payload)
        if path is None:
            manager = self.refs.put("manager", {"type": "manager"})
            records = [{
                "ref": manager, "kind": "media.manager", "name": "Media artifacts",
                "advertised_actions": [], "source": "deterministic_artifact_parser", "freshness": "live",
            }]
        else:
            base = self._base_record(path, resource.rstrip("s"))
            if resource == "media.files":
                records = [base]
            elif resource == "media.dimensions":
                width, height, format_name = _image_dimensions(path)
                records = [{**base, "width": width, "height": height, "format": format_name}]
            elif resource in {"media.streams", "media.metadata"}:
                probe = self.backend.ffprobe(path)
                if resource == "media.streams":
                    records = []
                    for index, stream in enumerate(probe.get("streams") or []):
                        if not isinstance(stream, Mapping):
                            continue
                        ref = self.refs.put(f"stream:{path}:{index}", {"type": "stream", "path": str(path), "index": index})
                        records.append({
                            "ref": ref, "kind": "media.stream", "path": str(path), "index": index,
                            "fields": _json_value(stream), "advertised_actions": [],
                            "source": "ffprobe", "freshness": "artifact",
                        })
                else:
                    records = [{**base, "fields": _json_value(probe.get("format") or {})}]
            elif resource == "media.exif":
                records = [{**base, "fields": _json_value(self.backend.exif(path))}]
            elif resource == "media.ocr":
                records = [{**base, "text": _bounded_text(self.backend.ocr(path))}]
            elif resource in {"media.palette", "media.histogram"}:
                records = [{**base, **self._pillow_analysis(path, resource)}]
            else:
                raise BridgeError("unknown_resource", f"unknown media resource: {resource}")
        return {
            "records": records[:MAX_RECORDS], "total": len(records),
            "truncated": len(records) > MAX_RECORDS,
            "revision": f"media_{_digest(records)[:20]}", "execution_path": "native_api",
        }

    def _edit_with_pillow(self, source: Path, action: str, arguments: Mapping[str, Any]) -> None:
        try:
            from PIL import Image  # type: ignore
        except Exception as error:
            raise _gap(action, f"{action} requires deterministic Pillow support") from error
        with Image.open(source) as image:
            if action == "resize":
                width = arguments.get("width")
                height = arguments.get("height")
                if width is None and height is None:
                    raise BridgeError("invalid_request", "resize requires width or height")
                if width is None:
                    width = max(1, round(image.width * int(height) / image.height))
                if height is None:
                    height = max(1, round(image.height * int(width) / image.width))
                if not all(isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100_000 for value in (width, height)):
                    raise BridgeError("invalid_request", "resize dimensions are invalid")
                output = image.resize((width, height))
            else:
                values = tuple(arguments.get(name) for name in ("x", "y", "width", "height"))
                if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                    raise BridgeError("invalid_request", "crop requires integer x, y, width, height")
                x, y, width, height = values
                if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > image.width or y + height > image.height:
                    raise BridgeError("invalid_request", "crop rectangle is outside image bounds")
                output = image.crop((x, y, x + width, y + height))
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{source.name}.", suffix=source.suffix, dir=str(source.parent))
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                output.save(temporary, format=image.format)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, source)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise BridgeError("invalid_request", "action arguments must be an object")
        source = self._path(payload)
        if source is None:
            raise BridgeError("not_found", "media action requires a file ref")
        delta: dict[str, Any] = {"action": action, "path": str(source)}
        if action == "edit_metadata":
            fields = arguments.get("fields")
            if not isinstance(fields, Mapping):
                raise BridgeError("invalid_request", "edit_metadata requires fields")
            self.backend.edit_metadata(source, fields)
        elif action in {"convert", "export"}:
            destination = self.paths.resolve(arguments.get("path"), must_exist=False, directory=False)
            format_name = arguments.get("format")
            if not isinstance(format_name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}", format_name) is None:
                raise BridgeError("invalid_request", "media format is invalid")
            suffix = destination.suffix.casefold().removeprefix(".")
            aliases = {"jpeg": "jpg", "wave": "wav", "tiff": "tif"}
            requested = aliases.get(format_name.casefold(), format_name.casefold())
            actual = aliases.get(suffix, suffix)
            if actual and requested != actual:
                raise BridgeError(
                    "invalid_request",
                    "destination extension does not match the requested format",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.backend.transcode(source, destination)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise BridgeError("postcondition_failed", "converted artifact was not created", side_effect_state="unknown")
            self._known_paths.add(destination)
            delta["destination"] = str(destination)
        elif action in {"resize", "crop"}:
            self._edit_with_pillow(source, action, arguments)
        elif action == "save":
            return {
                "status": "no_effect", "changed": False, "execution_path": "native_api",
                "revision": self.revision({}), "delta": {"reason": "artifact has no pending live edits"},
            }
        else:
            raise BridgeError("unsupported", f"unsupported media action: {action}")
        return {
            "status": "applied", "changed": True, "execution_path": "native_api",
            "revision": self.revision({}), "delta": delta,
        }

    def _revision_state(self) -> Any:
        return [
            {"path": str(path), "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
            for path in sorted(self._known_paths) if path.exists()
        ]


class PicardGapBridge(NativeBridge):
    adapter_id = "picard-media@1"

    def health(self) -> Mapping[str, Any]:
        raise _gap(
            "picard_plugin",
            "the versioned Picard live-model plugin is not installed; deterministic media metadata remains available through media-metadata@1",
        )

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        del payload
        raise _gap("picard_plugin", "Picard live tag state is not exposed by a truthful native API")

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        del payload
        raise _gap("picard_plugin", "Picard mutations require the versioned live-model plugin")


@dataclass
class TerminalSession:
    ref: str
    cwd: Path
    created_at: str
    last_run_ref: str | None = None


class SandboxedExecutor:
    """argv-only process execution inside a networkless bubblewrap namespace."""

    def __init__(
        self,
        paths: GuestPathPolicy,
        *,
        executable: str | None = None,
    ) -> None:
        self.paths = paths
        self.executable = executable or shutil.which("bwrap") or ""

    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).is_file())

    @staticmethod
    def _validate_argv(argv: Any) -> tuple[str, ...]:
        if not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= MAX_ARGV:
            raise BridgeError("invalid_request", "exec requires a bounded non-empty argv array")
        result = []
        for item in argv:
            if not isinstance(item, str) or not item or "\x00" in item or len(item) > 64 * 1024:
                raise BridgeError("invalid_request", "argv contains an invalid value")
            result.append(item)
        return tuple(result)

    def command(self, argv: Any, cwd: Path) -> list[str]:
        values = self._validate_argv(argv)
        if not self.available():
            raise BridgeError(
                "adapter_unavailable",
                "bubblewrap is required; unsafe unsandboxed fallback is disabled",
            )
        command = [
            self.executable,
            "--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        for path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(path).exists():
                command.extend(["--ro-bind", path, path])
        for parent in ("/home", "/home/oai", "/home/user"):
            command.extend(["--dir", parent])
        for root in self.paths.roots:
            if root.exists():
                command.extend(["--bind", str(root), str(root)])
        command.extend([
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--chdir", str(cwd), "--", *values,
        ])
        return command

    def execute(
        self,
        argv: Any,
        *,
        cwd: Path,
        stdin: bytes,
        timeout: float,
    ) -> CommandResult:
        values = self._validate_argv(argv)
        command = self.command(values, cwd)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env={},
                start_new_session=True,
            )
        except OSError as error:
            raise BridgeError("adapter_unavailable", "could not start sandbox worker") from error
        timed_out = False
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            stdout, stderr = process.communicate()
        return CommandResult(
            values,
            int(process.returncode if process.returncode is not None else -1),
            stdout, stderr, time.monotonic() - started, timed_out,
        )


class TerminalProcessBridge(NativeBridge):
    adapter_id = "sandboxed-process@1"

    def __init__(
        self,
        *,
        paths: GuestPathPolicy | None = None,
        executor: SandboxedExecutor | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths or GuestPathPolicy()
        self.executor = executor or SandboxedExecutor(self.paths)
        self.sessions: dict[str, TerminalSession] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.manager_ref = self.refs.put("manager", {"type": "manager"})

    def health(self) -> Mapping[str, Any]:
        if not self.executor.available():
            raise BridgeError(
                "adapter_unavailable",
                "bubblewrap is required; unsafe unsandboxed fallback is disabled",
            )
        return {**super().health(), "sandbox": "bubblewrap", "network": "isolated"}

    def _session(self, payload: Mapping[str, Any]) -> TerminalSession:
        target = payload.get("target") or {}
        if not isinstance(target, Mapping) or not target.get("ref"):
            raise BridgeError("invalid_request", "terminal action requires a session ref")
        ref = str(target["ref"])
        if ref == self.manager_ref:
            raise BridgeError("invalid_request", "action requires a concrete terminal session")
        session = self.sessions.get(ref)
        if session is None:
            raise BridgeError("stale_ref", "terminal session no longer exists")
        return session

    @staticmethod
    def _output(data: bytes) -> tuple[str, str, bool]:
        truncated = len(data) > MAX_OUTPUT_BYTES
        if truncated:
            half = MAX_OUTPUT_BYTES // 2
            retained = data[:half] + b"\n[...output truncated...]\n" + data[-half:]
        else:
            retained = data
        return retained.decode("utf-8", "replace"), hashlib.sha256(data).hexdigest(), truncated

    def query(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        resource = str(payload.get("resource") or "")
        if resource == "terminal.sessions":
            records = [{
                "ref": self.manager_ref, "kind": "terminal.manager", "name": "Sandboxed sessions",
                "advertised_actions": ["create_session"], "source": "sandbox_worker", "freshness": "live",
            }]
            records.extend({
                "ref": session.ref, "kind": "terminal.session", "cwd": str(session.cwd),
                "created_at": session.created_at, "last_run_ref": session.last_run_ref,
                "advertised_actions": ["exec", "wait", "close_session"],
                "source": "sandbox_worker", "freshness": "live",
            } for session in self.sessions.values())
        elif resource in {"terminal.process", "terminal.output", "terminal.command_status"}:
            records = [dict(value) for value in self.runs.values()]
            if resource == "terminal.output":
                records = [{
                    key: value for key, value in record.items()
                    if key in {"ref", "kind", "stdout", "stderr", "stdout_hash", "stderr_hash", "truncated", "advertised_actions", "source", "freshness"}
                } for record in records]
            elif resource == "terminal.command_status":
                records = [{
                    key: value for key, value in record.items()
                    if key in {"ref", "kind", "argv", "cwd", "exit_status", "duration_seconds", "timed_out", "advertised_actions", "source", "freshness"}
                } for record in records]
        else:
            raise BridgeError("unknown_resource", f"unknown terminal resource: {resource}")
        return {
            "records": records[:MAX_RECORDS], "total": len(records),
            "truncated": len(records) > MAX_RECORDS,
            "revision": self.revision({}), "execution_path": "native_api",
        }

    def act(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise BridgeError("invalid_request", "action arguments must be an object")
        if any(name in arguments for name in ("command", "shell", "env")):
            raise BridgeError(
                "policy_violation", "shell strings and environment injection are unavailable"
            )
        if action == "create_session":
            target = payload.get("target") or {}
            if not isinstance(target, Mapping) or target.get("ref") != self.manager_ref:
                raise BridgeError("invalid_request", "create_session requires the manager ref")
            cwd = self.paths.resolve(arguments.get("cwd"), must_exist=True, directory=True)
            session_ref = self.refs.put(f"session:{secrets.token_urlsafe(12)}", {"type": "session"})
            session = TerminalSession(session_ref, cwd, _now())
            self.sessions[session_ref] = session
            delta: Mapping[str, Any] = {"session_ref": session_ref, "cwd": str(cwd)}
        elif action == "close_session":
            session = self._session(payload)
            self.sessions.pop(session.ref, None)
            delta = {"closed_session_ref": session.ref}
        elif action == "exec":
            session = self._session(payload)
            cwd = self.paths.resolve(arguments.get("cwd"), must_exist=True, directory=True)
            if cwd != session.cwd and session.cwd not in cwd.parents:
                raise BridgeError("permission_denied", "exec cwd is outside the owned session workspace")
            stdin_value = arguments.get("stdin", "")
            if not isinstance(stdin_value, str):
                raise BridgeError("invalid_request", "stdin must be text")
            stdin = stdin_value.encode("utf-8")
            if len(stdin) > MAX_STDIN_BYTES:
                raise BridgeError("invalid_request", "stdin exceeds the one MiB limit")
            timeout = arguments.get("timeout_seconds", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 300:
                raise BridgeError("invalid_request", "timeout_seconds is invalid")
            outcome = self.executor.execute(
                arguments.get("argv"), cwd=cwd, stdin=stdin, timeout=float(timeout)
            )
            stdout, stdout_hash, stdout_truncated = self._output(outcome.stdout)
            stderr, stderr_hash, stderr_truncated = self._output(outcome.stderr)
            run_ref = self.refs.put(f"run:{secrets.token_urlsafe(12)}", {"type": "run"})
            record = {
                "ref": run_ref, "kind": "terminal.process", "argv": list(outcome.argv),
                "cwd": str(cwd), "exit_status": outcome.returncode,
                "duration_seconds": round(outcome.duration_seconds, 6), "timed_out": outcome.timed_out,
                "stdout": stdout, "stderr": stderr, "stdout_hash": stdout_hash,
                "stderr_hash": stderr_hash, "truncated": stdout_truncated or stderr_truncated,
                "advertised_actions": ["wait"], "source": "sandbox_worker", "freshness": "live",
            }
            self.runs[run_ref] = record
            session.last_run_ref = run_ref
            if outcome.timed_out:
                raise BridgeError(
                    "timeout",
                    "sandboxed process timed out and its process group was killed",
                    retryable=False,
                    side_effect_state="unknown",
                )
            delta = record
        elif action == "wait":
            session = self._session(payload)
            if session.last_run_ref is None:
                raise BridgeError("not_found", "session has no process to wait for")
            delta = dict(self.runs[session.last_run_ref])
        elif action == "send_stdin":
            raise _gap("send_stdin", "the bounded worker exposes completed argv executions, not an interactive PTY")
        else:
            raise BridgeError("unsupported", f"unsupported terminal action: {action}")
        return {
            "status": "applied", "changed": action not in {"wait"},
            "execution_path": "native_api", "revision": self.revision({}), "delta": delta,
        }

    def _revision_state(self) -> Any:
        return {
            "sessions": [
                {"ref": ref, "cwd": str(session.cwd), "last_run": session.last_run_ref}
                for ref, session in sorted(self.sessions.items())
            ],
            "runs": [
                {"ref": ref, "exit": run["exit_status"], "stdout_hash": run["stdout_hash"], "stderr_hash": run["stderr_hash"]}
                for ref, run in sorted(self.runs.items())
            ],
        }


class NativeAppBridgeDispatcher:
    """Routes versioned guest adapter paths to isolated bridge instances."""

    _PATH = re.compile(
        r"^/v1/adapters/(?P<adapter>[A-Za-z][A-Za-z0-9._-]{0,126}@[A-Za-z0-9._-]{1,64})/"
        r"(?P<operation>health|query|act|revision|resolve-ref|close)$"
    )

    def __init__(self, bridges: Iterable[NativeBridge] | None = None) -> None:
        if bridges is None:
            bridges = (
                MprisBridge("vlc-mpris-http@1", vlc_only=True),
                MprisBridge("mpris-media@1"),
                PdfEvinceBridge(),
                TerminalProcessBridge(),
                PicardGapBridge(),
                MediaMetadataBridge(),
            )
        self.bridges: dict[str, NativeBridge] = {}
        for bridge in bridges:
            if bridge.adapter_id in self.bridges:
                raise ValueError(f"duplicate native bridge: {bridge.adapter_id}")
            self.bridges[bridge.adapter_id] = bridge

    @staticmethod
    def _success(result: Mapping[str, Any]) -> dict[str, Any]:
        return {"ok": True, "status": "ok", "result": dict(result)}

    @staticmethod
    def _failure(error: BridgeError) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "uncertain" if error.side_effect_state == "unknown" else "failed",
            "error": error.to_dict(),
        }

    @staticmethod
    def _private_query_page(
        result: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bound guest transport pages before JSON serialization.

        These offsets are server-private.  The outer semantic kernel assembles
        one revision-stable snapshot and then applies the model-visible opaque
        cursor contract.
        """

        records = result.get("records")
        if not isinstance(records, (list, tuple)) or not all(
            isinstance(record, Mapping) for record in records
        ):
            raise BridgeError("internal_error", "native query records are invalid")
        raw_offset = payload.get("internal_offset", 0)
        raw_limit = payload.get("limit", 100)
        if (
            isinstance(raw_offset, bool) or not isinstance(raw_offset, int)
            or raw_offset < 0
        ):
            raise BridgeError("invalid_request", "internal query offset is invalid")
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise BridgeError("invalid_request", "native query limit is invalid")
        limit = min(max(raw_limit, 1), 100)
        end = min(raw_offset + limit, len(records))
        page = dict(result)
        page["records"] = [dict(record) for record in records[raw_offset:end]]
        original_truncated = bool(result.get("truncated", False))
        has_more_materialized = end < len(records)
        page["total"] = int(result.get("total", len(records)))
        page["truncated"] = has_more_materialized or original_truncated
        page["next_internal_offset"] = end if has_more_materialized else None
        return page

    def dispatch(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        match = self._PATH.fullmatch(path)
        if match is None:
            return self._failure(BridgeError("not_found", "native adapter route was not found"))
        bridge = self.bridges.get(match.group("adapter"))
        if bridge is None:
            return self._failure(_gap(
                match.group("adapter"), "versioned native adapter integration is not installed"
            ))
        operation = match.group("operation")
        expected_method = "GET" if operation == "health" else "POST"
        if method.upper() != expected_method:
            return self._failure(BridgeError("invalid_request", f"{operation} requires {expected_method}"))
        body = payload or {}
        if not isinstance(body, Mapping):
            return self._failure(BridgeError("invalid_request", "native adapter payload must be an object"))
        try:
            bridge.ensure_open()
            if operation == "health":
                result = bridge.health()
            elif operation == "query":
                result = self._private_query_page(bridge.query(body), body)
            elif operation == "act":
                result = bridge.act(body)
            elif operation == "revision":
                result = {"revision": bridge.revision(body)}
            elif operation == "resolve-ref":
                result = bridge.resolve_ref(body)
            else:
                bridge.close()
                result = {"closed": True}
            return self._success(result)
        except BridgeError as error:
            return self._failure(error)
        except Exception as error:
            side_effect = "unknown" if operation == "act" else "none"
            code = "uncertain" if operation == "act" else "internal_error"
            return self._failure(BridgeError(
                code,
                f"native bridge failed: {type(error).__name__}",
                side_effect_state=side_effect,
            ))


_DEFAULT_DISPATCHER: NativeAppBridgeDispatcher | None = None
_DEFAULT_LOCK = threading.Lock()


def dispatch_native_app_request(
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Process one guest adapter request through an episode-local singleton.

    The guest daemon should call this only after its bearer-token and request
    size checks.  No credentials are accepted by or returned from this module.
    """

    global _DEFAULT_DISPATCHER
    with _DEFAULT_LOCK:
        if _DEFAULT_DISPATCHER is None:
            _DEFAULT_DISPATCHER = NativeAppBridgeDispatcher()
        dispatcher = _DEFAULT_DISPATCHER
    return dispatcher.dispatch(method, path, payload)


__all__ = [
    "BRIDGE_VERSION",
    "BridgeError",
    "CommandResult",
    "GuestPathPolicy",
    "MediaBackend",
    "MediaMetadataBridge",
    "MprisBridge",
    "NativeAppBridgeDispatcher",
    "PdfBackend",
    "PdfEvinceBridge",
    "PicardGapBridge",
    "SandboxedExecutor",
    "TerminalProcessBridge",
    "dispatch_native_app_request",
    "run_fixed_command",
]
