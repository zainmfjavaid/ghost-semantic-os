from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

from guest_agent.native_app_bridges import (
    CommandResult,
    GuestPathPolicy,
    MediaBackend,
    MediaMetadataBridge,
    MprisBridge,
    NativeBridge,
    NativeAppBridgeDispatcher,
    PdfBackend,
    PdfEvinceBridge,
    PicardGapBridge,
    SandboxedExecutor,
    TerminalProcessBridge,
    run_fixed_command,
)


class FakeMprisBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.properties: list[tuple[str, str, Any]] = []
        self.status = "Paused"

    def players(self) -> Sequence[Mapping[str, Any]]:
        return [{
            "bus_name": "org.mpris.MediaPlayer2.vlc",
            "root": {"Identity": "VLC media player", "DesktopEntry": "vlc"},
            "player": {
                "PlaybackStatus": self.status,
                "Position": 3_000_000,
                "Volume": 0.5,
                "LoopStatus": "None",
                "Shuffle": False,
                "CanSeek": True,
                "CanControl": True,
                "Metadata": {
                    "mpris:trackid": "/track/1",
                    "mpris:length": 10_000_000,
                    "xesam:title": "Example",
                    "xesam:artist": ["Artist"],
                    "xesam:url": "file:///home/oai/share/example.ogg",
                },
            },
        }]

    def call(self, bus_name: str, interface: str, method: str, *args: Any) -> Any:
        self.calls.append((bus_name, interface, method, *args))
        if method == "Play":
            self.status = "Playing"
        return None

    def set_property(self, bus_name: str, name: str, value: Any) -> None:
        self.properties.append((bus_name, name, value))

    def track_list(self, bus_name: str) -> Sequence[Mapping[str, Any]]:
        self.calls.append((bus_name, "track_list"))
        return [{
            "track_id": "/track/1",
            "metadata": {"xesam:title": "Example", "xesam:artist": ["Artist"]},
        }]


class FakePdfProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True


class FakePdfBackend(PdfBackend):
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.process = FakePdfProcess()

    def info(self, path: Path) -> Mapping[str, Any]:
        return {"pages": 2, "title": "Fixture", "pdf_version": "1.7"}

    def text(self, path: Path, page: int | None = None) -> str:
        return f"page {page}" if page is not None else "all pages"

    def open_evince(self, path: Path, page: int | None = None):
        self.opened.append(path)
        return self.process


class FakeMediaBackend(MediaBackend):
    def __init__(self) -> None:
        self.edits: list[tuple[Path, Mapping[str, Any]]] = []

    def ffprobe(self, path: Path) -> Mapping[str, Any]:
        return {
            "format": {"filename": str(path), "duration": "1.0", "tags": {"artist": "A"}},
            "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
        }

    def exif(self, path: Path) -> Mapping[str, Any]:
        return {"FileType": "PNG"}

    def ocr(self, path: Path) -> str:
        return "deterministic text"

    def edit_metadata(self, path: Path, fields: Mapping[str, Any]) -> None:
        self.edits.append((path, dict(fields)))

    def transcode(self, source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())


class FakeExecutor:
    def __init__(self, *, timed_out: bool = False) -> None:
        self.timed_out = timed_out
        self.calls: list[tuple[Any, ...]] = []

    def available(self) -> bool:
        return True

    def execute(
        self,
        argv: Any,
        *,
        cwd: Path,
        stdin: bytes,
        timeout: float,
    ) -> CommandResult:
        values = tuple(argv)
        self.calls.append((values, cwd, stdin, timeout))
        return CommandResult(
            values,
            -1 if self.timed_out else 0,
            b"stdout",
            b"stderr",
            0.25,
            self.timed_out,
        )


class DispatcherContractTests(unittest.TestCase):
    def test_health_query_revision_resolve_and_close_contract(self) -> None:
        backend = FakeMprisBackend()
        bridge = MprisBridge("mpris-media@1", backend=backend)
        dispatcher = NativeAppBridgeDispatcher([bridge])
        health = dispatcher.dispatch("GET", "/v1/adapters/mpris-media@1/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["result"]["bridge_version"], "1.0.0-alpha.1")

        query = dispatcher.dispatch("POST", "/v1/adapters/mpris-media@1/query", {
            "resource": "media.players", "scope": {}, "parameters": {},
        })
        self.assertTrue(query["ok"])
        record = query["result"]["records"][0]
        self.assertNotIn("bus_name", record)
        self.assertNotIn("coordinates", record)
        self.assertEqual(record["playback_status"], "Paused")

        resolved = dispatcher.dispatch("POST", "/v1/adapters/mpris-media@1/resolve-ref", {
            "ref": record["ref"],
        })
        self.assertTrue(resolved["ok"])
        revision = dispatcher.dispatch("POST", "/v1/adapters/mpris-media@1/revision", {})
        self.assertTrue(revision["result"]["revision"].startswith("native_"))
        closed = dispatcher.dispatch("POST", "/v1/adapters/mpris-media@1/close", {})
        self.assertTrue(closed["result"]["closed"])
        unavailable = dispatcher.dispatch("GET", "/v1/adapters/mpris-media@1/health")
        self.assertEqual(unavailable["error"]["code"], "adapter_unavailable")

    def test_route_and_method_errors_are_typed_and_side_effect_free(self) -> None:
        dispatcher = NativeAppBridgeDispatcher([])
        missing = dispatcher.dispatch("GET", "/v1/adapters/nope@1/health")
        self.assertEqual(missing["error"]["code"], "representation_gap")
        self.assertEqual(missing["error"]["side_effect_state"], "none")
        wrong_method = NativeAppBridgeDispatcher([
            MprisBridge("mpris-media@1", backend=FakeMprisBackend())
        ]).dispatch("POST", "/v1/adapters/mpris-media@1/health", {})
        self.assertEqual(wrong_method["error"]["code"], "invalid_request")

    def test_picard_gap_is_explicit_and_does_not_fake_live_state(self) -> None:
        dispatcher = NativeAppBridgeDispatcher([PicardGapBridge()])
        response = dispatcher.dispatch("GET", "/v1/adapters/picard-media@1/health")
        self.assertEqual(response["error"]["code"], "representation_gap")
        self.assertEqual(response["error"]["missing_capability"], "picard_plugin")
        self.assertEqual(response["error"]["side_effect_state"], "none")

    def test_fixed_command_runner_never_uses_a_shell(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
        with mock.patch("guest_agent.native_app_bridges.subprocess.run", return_value=completed) as run:
            result = run_fixed_command(["pdfinfo", "--", "/home/oai/share/a.pdf"])
        self.assertEqual(result.stdout, b"ok")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertIs(kwargs["shell"], False)

    def test_large_native_query_is_privately_paged_before_serialization(self) -> None:
        class ManyBridge(NativeBridge):
            adapter_id = "many@1"

            def query(self, _payload):
                records = [{"ref": f"r-{index}", "kind": "item", "index": index}
                           for index in range(235)]
                return {
                    "records": records,
                    "total": len(records),
                    "truncated": False,
                    "revision": "many-1",
                }

        dispatcher = NativeAppBridgeDispatcher([ManyBridge()])
        first = dispatcher.dispatch(
            "POST", "/v1/adapters/many@1/query", {"limit": 100}
        )["result"]
        self.assertEqual(len(first["records"]), 100)
        self.assertEqual(first["next_internal_offset"], 100)
        final = dispatcher.dispatch(
            "POST", "/v1/adapters/many@1/query",
            {"limit": 100, "internal_offset": 200},
        )["result"]
        self.assertEqual(len(final["records"]), 35)
        self.assertIsNone(final["next_internal_offset"])
        self.assertFalse(final["truncated"])


class MprisBridgeTests(unittest.TestCase):
    def test_real_mpris_methods_are_used_for_play_seek_volume_loop_shuffle(self) -> None:
        backend = FakeMprisBackend()
        bridge = MprisBridge("vlc-mpris-http@1", backend=backend, vlc_only=True)
        player = bridge.query({"resource": "vlc.playback"})["records"][0]
        target = {"ref": player["ref"]}
        bridge.act({"target": target, "action": "play", "arguments": {}})
        bridge.act({"target": target, "action": "seek", "arguments": {"position_seconds": 4.5}})
        bridge.act({"target": target, "action": "set_volume", "arguments": {"volume": 0.75}})
        bridge.act({"target": target, "action": "set_loop", "arguments": {"mode": "playlist"}})
        bridge.act({"target": target, "action": "set_shuffle", "arguments": {"enabled": True}})
        methods = [call[2] for call in backend.calls if len(call) >= 3]
        self.assertIn("Play", methods)
        self.assertIn("SetPosition", methods)
        self.assertIn(("org.mpris.MediaPlayer2.vlc", "Volume", 0.75), backend.properties)
        self.assertIn(("org.mpris.MediaPlayer2.vlc", "LoopStatus", "Playlist"), backend.properties)
        self.assertIn(("org.mpris.MediaPlayer2.vlc", "Shuffle", True), backend.properties)

    def test_playlist_query_uses_tracklist_and_nonstandard_state_is_gap(self) -> None:
        bridge = MprisBridge("vlc-mpris-http@1", backend=FakeMprisBackend(), vlc_only=True)
        playlist = bridge.query({"resource": "vlc.playlist"})
        self.assertEqual(playlist["records"][0]["title"], "Example")
        with self.assertRaises(Exception) as caught:
            bridge.query({"resource": "vlc.equalizer"})
        self.assertEqual(getattr(caught.exception, "code", None), "representation_gap")


class PdfBridgeTests(unittest.TestCase):
    def test_poppler_structure_and_atomic_save_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.7\nfixture")
            bridge = PdfEvinceBridge(
                backend=FakePdfBackend(), paths=GuestPathPolicy([root])
            )
            documents = bridge.query({"resource": "pdf.documents", "scope": {"path": str(source)}})
            document = documents["records"][0]
            self.assertEqual(document["page_count"], 2)
            pages = bridge.query({"resource": "pdf.pages", "scope": {"path": str(source)}})
            self.assertEqual([record["page"] for record in pages["records"]], [1, 2])
            text = bridge.query({
                "resource": "pdf.text", "scope": {"path": str(source)},
                "parameters": {"page": 2},
            })
            self.assertEqual(text["records"][0]["text"], "page 2")

            destination = root / "copy.pdf"
            result = bridge.act({
                "target": {"ref": document["ref"]}, "action": "save_copy",
                "arguments": {"path": str(destination)},
            })
            self.assertTrue(result["changed"])
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_unrepresented_live_evince_state_and_host_paths_fail_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF")
            bridge = PdfEvinceBridge(backend=FakePdfBackend(), paths=GuestPathPolicy([root]))
            with self.assertRaises(Exception) as selection:
                bridge.query({"resource": "pdf.selection", "scope": {"path": str(source)}})
            self.assertEqual(getattr(selection.exception, "code", None), "representation_gap")
            with self.assertRaises(Exception) as host:
                bridge.query({"resource": "pdf.documents", "scope": {"path": "/Users/zain/private.pdf"}})
            self.assertEqual(getattr(host.exception, "code", None), "permission_denied")


class MediaBridgeTests(unittest.TestCase):
    def test_deterministic_image_dimensions_metadata_and_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Dimension parsing needs only a valid PNG signature and IHDR width/height.
            image = root / "fixture.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"\x00\x00\x00\x03\x00\x00\x00\x02" + b"\x00" * 8)
            backend = FakeMediaBackend()
            bridge = MediaMetadataBridge(backend=backend, paths=GuestPathPolicy([root]))
            dimensions = bridge.query({"resource": "media.dimensions", "scope": {"path": str(image)}})
            record = dimensions["records"][0]
            self.assertEqual((record["width"], record["height"]), (3, 2))
            self.assertEqual(record["visual_derivation"], "deterministic")
            ocr = bridge.query({"resource": "media.ocr", "scope": {"path": str(image)}})
            self.assertEqual(ocr["records"][0]["text"], "deterministic text")
            metadata = bridge.query({"resource": "media.metadata", "scope": {"path": str(image)}})
            self.assertEqual(metadata["records"][0]["fields"]["duration"], "1.0")

    def test_metadata_action_uses_target_ref_and_output_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            source.write_bytes(b"input")
            backend = FakeMediaBackend()
            bridge = MediaMetadataBridge(backend=backend, paths=GuestPathPolicy([root]))
            record = bridge.query({"resource": "media.files", "scope": {"path": str(source)}})["records"][0]
            bridge.act({
                "target": {"ref": record["ref"]}, "action": "edit_metadata",
                "arguments": {"fields": {"Artist": "Example"}},
            })
            self.assertEqual(backend.edits[0][1], {"Artist": "Example"})
            destination = root / "output.bin"
            converted = bridge.act({
                "target": {"ref": record["ref"]}, "action": "convert",
                "arguments": {"path": str(destination), "format": "bin"},
            })
            self.assertTrue(converted["changed"])
            self.assertEqual(destination.read_bytes(), b"input")


class TerminalBridgeTests(unittest.TestCase):
    def test_exec_is_argv_only_and_returns_bounded_hashed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor = FakeExecutor()
            bridge = TerminalProcessBridge(
                paths=GuestPathPolicy([root]), executor=executor  # type: ignore[arg-type]
            )
            manager = bridge.query({"resource": "terminal.sessions"})["records"][0]
            created = bridge.act({
                "target": {"ref": manager["ref"]}, "action": "create_session",
                "arguments": {"cwd": str(root)},
            })
            session_ref = created["delta"]["session_ref"]
            executed = bridge.act({
                "target": {"ref": session_ref}, "action": "exec",
                "arguments": {
                    "argv": ["python3", "-c", "print('literal')"],
                    "cwd": str(root), "stdin": "input", "timeout_seconds": 5,
                },
            })
            self.assertEqual(executed["delta"]["stdout"], "stdout")
            self.assertEqual(executed["delta"]["exit_status"], 0)
            self.assertEqual(executor.calls[0][0][0], "python3")
            output = bridge.query({"resource": "terminal.output"})["records"][0]
            self.assertEqual(len(output["stdout_hash"]), 64)

    def test_shell_environment_and_timeout_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = TerminalProcessBridge(
                paths=GuestPathPolicy([root]), executor=FakeExecutor(timed_out=True)  # type: ignore[arg-type]
            )
            manager = bridge.query({"resource": "terminal.sessions"})["records"][0]
            session = bridge.act({
                "target": {"ref": manager["ref"]}, "action": "create_session",
                "arguments": {"cwd": str(root)},
            })["delta"]["session_ref"]
            with self.assertRaises(Exception) as policy:
                bridge.act({
                    "target": {"ref": session}, "action": "exec",
                    "arguments": {"command": "echo hi", "cwd": str(root)},
                })
            self.assertEqual(getattr(policy.exception, "code", None), "policy_violation")
            dispatcher = NativeAppBridgeDispatcher([bridge])
            timeout = dispatcher.dispatch("POST", "/v1/adapters/sandboxed-process@1/act", {
                "target": {"ref": session}, "action": "exec",
                "arguments": {"argv": ["tool"], "cwd": str(root), "timeout_seconds": 1},
            })
            self.assertEqual(timeout["error"]["code"], "timeout")
            self.assertEqual(timeout["error"]["side_effect_state"], "unknown")
            self.assertEqual(timeout["status"], "uncertain")

    def test_bubblewrap_command_blocks_network_and_desktop_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = GuestPathPolicy([temporary])
            executor = SandboxedExecutor(paths, executable="/usr/bin/true")
            command = executor.command(["echo", "hello"], Path(temporary))
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertNotIn("--share-net", command)
            joined = " ".join(command)
            for secret in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS"):
                self.assertNotIn(secret, joined)
            self.assertEqual(command[-2:], ["echo", "hello"])


if __name__ == "__main__":
    unittest.main()
