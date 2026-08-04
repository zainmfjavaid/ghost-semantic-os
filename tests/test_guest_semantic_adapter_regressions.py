"""Regression tests for live guest-adapter paging and settings discovery."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from guest_agent import semantic_agent


class GuestAdapterPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        semantic_agent.STATE.clear_private_snapshots()

    def tearDown(self) -> None:
        semantic_agent.STATE.clear_private_snapshots()

    def test_continuation_reuses_one_immutable_snapshot(self) -> None:
        builds = 0

        def build():
            nonlocal builds
            builds += 1
            return ([{"value": value} for value in range(5)], "revision_initial")

        base = {
            "scope": {"surface": "active"},
            "parameters": {},
            "where": {},
            "fields": [],
            "order_by": [],
            "freshness": "live",
            "limit": 2,
        }
        first = semantic_agent._private_snapshot_page(
            "ui.elements", {**base, "internal_offset": 0}, build
        )
        second = semantic_agent._private_snapshot_page(
            "ui.elements", {**base, "internal_offset": 2}, build
        )
        third = semantic_agent._private_snapshot_page(
            "ui.elements", {**base, "internal_offset": 4}, build
        )

        self.assertEqual(builds, 1)
        self.assertEqual(first["records"], [{"value": 0}, {"value": 1}])
        self.assertEqual(second["records"], [{"value": 2}, {"value": 3}])
        self.assertEqual(third["records"], [{"value": 4}])
        self.assertEqual(
            {first["revision"], second["revision"], third["revision"]},
            {"revision_initial"},
        )
        self.assertFalse(semantic_agent.STATE.private_snapshots)

    def test_missing_continuation_fails_as_revision_conflict(self) -> None:
        with self.assertRaises(semantic_agent.AgentError) as raised:
            semantic_agent._private_snapshot_page(
                "spreadsheet.cells",
                {
                    "scope": {}, "parameters": {"range": "A1:A3"},
                    "limit": 2, "internal_offset": 2,
                },
                lambda: ([{"value": 1}], "never_built"),
            )
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertTrue(raised.exception.retryable)

    def test_action_boundary_invalidates_private_snapshots(self) -> None:
        semantic_agent._private_snapshot_page(
            "ui.elements",
            {"scope": {}, "parameters": {}, "limit": 1, "internal_offset": 0},
            lambda: ([{"value": 1}, {"value": 2}], "revision_before_action"),
        )
        self.assertTrue(semantic_agent.STATE.private_snapshots)

        with self.assertRaises(semantic_agent.AgentError):
            semantic_agent.act({"action": "not_an_action", "arguments": {}})
        self.assertFalse(semantic_agent.STATE.private_snapshots)

    def test_surface_queries_do_not_walk_deep_control_subtrees(self) -> None:
        application = {
            "ref": "native-app", "kind": "ui.element", "role": "application",
            "name": "app", "state": {}, "advertised_actions": [],
        }
        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([application], "surface_revision"),
        ) as walk:
            result = semantic_agent._query_accessibility(
                "system.surfaces",
                {"scope": {}, "parameters": {}, "limit": 100, "internal_offset": 0},
            )
        walk.assert_called_once_with(max_depth=2, lightweight=True)
        self.assertEqual(result["records"], [application])

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([application], "element_revision"),
        ) as walk:
            semantic_agent._query_accessibility(
                "ui.elements",
                {"scope": {}, "parameters": {}, "limit": 100, "internal_offset": 0},
            )
        walk.assert_called_once_with(max_depth=32, lightweight=False)


class GuestActiveSurfaceQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        semantic_agent.STATE.clear_private_snapshots()
        self.wm_patcher = patch.object(
            semantic_agent, "_wm_active_window", return_value=None,
        )
        self.wm_patcher.start()

    def tearDown(self) -> None:
        self.wm_patcher.stop()
        for ref in (
            "shell-window", "mail-window", "first-window", "second-window",
        ):
            semantic_agent.STATE.refs.pop(ref, None)
        semantic_agent.STATE.clear_private_snapshots()

    @staticmethod
    def _payload(*, active_surface_only: bool = False) -> dict:
        return {
            "scope": {},
            "parameters": (
                {"active_surface_only": True} if active_surface_only else {}
            ),
            "where": {},
            "fields": [],
            "order_by": [],
            "freshness": "live",
            "limit": 100,
            "internal_offset": 0,
        }

    @staticmethod
    def _surface(
        ref: str,
        *,
        role: str = "frame",
        active: bool = False,
        focused: bool = False,
        modal: bool = False,
        name: str | None = None,
        parent_ref: str | None = None,
    ) -> dict:
        return {
            "ref": ref,
            "kind": "ui.element",
            "role": role,
            "name": name or ref,
            "state": {
                "showing": True,
                "visible": True,
                "active": active,
                "focused": focused,
                "modal": modal,
            },
            "advertised_actions": [],
            "parent_ref": parent_ref,
        }

    def test_descriptor_advertises_surface_snapshot_and_active_tree_parameter(self) -> None:
        descriptor = next(
            value
            for value in semantic_agent.CAPABILITIES
            if value["adapter_id"] == "universal-atspi@1"
        )

        self.assertIn("ui.surfaces", descriptor["resources"])
        self.assertEqual(
            descriptor["resource_actions"]["ui.surfaces"],
            ["activate_window", "dismiss", "close_window"],
        )
        self.assertEqual(
            descriptor["resource_schemas"]["ui.elements"]["properties"],
            {
                "active_surface_only": {"type": "boolean"},
                "max_records": {
                    "type": "integer", "minimum": 1,
                    "maximum": semantic_agent.MAX_RECORDS,
                },
            },
        )

    def test_ui_surfaces_is_exactly_one_shallow_walk(self) -> None:
        application = self._surface("application", role="application")
        window = self._surface("window", focused=True)
        deep_button = self._surface("button", role="button")

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([application, window, deep_button], "surface_revision"),
        ) as walk:
            result = semantic_agent._query_accessibility(
                "ui.surfaces", self._payload()
            )

        walk.assert_called_once_with(max_depth=2, lightweight=True)
        self.assertEqual(result["records"], [application, window])

    def test_active_surface_only_scopes_full_walk_to_focused_window(self) -> None:
        focused = self._surface("focused-window", focused=True)
        background = self._surface("background-window")
        native_root = object()
        full_tree = [
            self._surface("focused-window", focused=True),
            self._surface("save", role="button"),
        ]

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            side_effect=[
                ([background, focused], "shallow_revision"),
                (full_tree, "focused_revision"),
            ],
        ) as walk, patch.object(
            semantic_agent, "_resolve", return_value=native_root
        ) as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        self.assertEqual(
            walk.call_args_list,
            [
                call(max_depth=2, lightweight=True),
                call(max_depth=32, roots=[native_root]),
            ],
        )
        resolve.assert_called_once_with("focused-window")
        self.assertEqual(result["records"], full_tree)
        self.assertEqual(result["revision"], "focused_revision")

    def test_active_surface_only_forwards_bounded_record_budget(self) -> None:
        focused = self._surface("focused-window", focused=True)
        native_root = object()

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            side_effect=[
                ([focused], "shallow_revision"),
                ([focused], "focused_revision"),
            ],
        ) as walk, patch.object(
            semantic_agent, "_resolve", return_value=native_root,
        ):
            payload = self._payload(active_surface_only=True)
            payload["parameters"]["max_records"] = 1500
            result = semantic_agent._query_accessibility("ui.elements", payload)

        self.assertEqual(result["records"], [focused])
        self.assertEqual(
            walk.call_args_list,
            [
                call(max_depth=2, lightweight=True),
                call(max_depth=32, roots=[native_root], max_records=1500),
            ],
        )

    def test_active_surface_record_budget_is_validated(self) -> None:
        for invalid in (True, 0, semantic_agent.MAX_RECORDS + 1, "1500"):
            with self.subTest(invalid=invalid), patch.object(
                semantic_agent, "_walk_accessibility",
                return_value=([], "shallow_revision"),
            ):
                payload = self._payload(active_surface_only=True)
                payload["parameters"]["max_records"] = invalid
                with self.assertRaises(semantic_agent.AgentError) as caught:
                    semantic_agent._query_accessibility("ui.elements", payload)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_active_modal_dialog_wins_over_active_owner_window(self) -> None:
        owner = self._surface("owner-window", active=True)
        modal = self._surface(
            "modal-dialog", role="dialog", focused=True, modal=True
        )
        modal_root = object()
        modal_tree = [modal, self._surface("confirm", role="button")]

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            side_effect=[
                ([owner, modal], "shallow_revision"),
                (modal_tree, "modal_revision"),
            ],
        ) as walk, patch.object(
            semantic_agent, "_resolve", return_value=modal_root
        ) as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        resolve.assert_called_once_with("modal-dialog")
        self.assertEqual(
            walk.call_args_list[-1], call(max_depth=32, roots=[modal_root])
        )
        self.assertEqual(result["records"], modal_tree)

    def test_showing_nonmodal_dialog_does_not_hijack_active_window(self) -> None:
        nonmodal = self._surface("find-dialog", role="dialog", modal=False)
        owner = self._surface("document-window", active=True)
        owner_root = object()
        owner_tree = [owner, self._surface("document", role="document")]

        # Put the dialog first to prove surface order alone cannot select it.
        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            side_effect=[
                ([nonmodal, owner], "shallow_revision"),
                (owner_tree, "owner_revision"),
            ],
        ), patch.object(
            semantic_agent, "_resolve", return_value=owner_root
        ) as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        resolve.assert_called_once_with("document-window")
        self.assertEqual(result["records"], owner_tree)

    def test_ambiguous_unfocused_multiwindow_returns_no_guessed_tree(self) -> None:
        first = self._surface("first-window")
        second = self._surface("second-window")

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([first, second], "shallow_revision"),
        ) as walk, patch.object(semantic_agent, "_resolve") as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        walk.assert_called_once_with(max_depth=2, lightweight=True)
        resolve.assert_not_called()
        self.assertEqual(result["records"], [])
        self.assertEqual(result["revision"], "shallow_revision")

    def test_atspi_modal_state_is_exposed(self) -> None:
        class _StateSet:
            @staticmethod
            def contains(value):
                return value == "modal-flag"

        accessible = SimpleNamespace(getState=lambda: _StateSet())
        fake_pyatspi = SimpleNamespace(STATE_MODAL="modal-flag")

        self.assertEqual(
            semantic_agent._states(accessible, fake_pyatspi),
            {"modal": True},
        )

    def test_visible_portal_modal_remains_an_active_surface_when_not_showing(self) -> None:
        portal = self._surface(
            "portal-app", role="application", name="xdg-desktop-portal-gnome",
        )
        chooser = self._surface(
            "chooser", role="dialog", name="Open File", parent_ref="portal-app",
        )
        chooser["state"].update({
            "visible": True, "showing": False, "modal": True,
        })
        semantic_agent.STATE.refs["chooser"] = SimpleNamespace(
            get_process_id=lambda: 500,
        )

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            return_value={"pid": 500, "title": "Open File"},
        ), patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([portal, chooser], "revision"),
        ):
            result = semantic_agent._query_accessibility(
                "ui.surfaces", self._payload(),
            )

        surfaced = {record["ref"]: record for record in result["records"]}
        self.assertTrue(surfaced["chooser"]["state"]["active"])

    def test_wm_active_app_overrides_stale_shell_and_stays_private(self) -> None:
        shell_app = self._surface(
            "shell-app", role="application", name="gnome-shell",
        )
        shell = self._surface(
            "shell-window", active=True, focused=True,
            name="gnome-shell", parent_ref="shell-app",
        )
        app = self._surface(
            "mail-app", role="application", name="Thunderbird",
        )
        mail = self._surface(
            "mail-window", name="Inbox — Thunderbird", parent_ref="mail-app",
        )
        semantic_agent.STATE.refs["shell-window"] = SimpleNamespace(
            get_process_id=lambda: 100,
        )
        semantic_agent.STATE.refs["mail-window"] = SimpleNamespace(
            get_process_id=lambda: 200,
        )

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            return_value={
                "pid": 200,
                "title": "Inbox — Thunderbird",
                "class_name": "thunderbird",
                "xid": "private-window-id",
            },
        ), patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([shell_app, shell, app, mail], "surface_revision"),
        ):
            result = semantic_agent._query_accessibility(
                "ui.surfaces", self._payload()
            )

        refs = [record["ref"] for record in result["records"]]
        self.assertEqual(refs, ["mail-app", "mail-window"])
        self.assertTrue(result["records"][-1]["state"]["active"])
        self.assertTrue(result["records"][-1]["state"]["focused"])
        serialized = repr(result["records"])
        self.assertNotIn("private-window-id", serialized)
        self.assertNotIn("class_name", serialized)
        self.assertNotIn("pid", serialized)

    def test_active_tree_uses_unique_wm_match_not_stale_shell(self) -> None:
        shell = self._surface(
            "shell-window", active=True, focused=True, name="gnome-shell",
        )
        mail = self._surface("mail-window", name="Inbox — Thunderbird")
        shell_native = SimpleNamespace(get_process_id=lambda: 100)
        mail_native = SimpleNamespace(get_process_id=lambda: 200)
        semantic_agent.STATE.refs.update({
            "shell-window": shell_native,
            "mail-window": mail_native,
        })
        full_tree = [mail, self._surface("compose", role="button")]

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            return_value={"pid": 200, "title": "Inbox — Thunderbird"},
        ), patch.object(
            semantic_agent,
            "_walk_accessibility",
            side_effect=[
                ([shell, mail], "shallow_revision"),
                (full_tree, "mail_revision"),
            ],
        ) as walk, patch.object(
            semantic_agent, "_resolve", return_value=mail_native,
        ) as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        resolve.assert_called_once_with("mail-window")
        self.assertEqual(
            walk.call_args_list[-1], call(max_depth=32, roots=[mail_native])
        )
        self.assertEqual(result["records"], full_tree)

    def test_unique_wm_class_matches_app_when_window_title_changed(self) -> None:
        chrome_app = self._surface(
            "chrome-app", role="application", name="Google Chrome",
        )
        chrome = self._surface(
            "chrome-window", name="Transient network title", parent_ref="chrome-app",
        )
        writer_app = self._surface(
            "writer-app", role="application", name="soffice",
        )
        writer = self._surface(
            "writer-window", name="Untitled 1", parent_ref="writer-app",
        )
        semantic_agent.STATE.refs.update({
            "chrome-window": SimpleNamespace(get_process_id=lambda: 200),
            "writer-window": SimpleNamespace(get_process_id=lambda: 300),
        })

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            return_value={
                "pid": 999,
                "title": "A title AT-SPI has not observed yet",
                "class_name": "google-chrome",
            },
        ), patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([chrome_app, chrome, writer_app, writer], "revision"),
        ):
            result = semantic_agent._query_accessibility(
                "ui.surfaces", self._payload(),
            )

        active = [
            record for record in result["records"]
            if record.get("state", {}).get("active") is True
        ]
        self.assertEqual([record["ref"] for record in active], ["chrome-window"])

    def test_ambiguous_wm_class_does_not_guess_between_app_windows(self) -> None:
        chrome_app = self._surface(
            "chrome-app", role="application", name="Google Chrome",
        )
        first = self._surface("first-window", name="First", parent_ref="chrome-app")
        second = self._surface("second-window", name="Second", parent_ref="chrome-app")

        matched = semantic_agent._wm_surface_match(
            [first, second],
            {"pid": 999, "title": "Unknown", "class_name": "google-chrome"},
            by_ref={
                "chrome-app": chrome_app,
                "first-window": first,
                "second-window": second,
            },
        )

        self.assertIsNone(matched)

    def test_ambiguous_wm_identity_returns_no_guessed_active_tree(self) -> None:
        first = self._surface("first-window", active=True, name="Shared")
        second = self._surface("second-window", name="Shared")
        semantic_agent.STATE.refs.update({
            "first-window": SimpleNamespace(get_process_id=lambda: 300),
            "second-window": SimpleNamespace(get_process_id=lambda: 300),
        })

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            return_value={"pid": 300, "title": "Shared"},
        ), patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([first, second], "shallow_revision"),
        ) as walk, patch.object(semantic_agent, "_resolve") as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        walk.assert_called_once_with(max_depth=2, lightweight=True)
        resolve.assert_not_called()
        self.assertEqual(result["records"], [])

    def test_failed_wm_probe_ignores_shell_but_does_not_guess_between_apps(self) -> None:
        shell = self._surface(
            "shell-window", active=True, focused=True, name="gnome-shell",
        )
        first = self._surface("first-window", name="First")
        second = self._surface("second-window", name="Second")

        with patch.object(
            semantic_agent,
            "_walk_accessibility",
            return_value=([shell, first, second], "shallow_revision"),
        ) as walk, patch.object(semantic_agent, "_resolve") as resolve:
            result = semantic_agent._query_accessibility(
                "ui.elements", self._payload(active_surface_only=True)
            )

        walk.assert_called_once_with(max_depth=2, lightweight=True)
        resolve.assert_not_called()
        self.assertEqual(result["records"], [])


class GuestOSSettingsTests(unittest.TestCase):
    def test_schema_omission_lists_discoverable_gsettings_schemas(self) -> None:
        completed = {
            "argv": ["gsettings", "list-schemas"],
            "exit_code": 0,
            "stdout": "org.gnome.desktop.interface\norg.gnome.system.proxy\n",
            "stderr": "",
        }
        with patch.object(semantic_agent, "_bounded_command", return_value=completed) as run:
            result = semantic_agent._query_os(
                "os.settings",
                {"parameters": {}, "limit": 100, "internal_offset": 0},
            )

        run.assert_called_once_with(["gsettings", "list-schemas"])
        self.assertEqual(
            [record["schema"] for record in result["records"]],
            ["org.gnome.desktop.interface", "org.gnome.system.proxy"],
        )
        self.assertTrue(all(record["kind"] == "os.setting_schema" for record in result["records"]))

    def test_key_without_schema_is_rejected(self) -> None:
        with self.assertRaises(semantic_agent.AgentError) as raised:
            semantic_agent._query_os(
                "os.settings",
                {"parameters": {"key": "enabled"}, "limit": 100},
            )
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_settings_descriptor_advertises_schema_as_optional(self) -> None:
        descriptor = next(
            value
            for value in semantic_agent.CAPABILITIES
            if value["adapter_id"] == "guest-os@1"
        )
        schema = descriptor["resource_schemas"]["os.settings"]["parameters"]
        self.assertEqual(schema["schema"], "optional string")


class GuestWindowManagerProbeTests(unittest.TestCase):
    def test_unique_exact_window_identity_can_be_activated_privately(self) -> None:
        target = SimpleNamespace(
            name="Semantic Public Journey - Google Chrome",
            get_process_id=lambda: 4242,
        )

        def command(argv):
            if argv[:4] == ["xdotool", "search", "--onlyvisible", "--name"]:
                return 0, "73400327\n"
            if argv == ["xdotool", "search", "--onlyvisible", "--pid", "4242"]:
                return 0, "73400327\n"
            if argv == ["xdotool", "windowactivate", "--sync", "73400327"]:
                return 0, ""
            raise AssertionError(argv)

        with patch.object(
            semantic_agent, "_private_wm_command", side_effect=command,
        ):
            self.assertTrue(semantic_agent._wm_activate_accessible(target))

    def test_ambiguous_window_identity_is_never_activated(self) -> None:
        target = SimpleNamespace(name="Shared", get_process_id=lambda: None)
        with patch.object(
            semantic_agent,
            "_private_wm_command",
            return_value=(0, "10\n11\n"),
        ) as command:
            self.assertFalse(semantic_agent._wm_activate_accessible(target))

        self.assertEqual(command.call_count, 2)

    def test_unique_app_suffix_recovers_from_transient_native_title(self) -> None:
        target = SimpleNamespace(
            name="Semantic Public Journey - Google Chrome",
            get_process_id=lambda: 4242,
        )

        def command(argv):
            if argv[-1] == r"^Semantic\ Public\ Journey\ \-\ Google\ Chrome$":
                return 1, ""
            if argv[-1] == "Google\\ Chrome":
                return 0, "73400327\n"
            if argv == ["xdotool", "windowactivate", "--sync", "73400327"]:
                return 0, ""
            raise AssertionError(argv)

        with patch.object(
            semantic_agent, "_private_wm_command", side_effect=command,
        ):
            self.assertTrue(semantic_agent._wm_activate_accessible(target))

    def test_wmctrl_inventory_is_a_unique_activation_fallback(self) -> None:
        target = SimpleNamespace(
            name="Semantic Public Journey - Google Chrome",
            get_process_id=lambda: 4242,
        )

        def command(argv):
            if argv[0] == "xdotool":
                return 1, ""
            if argv == ["wmctrl", "-lp"]:
                return 0, (
                    "0x04400007  0 4242 guest Semantic Public Journey - Google Chrome\n"
                    "0x04600009  0 5000 guest Untitled 1 - LibreOffice Writer\n"
                )
            if argv == ["wmctrl", "-i", "-a", "0x04400007"]:
                return 0, ""
            raise AssertionError(argv)

        with patch.object(
            semantic_agent, "_private_wm_command", side_effect=command,
        ):
            self.assertTrue(semantic_agent._wm_activate_accessible(target))

    def test_nonshowing_portal_chooser_uses_verified_exact_location(self) -> None:
        target = SimpleNamespace(name="Open File")
        path = Path("/home/user/share/semantic-upload.txt")
        events = []
        registry = SimpleNamespace(
            generateKeyboardEvent=lambda *event: events.append(event) or True,
        )
        fake_atspi = SimpleNamespace(
            Registry=registry,
            MODIFIER_CONTROL=2,
            KEY_LOCKMODIFIERS=5,
            KEY_UNLOCKMODIFIERS=6,
            KEY_STRING=4,
            KEY_SYM=3,
        )

        with patch.object(
            semantic_agent,
            "_wm_active_window",
            side_effect=[
                {"pid": 500, "title": "Open File"},
                {"pid": 600, "title": "Semantic Public Journey - Google Chrome"},
            ],
        ), patch.object(
            semantic_agent, "_atspi_module", return_value=fake_atspi,
        ):
            result = semantic_agent._choose_file_path_semantic_input(target, path)

        self.assertEqual(result["execution_path"], "semantic_input")
        self.assertTrue(result["chooser_closed"])
        self.assertEqual(events, [
            (4, None, 5),
            (0, "l", 4),
            (4, None, 6),
            (0, "/home/user/share/semantic-upload.txt", 4),
            (0xFF0D, None, 3),
        ])

    def test_xprop_probe_extracts_private_active_window_identity(self) -> None:
        def command(argv):
            if argv == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
                return 0, "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3e00007\n"
            if argv[:3] == ["xprop", "-id", "0x3e00007"]:
                return 0, (
                    '_NET_WM_PID(CARDINAL) = 4242\n'
                    '_NET_WM_NAME(UTF8_STRING) = "Inbox — Thunderbird"\n'
                    'WM_CLASS(STRING) = "thunderbird", "Thunderbird"\n'
                )
            raise AssertionError(f"unexpected command: {argv!r}")

        with patch.object(
            semantic_agent, "_private_wm_command", side_effect=command,
        ):
            identity = semantic_agent._wm_active_window()

        self.assertEqual(identity, {
            "pid": 4242,
            "title": "Inbox — Thunderbird",
            "class_name": "Thunderbird",
        })

    def test_failed_wm_commands_return_no_identity(self) -> None:
        with patch.object(
            semantic_agent, "_private_wm_command", return_value=(127, ""),
        ):
            self.assertIsNone(semantic_agent._wm_active_window())


if __name__ == "__main__":
    unittest.main()
