from __future__ import annotations

import copy
import re
import unittest
from dataclasses import dataclass
from typing import Any

from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.simple_facade import (
    MAX_QUERY_CONTEXT_GROUP_SIZE,
    MAX_QUERY_PAGE_SIZE,
    MAX_OUTPUT_CHARS,
    PAGE_TEXT_CHUNK_CHARS,
    SimpleComputerFacade,
    _letters,
)


@dataclass(frozen=True)
class _ResolvedRef:
    adapter_id: str
    resource: str
    locator: dict[str, Any]


class _Raises:
    def __init__(self, exception_type: type[BaseException]) -> None:
        self.exception_type = exception_type
        self.value: BaseException

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        if kind is None:
            raise AssertionError(f"{self.exception_type.__name__} was not raised")
        if not issubclass(kind, self.exception_type):
            return False
        self.value = value
        return True


class _FakeState:
    def __init__(self) -> None:
        self.refs: dict[str, _ResolvedRef] = {}
        self.consumed_operations = 0

    def consume_operation(self) -> None:
        self.consumed_operations += 1

    def resolve_ref(self, ref: str) -> _ResolvedRef:
        try:
            return self.refs[ref]
        except KeyError as exc:  # pragma: no cover - a broken test fixture
            raise AssertionError(f"unregistered fake ref: {ref}") from exc


class _FakeRuntime:
    RESOURCES = (
        "ui.surfaces",
        "ui.elements",
        "os.windows",
        "os.applications",
        "os.dialogs",
        "os.file_choosers",
        "os.desktop_entries",
        "browser.tabs",
        "browser.elements",
        "browser.text",
        "document.sessions",
        "document.state",
        "writer.paragraphs",
        "writer.tables",
        "spreadsheet.sheets",
        "spreadsheet.selection",
        "spreadsheet.cells",
        "spreadsheet.ranges",
        "vlc.playback",
        "vlc.playlist",
        "chrome.settings",
        "chrome.extensions",
    )

    def __init__(self) -> None:
        self.state = _FakeState()
        self.observations: dict[str, list[dict[str, Any]]] = {
            resource: [] for resource in self.RESOURCES
        }
        self.action_calls: list[dict[str, Any]] = []
        self.observe_calls: list[dict[str, Any]] = []
        self._next_ref = 1

    def record(
        self,
        resource: str,
        native_ref: str,
        *,
        adapter_id: str = "fake-desktop@1",
        **fields: Any,
    ) -> dict[str, Any]:
        ref = f"opaque-{self._next_ref}"
        self._next_ref += 1
        self.state.refs[ref] = _ResolvedRef(
            adapter_id=adapter_id,
            resource=resource,
            locator={"native_ref": native_ref},
        )
        return {"ref": ref, **fields}

    def observe(
        self, resource: str, *, parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.observe_calls.append({
            "resource": resource,
            "parameters": copy.deepcopy(parameters),
        })
        return copy.deepcopy(self.observations.get(resource, []))

    def _act(
        self,
        payload: dict[str, Any],
        *,
        consume_budget: bool,
        request_id: str,
    ) -> tuple[dict[str, Any], None, dict[str, Any]]:
        self.action_calls.append({
            "payload": copy.deepcopy(payload),
            "consume_budget": consume_budget,
            "request_id": request_id,
        })
        return {"execution_path": "fake-accessibility"}, None, {}


class _Harness:
    def __init__(self) -> None:
        self.runtime = _FakeRuntime()
        self.facade = SimpleComputerFacade(self.runtime)  # type: ignore[arg-type]
        # Keep these tests model-free and isolate facade behavior from adapter
        # transport. Identity resolution and action payloads still use the fake
        # runtime exactly as the real facade does.
        self.facade._try_observe = self.runtime.observe  # type: ignore[method-assign]
        self.facade._observe = self.runtime.observe  # type: ignore[method-assign]

    def install_desktop(
        self,
        surfaces: list[dict[str, Any]],
        *,
        app: str = "Test App",
    ) -> None:
        application = self.runtime.record(
            "ui.elements",
            f"application:{app}",
            role="application",
            name=app,
            states={"visible": True},
        )
        surface_application = self.runtime.record(
            "ui.surfaces",
            f"application:{app}",
            role="application",
            name=app,
            states={"visible": True},
        )
        ui_records = [application]
        surface_records = [surface_application]
        window_records: list[dict[str, Any]] = []
        for surface in surfaces:
            key = str(surface["key"])
            active = bool(surface.get("active"))
            frame = self.runtime.record(
                "ui.elements",
                f"window:{key}",
                role=surface.get("role", "frame"),
                name=surface.get("title", key),
                parent_ref=application["ref"],
                states={
                    "visible": True,
                    "showing": True,
                    "active": active,
                    **surface.get("states", {}),
                },
            )
            ui_records.append(frame)
            surface_records.append(self.runtime.record(
                "ui.surfaces",
                f"window:{key}",
                role=surface.get("role", "frame"),
                name=surface.get("title", key),
                parent_ref=surface_application["ref"],
                states={
                    "visible": True,
                    "showing": True,
                    "active": active,
                    **surface.get("states", {}),
                },
            ))
            window_records.append(self.runtime.record(
                "os.windows",
                f"window:{key}",
                role="window",
                name=surface.get("title", key),
                states={"visible": True, "active": active},
            ))

            emitted: list[dict[str, Any]] = []
            for element_spec in surface.get("elements", []):
                element = dict(element_spec)
                native = str(element.pop("native"))
                parent_index = element.pop("parent_index", None)
                parent_ref = (
                    frame["ref"]
                    if parent_index is None
                    else emitted[int(parent_index)]["ref"]
                )
                emitted.append(self.runtime.record(
                    "ui.elements",
                    native,
                    parent_ref=parent_ref,
                    **element,
                ))
            ui_records.extend(emitted)

        self.runtime.observations.update({
            "ui.surfaces": surface_records,
            "ui.elements": ui_records,
            "os.windows": window_records,
            "os.applications": [],
            "browser.tabs": [],
            "browser.elements": [],
        })


def _element_ids(text: str) -> list[str]:
    return re.findall(r"^\s*\[([A-Z]+[1-9][0-9]*)\]", text, flags=re.MULTILINE)


def _button(native: str, name: str) -> dict[str, Any]:
    return {
        "native": native,
        "role": "button",
        "name": name,
        "advertised_actions": ["invoke"],
        "states": {"visible": True},
    }


def _textbox(native: str, name: str) -> dict[str, Any]:
    return {
        "native": native,
        "role": "text box",
        "name": name,
        "advertised_actions": ["set_text"],
        "states": {"visible": True, "editable": True},
    }


def _element_named(harness: _Harness, name: str) -> Any:
    matches = [
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.name == name
    ]
    assert len(matches) == 1, (name, [element.name for element in matches])
    return matches[0]


def _assert_compact_model_text(text: str) -> None:
    assert len(text) <= MAX_OUTPUT_CHARS
    lowered = text.casefold()
    for jargon in (
        "advertised_actions",
        "adapter_id",
        "native_ref",
        "_simple_",
        "ui.elements",
        "browser.text",
        "document.sessions",
        "writer.paragraphs",
        "spreadsheet.cells",
        "vlc.playback",
        "chrome.settings",
    ):
        assert jargon not in lowered


def test_generic_action_delta_prioritizes_a_new_active_tab_surface() -> None:
    before = {
        "active_surface": "A",
        "elements_complete": True,
        "surfaces": {
            "A": {
                "label": "Mail — Message — active",
                "app": "Mail", "title": "Message", "active": True,
                "modified": False, "modal": False, "busy": False,
            },
        },
        "elements": {},
    }
    after = {
        "active_surface": "B",
        "elements_complete": True,
        "surfaces": {
            "A": {
                "label": "Mail — Message",
                "app": "Mail", "title": "Message", "active": False,
                "modified": False, "modal": False, "busy": False,
            },
            "B": {
                "label": "Browser — Invoice — active",
                "app": "Browser", "title": "Invoice", "active": True,
                "modified": False, "modal": False, "busy": False,
            },
        },
        "elements": {},
    }

    delta = SimpleComputerFacade._action_delta(before, after)

    assert delta is not None
    assert delta.startswith("After action — now visible [B] Browser — Invoice — active")
    assert "surface [A] active=true→false" in delta
    assert "native" not in delta.casefold()
    assert "coordinate" not in delta.casefold()


def test_generic_action_delta_ignores_element_set_churn_after_filtered_read() -> None:
    before = {
        "active_surface": "A",
        "elements_complete": False,
        "surfaces": {
            "A": {
                "label": "Editor — Note — active",
                "app": "Editor", "title": "Note", "active": True,
                "modified": False, "modal": False, "busy": False,
            },
        },
        "elements": {
            "A8": {
                "surface": "A", "role": "button", "name": "Match",
                "value": "", "states": {}, "actionable": True,
            },
        },
    }
    after = {
        "active_surface": "A",
        "elements_complete": True,
        "surfaces": before["surfaces"],
        "elements": {
            **before["elements"],
            **{
                f"A{index}": {
                    "surface": "A", "role": "button", "name": f"Existing {index}",
                    "value": "", "states": {}, "actionable": True,
                }
                for index in range(20, 80)
            },
        },
    }

    assert SimpleComputerFacade._action_delta(before, after) is None


def test_surface_letter_sequence_and_public_ids_reach_a_z_aa() -> None:
    assert [_letters(index) for index in (0, 25, 26)] == ["A", "Z", "AA"]

    harness = _Harness()
    harness.install_desktop([
        {"key": f"window-{index}", "title": f"Window {index + 1}", "active": index == 0}
        for index in range(27)
    ])
    view = harness.facade.read()

    assert view["surface_count"] == 27
    assert "[A] Test App — Window 1 — active" in view["text"]
    assert "[Z] Test App — Window 26" in view["text"]
    assert "[AA] Test App — Window 27" in view["text"]
    assert re.findall(r"^\[([A-Z]+)\]", view["text"], flags=re.MULTILINE) == [
        *[_letters(index) for index in range(26)],
        "AA",
    ]


def test_active_surface_header_always_names_app_title_and_state() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "bills", "title": "Bills", "active": True}],
        app="Thunderbird",
    )

    view = harness.facade.read()

    assert view["active_surface"] == "A"
    assert "Active Surface [A] Thunderbird — Bills — active" in view["text"]
    assert "Active Surface [A]\n" not in view["text"]


def test_full_read_omits_empty_inactive_surface_sections() -> None:
    harness = _Harness()
    harness.install_desktop([
        {
            "key": "inbox",
            "title": "Inbox",
            "active": True,
            "elements": [_button("compose", "Compose")],
        },
        {"key": "archive", "title": "Archive", "active": False},
        {"key": "settings", "title": "Settings", "active": False},
    ], app="Thunderbird")

    view = harness.facade.read()

    assert "[B] Thunderbird — Archive" in view["text"]
    assert "[C] Thunderbird — Settings" in view["text"]
    assert "Surface [B]" not in view["text"]
    assert "Surface [C]" not in view["text"]
    assert "No meaningful semantic elements on this surface." not in view["text"]
    assert 'button "Compose" click' in view["text"]


def test_common_process_names_render_as_friendly_application_names() -> None:
    cases = {
        "vlc": "VLC",
        "gnome-terminal-server": "Terminal",
        "nautilus": "Files",
        "evince": "Document Viewer",
        "code": "Visual Studio Code",
        "org.gnome.Nautilus": "Files",
    }
    for process_name, friendly_name in cases.items():
        harness = _Harness()
        harness.install_desktop(
            [{"key": process_name, "title": "Current document", "active": True}],
            app=process_name,
        )
        view = harness.facade.read()
        assert f"Active Surface [A] {friendly_name} — Current document — active" in view["text"]

    harness = _Harness()
    harness.install_desktop(
        [{"key": "calc", "title": "grades.xlsx - LibreOffice Calc", "active": True}],
        app="soffice",
    )
    assert (
        "Active Surface [A] LibreOffice Calc — grades.xlsx - LibreOffice Calc — active"
        in harness.facade.read()["text"]
    )


def test_background_libreoffice_surface_uses_unique_uno_document_title() -> None:
    harness = _Harness()
    app = harness.runtime.record(
        "ui.surfaces", "application:soffice", role="application", name="soffice",
        states={"visible": True},
    )
    calc = harness.runtime.record(
        "ui.surfaces", "window:calc", role="frame", name="soffice",
        parent_ref=app["ref"], states={"visible": True, "showing": True},
    )
    writer = harness.runtime.record(
        "ui.surfaces", "window:writer", role="frame",
        name="ReferenceAnswers.docx - LibreOffice Writer", parent_ref=app["ref"],
        states={"visible": True, "showing": True, "active": True},
    )
    harness.runtime.observations["ui.surfaces"] = [app, calc, writer]
    harness.runtime.observations["ui.elements"] = []
    harness.runtime.observations["document.sessions"] = [
        harness.runtime.record(
            "document.sessions", "document:calc", document_type="calc",
            title="grades.xlsx", modified=False,
        ),
        harness.runtime.record(
            "document.sessions", "document:writer", document_type="writer",
            title="ReferenceAnswers.docx", modified=False,
        ),
    ]

    view = harness.facade.read()

    assert "LibreOffice Calc — grades.xlsx" in view["text"]
    assert "LibreOffice Writer — ReferenceAnswers.docx — active" in view["text"]
    assert "LibreOffice — soffice" not in view["text"]


def test_redundant_action_labels_and_punctuation_fragments_are_compacted() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "page",
        "title": "Example",
        "active": True,
        "elements": [
            {
                "native": "link",
                "role": "link",
                "name": "Ozempic",
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
            },
            {
                "native": "duplicate-label",
                "role": "statictext",
                "name": "Ozempic",
                "states": {"visible": True},
            },
            {
                "native": "punctuation",
                "role": "statictext",
                "name": ",",
                "states": {"visible": True},
            },
            {
                "native": "unique-copy",
                "role": "statictext",
                "name": "Unique explanatory copy",
                "states": {"visible": True},
            },
        ],
    }], app="Chrome")

    view = harness.facade.read()

    assert view["text"].count('"Ozempic"') == 1
    assert 'link "Ozempic" click' in view["text"]
    assert 'statictext ","' not in view["text"]
    assert 'statictext "Unique explanatory copy"' in view["text"]


def test_desktop_exposes_compact_queryable_application_launchers() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "desktop", "title": "@!0,0;BDHF", "active": True}],
        app="gjs",
    )
    harness.runtime.observations["os.desktop_entries"] = [
        harness.runtime.record(
            "os.desktop_entries", "desktop:settings",
            desktop_id="gnome-control-center.desktop", name="Settings", hidden=False,
            executable_template="gnome-control-center",
        ),
        harness.runtime.record(
            "os.desktop_entries", "desktop:writer",
            desktop_id="libreoffice-writer.desktop", name="LibreOffice Writer", hidden=False,
            executable_template="libreoffice --writer",
        ),
        harness.runtime.record(
            "os.desktop_entries", "desktop:blender",
            desktop_id="blender.desktop", name="Blender", hidden=False,
            executable_template="blender",
        ),
        harness.runtime.record(
            "os.desktop_entries", "desktop:hidden",
            desktop_id="hidden.desktop", name="Hidden Utility", hidden=True,
            executable_template="hidden",
        ),
    ]

    default = harness.facade.read()
    assert "Active Surface [A] Desktop — active" in default["text"]
    assert 'application "Settings" click' in default["text"]
    assert 'application "LibreOffice Writer" click' in default["text"]
    assert "Blender" not in default["text"]
    assert "gnome-control-center.desktop" not in default["text"]
    assert "executable_template" not in default["text"]

    searched = harness.facade.read(query="Blender")
    assert 'application "Blender" click' in searched["text"]
    assert "Hidden Utility" not in searched["text"]


def test_desktop_application_click_uses_native_launch_binding() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "desktop", "title": "@!0,0;BDHF", "active": True}],
        app="gjs",
    )
    harness.runtime.observations["os.desktop_entries"] = [
        harness.runtime.record(
            "os.desktop_entries", "desktop:settings",
            desktop_id="gnome-control-center.desktop", name="Settings", hidden=False,
        ),
    ]
    harness.facade.read(query="Settings")
    launcher = _element_named(harness, "Settings")
    harness.facade._wait_for_launched_surface = lambda _previous: None  # type: ignore[method-assign]

    result = harness.facade.click(launcher.public_id)

    assert result["ok"] is True
    call = harness.runtime.action_calls[-1]["payload"]
    assert call["action"] == "launch"
    assert call["arguments"] == {"desktop_id": "gnome-control-center.desktop"}
    assert call["target"] == {"ref": launcher.ref}


def test_browser_address_and_navigation_are_bound_to_active_tab() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Example", "active": True}],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs", "tab:one", adapter_id="browser.cdp@1",
        title="Example", url="https://example.com/", active=True,
        advertised_actions=[
            "switch_tab", "navigate", "back", "forward", "reload", "open_tab",
        ],
    )
    harness.runtime.observations["browser.tabs"] = [tab]

    view = harness.facade.read()
    assert 'input "Address" value="https://example.com/" type=replace' in view["text"]
    address_line = next(
        line for line in view["text"].splitlines() if 'input "Address"' in line
    )
    assert "replace-or-grid" not in address_line
    assert 'button "Back" click' in view["text"]
    assert 'button "Reload" click' in view["text"]
    address = _element_named(harness, "Address")

    harness.facade.type_text(address.public_id, "chrome://settings/privacy")

    call = harness.runtime.action_calls[-1]["payload"]
    assert call["target"] == {"ref": tab["ref"]}
    assert call["action"] == "navigate"
    assert call["arguments"] == {"url": "chrome://settings/privacy"}


def test_specialized_adapters_use_application_identity_not_browser_page_title() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{
            "key": "chrome",
            "title": "LibreOffice Writer and VLC guide",
            "active": True,
        }],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:misleading-title",
        adapter_id="browser.cdp@1",
        title="LibreOffice Writer and VLC guide",
        url="https://example.test/guide",
        active=True,
    )
    document = harness.runtime.record(
        "document.sessions",
        "document:background-writer",
        document_type="writer",
        title="Background.odt",
    )
    paragraph = harness.runtime.record(
        "writer.paragraphs",
        "paragraph:background",
        index=0,
        style="Body Text",
        text="Background Writer content must not join Chrome.",
    )
    player = harness.runtime.record(
        "vlc.playback",
        "vlc:background-player",
        identity="VLC",
        title="Background track",
        can_control=True,
        can_seek=True,
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["writer.paragraphs"] = [paragraph]
    harness.runtime.observations["vlc.playback"] = [player]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    assert "Active Surface [A] Chrome — LibreOffice Writer and VLC guide — active" in view["text"]
    assert 'input "Address" value="https://example.test/guide" type=replace' in view["text"]
    assert "Background Writer content" not in view["text"]
    assert "Background track" not in view["text"]
    observed = {call["resource"] for call in harness.runtime.observe_calls}
    assert "document.sessions" not in observed
    assert "writer.paragraphs" not in observed
    assert "vlc.playback" not in observed


def test_background_browser_is_listed_without_deep_element_probe() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "notes", "title": "Chrome migration notes", "active": True}],
        app="Notes",
    )
    harness.runtime.observations["browser.tabs"] = [harness.runtime.record(
        "browser.tabs",
        "tab:unrelated",
        adapter_id="browser.cdp@1",
        title="Unrelated browser tab",
        url="https://example.test/",
        active=True,
    )]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    assert "Active Surface [A] Notes — Chrome migration notes — active" in view["text"]
    assert "[B] Chrome — Unrelated browser tab" in view["text"]
    assert 'input "Address"' not in view["text"]
    observed = {call["resource"] for call in harness.runtime.observe_calls}
    assert "browser.tabs" in observed
    assert "browser.elements" not in observed


def test_libreoffice_identity_routes_document_without_title_keywords() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "writer", "title": "Quarterly notes", "active": True}],
        app="LibreOffice Writer",
    )
    document = harness.runtime.record(
        "document.sessions",
        "document:quarterly",
        document_type="writer",
        title="Quarterly notes",
    )
    paragraph = harness.runtime.record(
        "writer.paragraphs",
        "paragraph:quarterly",
        index=0,
        style="Body Text",
        text="Authoritative Writer content",
    )
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["writer.paragraphs"] = [paragraph]
    harness.runtime.observations["browser.tabs"] = [harness.runtime.record(
        "browser.tabs",
        "tab:unrelated",
        adapter_id="browser.cdp@1",
        title="Unrelated browser tab",
        active=True,
    )]
    harness.runtime.observations["vlc.playback"] = [harness.runtime.record(
        "vlc.playback",
        "vlc:unrelated",
        identity="VLC",
        title="Unrelated track",
        can_control=True,
    )]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    assert "Active Surface [A] LibreOffice Writer — Quarterly notes — active" in view["text"]
    assert "Authoritative Writer content" in view["text"]
    assert 'input "Address"' not in view["text"]
    assert "Unrelated track" not in view["text"]
    observed = {call["resource"] for call in harness.runtime.observe_calls}
    assert "document.sessions" in observed
    assert "writer.paragraphs" in observed
    assert "browser.tabs" in observed
    assert "vlc.playback" not in observed


def test_surface_and_element_ids_are_stable_and_qualified_per_surface() -> None:
    harness = _Harness()
    harness.install_desktop([
        {
            "key": "alpha",
            "title": "Alpha",
            "active": True,
            "elements": [_button("alpha-save", "Save")],
        },
        {
            "key": "beta",
            "title": "Beta",
            "active": False,
            "elements": [
                _button(f"beta-{index}", f"Beta action {index}")
                for index in range(1, 11)
            ],
        },
    ])
    first = harness.facade.read()
    assert _element_ids(first["text"]) == [
        "A1", *[f"B{index}" for index in range(1, 11)],
    ]

    harness.install_desktop([
        {
            "key": "alpha",
            "title": "Alpha",
            "active": False,
            "elements": [_button("alpha-save", "Save")],
        },
        {
            "key": "beta",
            "title": "Beta",
            "active": True,
            "elements": [
                _button(f"beta-{index}", f"Beta action {index}")
                for index in range(1, 11)
            ],
        },
    ])
    second = harness.facade.read()
    assert second["active_surface"] == "B"
    assert _element_ids(second["text"]) == [
        "A1", *[f"B{index}" for index in range(1, 11)],
    ]
    assert "[B10]" in second["text"]

    harness.install_desktop([
        {
            "key": "alpha",
            "title": "Alpha",
            "active": True,
            "elements": [_button("alpha-save", "Save")],
        },
        {"key": "beta", "title": "Beta", "active": False},
    ])
    third = harness.facade.read()
    assert third["active_surface"] == "A"
    assert _element_ids(third["text"]) == ["A1"]


def test_unique_unchanged_element_keeps_id_across_native_ref_churn() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button("save-old-native", "Save")],
    }])
    first = harness.facade.read()
    assert _element_ids(first["text"]) == ["A1"]
    old_element = harness.facade._current_elements["A1"]  # noqa: SLF001

    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button("save-fresh-native", "Save")],
    }])
    second = harness.facade.read()
    fresh_element = harness.facade._current_elements["A1"]  # noqa: SLF001

    assert _element_ids(second["text"]) == ["A1"]
    assert fresh_element.identity != old_element.identity
    assert fresh_element.ref != old_element.ref
    harness.facade.click("A1")
    assert harness.runtime.action_calls[-1]["payload"]["target"] == {
        "ref": fresh_element.ref,
    }


def test_duplicate_semantic_rows_never_guess_ids_across_native_ref_churn() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button("old-one", "Continue"),
            _button("old-two", "Continue"),
        ],
    }])
    assert _element_ids(harness.facade.read()["text"]) == ["A1", "A2"]

    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button("fresh-one", "Continue"),
            _button("fresh-two", "Continue"),
        ],
    }])
    second = harness.facade.read()

    assert _element_ids(second["text"]) == ["A3", "A4"]
    with _Raises(ProtocolError) as stale:
        harness.facade.click("A1")
    assert stale.value.code is ErrorCode.STALE_REF


def test_unique_surface_signature_keeps_public_id_across_fresh_native_identity() -> None:
    harness = _Harness()
    harness.install_desktop([
        {"key": "anchor", "title": "Inbox", "active": False},
        {"key": "bills-old-native", "title": "Bills", "active": True},
    ], app="Thunderbird")
    first = harness.facade.read()
    assert "[B] Thunderbird — Bills — active" in first["text"]
    old_surface = harness.facade._current_surfaces["B"]  # noqa: SLF001

    harness.install_desktop([
        {"key": "anchor", "title": "Inbox", "active": False},
        {"key": "bills-fresh-native", "title": "Bills", "active": True},
    ], app="Thunderbird")
    second = harness.facade.read()
    fresh_surface = harness.facade._current_surfaces["B"]  # noqa: SLF001

    assert second["active_surface"] == "B"
    assert set(harness.facade._current_surfaces) == {"A", "B"}  # noqa: SLF001
    assert "[B] Thunderbird — Bills — active" in second["text"]
    assert "[C] Thunderbird — Bills" not in second["text"]
    assert fresh_surface.identity != old_surface.identity
    assert fresh_surface.ref != old_surface.ref

    harness.facade.click("B")
    action = harness.runtime.action_calls[-1]["payload"]
    assert action["action"] == "activate_window"
    assert action["target"] == {"ref": fresh_surface.ref}
    assert action["target"] != {"ref": old_surface.ref}


def test_duplicate_surface_signatures_never_guess_identity_across_fresh_refs() -> None:
    harness = _Harness()
    harness.install_desktop([
        {"key": "anchor", "title": "Anchor", "active": True},
        {"key": "duplicate-old-1", "title": "Untitled", "active": False},
        {"key": "duplicate-old-2", "title": "Untitled", "active": False},
    ])
    first = harness.facade.read()
    assert "[B] Test App — Untitled" in first["text"]
    assert "[C] Test App — Untitled" in first["text"]

    harness.install_desktop([
        {"key": "anchor", "title": "Anchor", "active": True},
        {"key": "duplicate-fresh-1", "title": "Untitled", "active": False},
        {"key": "duplicate-fresh-2", "title": "Untitled", "active": False},
    ])
    second = harness.facade.read()

    assert set(harness.facade._current_surfaces) == {"A", "D", "E"}  # noqa: SLF001
    assert "[D] Test App — Untitled" in second["text"]
    assert "[E] Test App — Untitled" in second["text"]
    assert "[B] Test App — Untitled" not in second["text"]
    assert "[C] Test App — Untitled" not in second["text"]
    with _Raises(ProtocolError) as stale:
        harness.facade.click("B")
    assert stale.value.code is ErrorCode.STALE_REF


def test_cross_source_duplicate_native_surface_has_one_unique_public_id() -> None:
    harness = _Harness()
    application = harness.runtime.record(
        "os.applications",
        "application:chrome",
        role="application",
        name="google-chrome",
        states={"visible": True},
    )
    duplicate_window = harness.runtime.record(
        "os.windows",
        "window:chrome-update",
        role="window",
        name="Can't update Chrome",
        parent_ref=application["ref"],
        states={"visible": True, "showing": True, "active": True},
    )
    distinct_same_title = harness.runtime.record(
        "os.windows",
        "window:chrome-update-second",
        role="window",
        name="Can't update Chrome",
        parent_ref=application["ref"],
        states={"visible": True, "showing": True, "active": False},
    )
    duplicate_dialog = harness.runtime.record(
        "os.dialogs",
        "window:chrome-update",
        role="dialog",
        name="Can't update Chrome",
        parent_ref=application["ref"],
        states={
            "visible": True,
            "showing": True,
            "active": False,
            "modal": True,
        },
    )
    harness.runtime.observations.update({
        "os.applications": [application],
        "os.windows": [duplicate_window, distinct_same_title],
        "os.dialogs": [duplicate_dialog],
    })

    def observe_with_missing_surfaces(
        resource: str, *, parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if resource == "ui.surfaces":
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, "legacy guest")
        return harness.runtime.observe(resource, parameters=parameters)

    harness.facade._observe = observe_with_missing_surfaces  # type: ignore[method-assign]
    view = harness.facade.read()
    surfaces = list(harness.facade._current_surfaces.values())  # noqa: SLF001

    assert view["surface_count"] == 2
    assert [surface.public_id for surface in surfaces] == ["A", "B"]
    assert len({surface.public_id for surface in surfaces}) == len(surfaces)
    assert len({surface.identity for surface in surfaces}) == len(surfaces)
    assert len(re.findall(
        r"^\[A\] Chrome — Can't update Chrome", view["text"], flags=re.MULTILINE,
    )) == 1
    assert len(re.findall(
        r"^\[B\] Chrome — Can't update Chrome", view["text"], flags=re.MULTILINE,
    )) == 1
    assert harness.facade._current_surfaces["A"].modal is True  # noqa: SLF001

    harness.facade.click("A")
    assert harness.runtime.action_calls[-1]["payload"]["target"] == {
        "ref": duplicate_dialog["ref"],
    }


def test_surface_semantic_rebind_requires_the_same_role() -> None:
    harness = _Harness()
    harness.install_desktop([
        {"key": "anchor", "title": "Anchor", "active": True},
        {"key": "old-window", "title": "Details", "active": False},
    ])
    harness.facade.read()

    harness.install_desktop([
        {"key": "anchor", "title": "Anchor", "active": True},
        {
            "key": "fresh-dialog",
            "role": "dialog",
            "title": "Details",
            "active": False,
        },
    ])
    view = harness.facade.read()

    assert set(harness.facade._current_surfaces) == {"A", "C"}  # noqa: SLF001
    assert "[C] Test App — Details" in view["text"]
    assert "[B] Test App — Details" not in view["text"]


def test_known_gnome_ding_surface_renders_as_desktop_without_private_title() -> None:
    for private_title in ("@!0,0;BDHF", "@!-1920,1080;BDHF"):
        harness = _Harness()
        harness.install_desktop([{
            "key": "ding",
            "title": private_title,
            "active": True,
        }], app="gjs")

        view = harness.facade.read()
        surface = harness.facade._current_surfaces["A"]  # noqa: SLF001

        assert surface.app == "Desktop"
        assert surface.title == "Desktop"
        assert "[A] Desktop — active" in view["text"]
        assert "Active Surface [A] Desktop — active" in view["text"]
        assert "gjs" not in view["text"].casefold()
        assert "@!" not in view["text"]
        assert "BDHF" not in view["text"]

    unrelated = _Harness()
    unrelated.install_desktop([{
        "key": "other-gjs",
        "title": "@!0,0;OTHER",
        "active": True,
    }], app="gjs")
    unrelated_view = unrelated.facade.read()
    assert "[A] gjs — @!0,0;OTHER — active" in unrelated_view["text"]


def test_retired_surface_and_element_ids_are_never_reused() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "old",
        "title": "Old",
        "active": True,
        "elements": [_button("old-control", "Old control")],
    }])
    assert harness.facade.read()["active_surface"] == "A"

    harness.install_desktop([{
        "key": "old",
        "title": "Old",
        "active": True,
        "elements": [_button("replacement-control", "Replacement control")],
    }])
    replacement = harness.facade.read()
    assert _element_ids(replacement["text"]) == ["A2"]

    harness.install_desktop([{
        "key": "new",
        "title": "New",
        "active": True,
        "elements": [_button("new-control", "New control")],
    }])
    new_surface = harness.facade.read()
    assert new_surface["active_surface"] == "B"
    assert _element_ids(new_surface["text"]) == ["B1"]


def test_every_surface_contributes_elements_to_the_full_state_read() -> None:
    harness = _Harness()
    harness.install_desktop([
        {
            "key": "active",
            "title": "Active window",
            "active": True,
            "elements": [_button("active-control", "Visible active control")],
        },
        {
            "key": "background",
            "title": "Background window",
            "active": False,
            "elements": [_button("background-control", "Secret background control")],
        },
    ])

    view = harness.facade.read()

    assert view["surface_count"] == 2
    assert view["element_count"] == 2
    assert "Visible active control" in view["text"]
    assert "Secret background control" in view["text"]
    assert _element_ids(view["text"]) == ["A1", "B1"]


def test_dormant_native_controls_are_queryable_but_not_dumped_by_default() -> None:
    harness = _Harness()
    dormant = _button("closed-menu-item", "Dormant menu command")
    dormant["states"] = {"visible": True, "showing": False}
    harness.install_desktop([{
        "key": "active",
        "title": "Active window",
        "active": True,
        "elements": [
            _button("visible-control", "Visible command"),
            dormant,
        ],
    }])

    default = harness.facade.read()
    queried = harness.facade.read(query="Dormant menu command")

    assert "Visible command" in default["text"]
    assert "Dormant menu command" not in default["text"]
    assert 'button "Dormant menu command" click' in queried["text"]


def test_background_element_click_activates_then_re_resolves_exact_id() -> None:
    harness = _Harness()
    harness.install_desktop([
        {"key": "alpha", "title": "Alpha", "active": True},
        {
            "key": "beta", "title": "Beta", "active": False,
            "elements": [_button("beta-button", "Background action")],
        },
    ])
    harness.facade.read()

    harness.facade.click("B1")

    assert [
        call["payload"]["action"] for call in harness.runtime.action_calls[-2:]
    ] == ["activate_window", "invoke"]
    assert harness.runtime.action_calls[-1]["payload"]["target"] == {
        "ref": harness.facade._current_elements["B1"].ref  # noqa: SLF001
    }


def test_click_type_and_surface_activation_route_exactly_to_native_actions() -> None:
    harness = _Harness()
    harness.install_desktop([
        {
            "key": "alpha",
            "title": "Alpha",
            "active": True,
            "elements": [
                _button("save-button", "Save"),
                _textbox("subject-field", "Subject"),
            ],
        },
        {"key": "beta", "title": "Beta", "active": False},
    ])
    harness.facade.read()

    clicked = harness.facade.click("a1")
    click_call = harness.runtime.action_calls[-1]
    assert click_call["payload"]["action"] == "invoke"
    assert click_call["payload"]["target"] == {
        "ref": harness.facade._current_elements["A1"].ref  # noqa: SLF001
    }
    assert click_call["payload"]["arguments"] == {"advertised_action": "invoke"}
    assert click_call["consume_budget"] is True
    assert click_call["request_id"] == "simple-act-A1"
    assert clicked["text"].startswith("COMPUTER\n")
    assert "Clicked [" not in clicked["text"]
    assert "Execution:" not in clicked["text"]
    assert clicked["action"] == {"execution_path": "fake-accessibility"}

    typed = harness.facade.type_text("A2", "Quarterly bills")
    type_call = harness.runtime.action_calls[-1]
    assert type_call["payload"]["action"] == "set_text"
    assert type_call["payload"]["target"] == {
        "ref": harness.facade._current_elements["A2"].ref  # noqa: SLF001
    }
    assert type_call["payload"]["arguments"] == {"value": "Quarterly bills"}
    assert type_call["request_id"] == "simple-act-A2"
    assert typed["text"].startswith("COMPUTER\n")
    assert "Typed " not in typed["text"]
    assert "Execution:" not in typed["text"]
    assert typed["action"] == {"execution_path": "fake-accessibility"}

    harness.facade.click("B")
    surface_call = harness.runtime.action_calls[-1]
    assert surface_call["payload"]["action"] == "activate_window"
    assert surface_call["payload"]["target"] == {
        "ref": harness.facade._current_surfaces["B"].ref  # noqa: SLF001
    }
    assert surface_call["request_id"] == "simple-act-B"


def test_browser_tab_and_element_clicks_route_to_their_distinct_actions() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Chrome", "active": True}],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:bills",
        role="tab",
        name="Bills",
        title="Bills",
        active=True,
    )
    button = harness.runtime.record(
        "browser.elements",
        "page:pay",
        role="button",
        name="Pay bill",
        advertised_actions=["invoke"],
        states={"visible": True},
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["browser.elements"] = [button]
    view = harness.facade.read()
    assert _element_ids(view["text"]) == [f"A{index}" for index in range(1, 8)]

    harness.facade.click("A1")
    assert harness.runtime.action_calls[-1]["payload"]["action"] == "switch_tab"
    pay_button = _element_named(harness, "Pay bill")
    harness.facade.click(pay_button.public_id)
    assert harness.runtime.action_calls[-1]["payload"]["action"] == "invoke"


def test_action_returns_a_fresh_bounded_view_with_truthful_pagination() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button(f"item-{index}", f"Item {index}") for index in range(130)
        ],
    }])
    harness.facade.read()

    result = harness.facade.click("A1")

    assert isinstance(result["next_cursor"], str)
    assert result["returned_elements"] == 60
    assert result["text"].startswith("COMPUTER\n")
    assert "Execution:" not in result["text"]


def test_disappeared_refs_fail_closed_as_stale_and_are_actionable() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button("old-button", "Old button")],
    }])
    harness.facade.read()

    harness.install_desktop([{"key": "main", "title": "Main", "active": True}])
    harness.facade.read()

    with _Raises(ProtocolError) as raised:
        harness.facade.click("A1")
    assert raised.value.code is ErrorCode.STALE_REF
    assert "A1" in raised.value.message
    assert "read again" in raised.value.message.casefold()
    assert harness.runtime.action_calls == []


def test_query_within_and_cursor_are_composable_and_cursor_is_revision_bound() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            {"native": "group", "role": "group", "name": "Billing group"},
            {"native": "leaf", "role": "button", "name": "Needle child", "parent_index": 0},
            {"native": "deep", "role": "text", "text": "Deep descendant", "parent_index": 1},
            _button("sibling", "Sibling action"),
            _button("last", "Last action"),
        ],
    }])
    full = harness.facade.read()
    assert _element_ids(full["text"]) == ["A1", "A2", "A3", "A4", "A5"]

    within = harness.facade.read(within="a1")
    assert _element_ids(within["text"]) == ["A1", "A2", "A3"]
    assert "Sibling action" not in within["text"]

    queried = harness.facade.read(query="nEeDlE", within="A1")
    assert _element_ids(queried["text"]) == ["A1", "A2", "A3"]
    assert queried["element_count"] == 3

    first_page = harness.facade.read(limit=2)
    cursor = first_page["next_cursor"]
    assert isinstance(cursor, str) and cursor
    assert _element_ids(first_page["text"]) == ["A1", "A2"]
    assert f'read_computer(cursor="{cursor}")' in first_page["text"]

    second_page = harness.facade.read(cursor=cursor, limit=2)
    assert _element_ids(second_page["text"]) == ["A3", "A4"]
    reused = harness.facade.read(cursor=cursor, limit=2)
    assert _element_ids(reused["text"]) == ["A3", "A4"]


def test_query_matches_exact_substrings_and_forgiving_token_and_fields() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "writer",
        "title": "Novel notes",
        "active": True,
        "elements": [
            {
                "native": "heading",
                "role": "heading",
                "name": "Heading 2",
                "text": "Five hints which may be useful in reading a novel:",
                "states": {"visible": True},
            },
            {
                "native": "other",
                "role": "paragraph",
                "name": "Body Text",
                "text": "Five unrelated observations.",
                "states": {"visible": True},
            },
        ],
    }], app="LibreOffice Writer")

    exact = harness.facade.read(query="Five hints which may be useful")
    assert _element_ids(exact["text"]) == ["A1"]
    forgiving = harness.facade.read(
        query="heading Five hints which may be useful in reading a novel:",
    )
    assert _element_ids(forgiving["text"]) == ["A1"]
    assert "Heading 2" in forgiving["text"]
    field_hint = harness.facade.read(query="text useful novel")
    assert _element_ids(field_hint["text"]) == ["A1"]


def test_descriptive_query_ranks_partial_tokens_without_task_specific_aliases() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "mail",
        "title": "Inbox",
        "active": True,
        "elements": [
            _button("bills", "Bills"),
            _button("travel", "Travel"),
            {
                "native": "invoice",
                "role": "list item",
                "name": "Amazon Web Service invoice available",
                "text": "Your monthly invoice email",
                "states": {"visible": True},
            },
        ],
    }], app="Thunderbird")

    result = harness.facade.read(
        query="please find the Bills folder email from Amazon Web Services",
    )

    ids = _element_ids(result["text"])
    assert ids[0] == "A3"
    assert "Travel" not in result["text"]
    assert "Amazon Web Service invoice available" in result["text"]

    # Before the message list is visible, one useful token is still better
    # than returning nothing for the model's descriptive request.
    initial = _Harness()
    initial.install_desktop([{
        "key": "mail",
        "title": "Account home",
        "active": True,
        "elements": [_button("bills", "Bills"), _button("travel", "Travel")],
    }], app="Thunderbird")
    folder = initial.facade.read(
        query="Bills folder email from Amazon Web Services",
    )
    assert _element_ids(folder["text"]) == ["A1"]
    assert 'button "Bills" click' in folder["text"]


def test_exact_phrase_outranks_and_suppresses_weaker_token_only_rows() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button("exact", "Quarterly paper awards"),
            _button("paper", "Paper archive"),
            _button("awards", "Awards archive"),
        ],
    }])

    result = harness.facade.read(query="quarterly paper awards")

    assert _element_ids(result["text"]) == ["A1"]
    assert "Paper archive" not in result["text"]


def test_query_segments_identifiers_filenames_camel_case_and_numeric_suffixes() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "files",
        "title": "Documents",
        "active": True,
        "elements": [
            _button("answer", "answer_sheet0.docx"),
            _button("camel", "BillingCost2024Report.xlsx"),
            _button("other", "unrelated_notes.docx"),
        ],
    }], app="Files")

    snake = harness.facade.read(query="answer_sheet")
    assert _element_ids(snake["text"]) == ["A1"]
    assert 'button "answer_sheet0.docx" click' in snake["text"]

    camel = harness.facade.read(query="billing cost 2024 report")
    assert _element_ids(camel["text"]) == ["A2"]
    assert "BillingCost2024Report.xlsx" in camel["text"]
    assert "unrelated_notes.docx" not in camel["text"]


def test_query_restores_bounded_table_row_context_with_original_action_ids() -> None:
    harness = _Harness()
    official_pdf = _button("pdf-2022", "Official PDF")
    official_pdf["parent_index"] = 1
    harness.install_desktop([{
        "key": "browser",
        "title": "Awards",
        "active": True,
        "elements": [
            {"native": "table", "role": "table", "name": "Awards table"},
            {"native": "row-2022", "role": "row", "parent_index": 0},
            {
                "native": "title-2022", "role": "cell",
                "text": "A Structured Research Result", "parent_index": 1,
            },
            {
                "native": "year-2022", "role": "cell",
                "text": "2022", "parent_index": 1,
            },
            {
                "native": "authors-2022", "role": "cell",
                "text": "Ada Example, Lin Sample", "parent_index": 1,
            },
            official_pdf,
            {"native": "row-2021", "role": "row", "parent_index": 0},
            {
                "native": "title-2021", "role": "cell",
                "text": "Unrelated older result", "parent_index": 6,
            },
            {
                "native": "year-2021", "role": "cell",
                "text": "2021", "parent_index": 6,
            },
        ],
    }])

    harness.facade.read()
    baseline_pdf_id = _element_named(harness, "Official PDF").public_id
    result = harness.facade.read(query="2022")

    assert _element_ids(result["text"]) == ["A8", "A2", "A3", "A4", "A5"]
    assert "A Structured Research Result" in result["text"]
    assert "Ada Example, Lin Sample" in result["text"]
    assert "Official PDF" in result["text"]
    assert "Awards table" not in result["text"]
    assert "Unrelated older result" not in result["text"]
    pdf = _element_named(harness, "Official PDF")
    assert pdf.public_id == baseline_pdf_id == "A5"
    harness.facade.click(pdf.public_id)
    assert harness.runtime.action_calls[-1]["payload"]["target"] == {
        "ref": pdf.ref,
    }


def test_repeated_listitem_matches_include_local_cards_and_stay_page_bounded() -> None:
    harness = _Harness()
    elements: list[dict[str, Any]] = [
        {"native": "all-cards", "role": "group", "name": "All papers"},
    ]
    for index in range(6):
        card_index = len(elements)
        elements.extend([
            {
                "native": f"card-{index}", "role": "list item",
                "parent_index": 0,
            },
            {
                "native": f"heading-{index}", "role": "heading",
                "text": f"Relevant paper {index}", "parent_index": card_index,
            },
            {
                "native": f"date-{index}", "role": "text",
                "text": "March 1, 2024", "parent_index": card_index,
            },
            {
                "native": f"link-{index}", "role": "link",
                "name": f"Open relevant paper {index}",
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
                "parent_index": card_index,
            },
        ])
    unrelated_index = len(elements)
    elements.extend([
        {
            "native": "unrelated-card", "role": "list item",
            "parent_index": 0,
        },
        {
            "native": "unrelated-heading", "role": "heading",
            "text": "Unrelated paper", "parent_index": unrelated_index,
        },
        {
            "native": "unrelated-date", "role": "text",
            "text": "March 2, 2024", "parent_index": unrelated_index,
        },
        {
            "native": "unrelated-link", "role": "link",
            "name": "Open unrelated paper", "advertised_actions": ["invoke"],
            "states": {"visible": True}, "parent_index": unrelated_index,
        },
    ])
    harness.install_desktop([{
        "key": "browser", "title": "Daily papers", "active": True,
        "elements": elements,
    }])

    first = harness.facade.read(query="March 1, 2024")

    assert first["element_count"] == 24
    assert first["returned_elements"] == 24
    assert first["next_cursor"] is None
    assert len(first["text"]) <= MAX_OUTPUT_CHARS
    assert "Relevant paper 0" in first["text"]
    assert "Open relevant paper 0" in first["text"]
    assert "All papers" not in first["text"]
    assert "Unrelated paper" not in first["text"]
    assert "March 2, 2024" not in first["text"]
    assert any(
        "unrelated" in element.name.casefold()
        or "unrelated" in element.text.casefold()
        for element in harness.facade._current_elements.values()  # noqa: SLF001
    )


def test_query_restores_nested_generic_card_without_unrelated_sibling() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "browser",
        "title": "Research results",
        "active": True,
        "elements": [
            {"native": "results-root", "role": "generic"},
            {
                "native": "target-card", "role": "section",
                "parent_index": 0,
            },
            {
                "native": "title-wrap", "role": "generic",
                "parent_index": 1,
            },
            {
                "native": "target-title", "role": "heading",
                "text": "Bounded Semantic Interfaces", "parent_index": 2,
            },
            {
                "native": "authors-wrap", "role": "container",
                "parent_index": 1,
            },
            {
                "native": "target-authors", "role": "static text",
                "text": "Ada Example and Lin Sample", "parent_index": 4,
            },
            {
                "native": "target-date", "role": "static text",
                "text": "March 1, 2024", "parent_index": 1,
            },
            {
                "native": "target-action", "role": "link",
                "name": "Open paper", "parent_index": 1,
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
            },
            {
                "native": "other-card", "role": "section",
                "parent_index": 0,
            },
            {
                "native": "other-title", "role": "heading",
                "text": "Unrelated Visual Benchmark", "parent_index": 8,
            },
            {
                "native": "other-authors", "role": "static text",
                "text": "Someone Else", "parent_index": 8,
            },
            {
                "native": "other-action", "role": "link",
                "name": "Open unrelated paper", "parent_index": 8,
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
            },
        ],
    }])

    harness.facade.read()
    baseline_action_id = _element_named(harness, "Open paper").public_id
    result = harness.facade.read(query="Bounded Semantic Interfaces")

    assert "Bounded Semantic Interfaces" in result["text"]
    assert "Ada Example and Lin Sample" in result["text"]
    assert "March 1, 2024" in result["text"]
    assert "Open paper" in result["text"]
    assert "Unrelated Visual Benchmark" not in result["text"]
    assert "Someone Else" not in result["text"]
    assert "Open unrelated paper" not in result["text"]
    assert _element_named(harness, "Open paper").public_id == baseline_action_id
    assert result["text"].index("Bounded Semantic Interfaces") < result["text"].index(
        "Ada Example and Lin Sample"
    ) < result["text"].index("March 1, 2024") < result["text"].index("Open paper")
    assert result["element_count"] <= MAX_QUERY_CONTEXT_GROUP_SIZE


def test_query_restores_extension_style_title_description_and_action() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "browser",
        "title": "Extensions",
        "active": True,
        "elements": [
            {"native": "extension-root", "role": "generic"},
            {
                "native": "target-extension", "role": "panel",
                "parent_index": 0,
            },
            {
                "native": "target-title-wrap", "role": "generic",
                "parent_index": 1,
            },
            {
                "native": "target-title", "role": "heading",
                "text": "Writing Assistant", "parent_index": 2,
            },
            {
                "native": "target-description", "role": "static text",
                "text": "Checks spelling and grammar in text fields",
                "parent_index": 1,
            },
            {
                "native": "target-button", "role": "button",
                "name": "Add to browser", "parent_index": 1,
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
            },
            {
                "native": "other-extension", "role": "panel",
                "parent_index": 0,
            },
            {
                "native": "other-title", "role": "heading",
                "text": "Unrelated Helper", "parent_index": 6,
            },
            {
                "native": "other-description", "role": "static text",
                "text": "Changes the browser theme", "parent_index": 6,
            },
            {
                "native": "other-button", "role": "button",
                "name": "Install unrelated helper", "parent_index": 6,
                "advertised_actions": ["invoke"],
                "states": {"visible": True},
            },
        ],
    }])

    harness.facade.read()
    baseline_button_id = _element_named(harness, "Add to browser").public_id
    result = harness.facade.read(query="Checks spelling and grammar")

    assert "Writing Assistant" in result["text"]
    assert "Checks spelling and grammar in text fields" in result["text"]
    assert "Add to browser" in result["text"]
    assert "Unrelated Helper" not in result["text"]
    assert "Changes the browser theme" not in result["text"]
    assert "Install unrelated helper" not in result["text"]
    assert _element_named(harness, "Add to browser").public_id == baseline_button_id
    assert result["text"].index("Writing Assistant") < result["text"].index(
        "Checks spelling and grammar in text fields"
    ) < result["text"].index("Add to browser")
    assert result["element_count"] <= MAX_QUERY_CONTEXT_GROUP_SIZE


def test_query_ignores_prose_only_noise_and_bounds_broad_matches() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button(f"paper-{index}", f"Paper {index}") for index in range(275)
        ],
    }])

    noise = harness.facade.read(query="please can you show me the current elements")
    assert noise["element_count"] == 0
    assert noise["next_cursor"] is None

    broad = harness.facade.read(query="find the papers")
    assert broad["element_count"] == 275
    assert broad["returned_elements"] == MAX_QUERY_PAGE_SIZE
    assert broad["next_cursor"]
    assert len(broad["text"]) < 8_000

    # Intent words can also be literal UI labels and must remain searchable.
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button("search", "Search settings")],
    }])
    search = harness.facade.read(query="search")
    assert 'button "Search settings" click' in search["text"]


def test_cursor_restores_query_and_scope_and_rejects_explicit_conflicts() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            {"native": "group", "role": "group", "name": "Billing"},
            {"native": "one", "role": "button", "name": "Needle one", "parent_index": 0},
            {"native": "two", "role": "button", "name": "Needle two", "parent_index": 0},
            {"native": "three", "role": "button", "name": "Needle three", "parent_index": 0},
            _button("outside", "Needle outside"),
        ],
    }])
    harness.facade.read()

    first = harness.facade.read(query="Needle", within="A1", limit=1)
    cursor = first["next_cursor"]
    assert cursor
    assert _element_ids(first["text"]) == ["A1"]

    # The emitted continuation contains only the cursor. It must retain the
    # original query and subtree scope without asking the model to repeat them.
    second = harness.facade.read(cursor=cursor, limit=1)
    assert _element_ids(second["text"]) == ["A2"]
    assert "Needle outside" not in second["text"]

    conflicting_cursor = harness.facade.read(
        query="Needle", within="A1", limit=1,
    )["next_cursor"]
    assert conflicting_cursor
    with _Raises(ProtocolError) as conflict:
        harness.facade.read(
            cursor=conflicting_cursor, query="Different", within="A1", limit=1,
        )
    assert conflict.value.code is ErrorCode.REVISION_CONFLICT
    assert "cursor query conflicts" in conflict.value.message


def test_cursor_detects_a_changed_computer_instead_of_silently_repaging() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button(f"item-{index}", f"Item {index}") for index in range(3)],
    }])
    cursor = harness.facade.read(limit=1)["next_cursor"]
    assert cursor

    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            _button("item-0", "Renamed item"),
            _button("item-1", "Item 1"),
            _button("item-2", "Item 2"),
        ],
    }])
    with _Raises(ProtocolError) as changed:
        harness.facade.read(cursor=cursor, limit=1)
    assert changed.value.code is ErrorCode.REVISION_CONFLICT
    assert "computer changed" in changed.value.message.casefold()
    assert "fresh read" in changed.value.message.casefold()


def test_cursor_revision_includes_surface_inventory() -> None:
    harness = _Harness()
    elements = [_button(f"item-{index}", f"Item {index}") for index in range(3)]
    harness.install_desktop([{
        "key": "main", "title": "Main", "active": True, "elements": elements,
    }])
    cursor = harness.facade.read(limit=1)["next_cursor"]
    assert cursor

    harness.install_desktop([
        {"key": "main", "title": "Main", "active": True, "elements": elements},
        {"key": "background", "title": "Background", "active": False},
    ])
    with _Raises(ProtocolError) as changed:
        harness.facade.read(cursor=cursor, limit=1)

    assert changed.value.code is ErrorCode.REVISION_CONFLICT
    assert "computer changed" in changed.value.message.casefold()


def test_read_limit_and_rendered_output_are_hard_bounded() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button(f"item-{index}", f"Item {index}") for index in range(250)],
    }])
    default_page = harness.facade.read()
    assert default_page["returned_elements"] == 60
    assert isinstance(default_page["next_cursor"], str)

    max_page = harness.facade.read(limit=50_000)
    assert max_page["returned_elements"] == 100
    assert isinstance(max_page["next_cursor"], str)
    assert len(max_page["text"]) <= MAX_OUTPUT_CHARS

    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [{
            "native": f"long-{index}",
            "role": "button",
            "name": "N" * 500,
            "text": f"T{index}" * 400,
            "description": "D" * 500,
            "advertised_actions": ["invoke"],
        } for index in range(80)],
    }])
    bounded = harness.facade.read(limit=5_000)
    assert len(bounded["text"]) <= MAX_OUTPUT_CHARS
    assert bounded["returned_elements"] < 80
    assert bounded["next_cursor"]
    assert "more elements" in bounded["text"]
    assert "read_computer(cursor=" in bounded["text"]


def test_click_and_type_rendered_outputs_obey_the_same_hard_bound() -> None:
    harness = _Harness()
    long_elements = [{
        "native": f"long-{index}",
        "role": "text box" if index == 0 else "button",
        "name": "N" * 500,
        "text": f"T{index}" * 400,
        "description": "D" * 500,
        "advertised_actions": ["invoke", "set_text"] if index == 0 else ["invoke"],
        "states": {"editable": index == 0},
    } for index in range(40)]
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": long_elements,
    }])
    harness.facade.read(limit=200)

    clicked = harness.facade.click("A1")
    typed = harness.facade.type_text("A1", "hello")
    lengths = {"click": len(clicked["text"]), "type": len(typed["text"])}
    assert all(length <= MAX_OUTPUT_CHARS for length in lengths.values()), lengths


def test_rendered_language_does_not_expose_internal_completion_or_verify_tools() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [_button("save", "Save"), _textbox("subject", "Subject")],
    }])

    rendered = "\n".join((
        harness.facade.read()["text"],
        harness.facade.click("A1")["text"],
        harness.facade.type_text("A2", "hello")["text"],
    )).casefold()
    assert "task_complete" not in rendered
    assert "task complete" not in rendered
    assert "verify" not in rendered


def test_failures_are_specific_clear_and_recoverable() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [{"native": "label", "role": "label", "name": "Read only label"}],
    }])
    harness.facade.read()

    with _Raises(ProtocolError) as malformed:
        harness.facade.click("definitely-not-an-id")
    assert malformed.value.code is ErrorCode.INVALID_REQUEST
    assert "surface letter like B" in malformed.value.message
    assert "element ID like B10" in malformed.value.message

    with _Raises(ProtocolError) as unknown:
        harness.facade.click("A99")
    assert unknown.value.code is ErrorCode.STALE_REF
    assert "A99" in unknown.value.message
    assert "read again" in unknown.value.message.casefold()

    with _Raises(ProtocolError) as not_clickable:
        harness.facade.click("A1")
    assert not_clickable.value.code is ErrorCode.UNSUPPORTED
    assert "A1" in not_clickable.value.message
    assert "readable but not clickable" in not_clickable.value.message

    with _Raises(ProtocolError) as not_editable:
        harness.facade.type_text("A1", "hello")
    assert not_editable.value.code is ErrorCode.UNSUPPORTED
    assert "A1" in not_editable.value.message
    assert "not editable" in not_editable.value.message

    with _Raises(ProtocolError) as bad_within:
        harness.facade.read(within="A")
    assert bad_within.value.code is ErrorCode.INVALID_REQUEST
    assert "current element ID on any listed surface" in bad_within.value.message

    harness.facade._current_surfaces["A"].ref = None  # noqa: SLF001
    with _Raises(ProtocolError) as unactivatable:
        harness.facade.click("A")
    assert unactivatable.value.code is ErrorCode.UNSUPPORTED
    assert "surface A cannot be activated" in unactivatable.value.message

    with _Raises(ProtocolError) as surface_type:
        harness.facade.type_text("A", "hello")
    assert surface_type.value.code is ErrorCode.UNSUPPORTED
    assert "type requires an editable element ID" in surface_type.value.message


def test_ui_surfaces_uses_one_surface_walk_and_skips_legacy_fallback_queries() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "mail",
        "title": "Inbox",
        "active": True,
        "elements": [_button("compose", "Compose")],
    }], app="Thunderbird")
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert resources.count("ui.surfaces") == 1
    assert not {"os.applications", "os.windows", "os.dialogs"}.intersection(resources)
    assert {
        "resource": "ui.elements",
        "parameters": {"active_surface_only": True, "max_records": 1500},
    } in harness.runtime.observe_calls
    assert "browser.tabs" in resources
    assert "browser.elements" not in resources
    assert "Active Surface [A] Thunderbird — Inbox — active" in view["text"]
    _assert_compact_model_text(view["text"])


def test_supported_empty_ui_surfaces_does_not_trigger_legacy_queries() -> None:
    harness = _Harness()
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert resources.count("ui.surfaces") == 1
    assert not {"os.applications", "os.windows", "os.dialogs"}.intersection(resources)
    assert resources.count("browser.tabs") == 1
    assert resources.count("browser.elements") == 0
    assert view["surface_count"] == 0
    assert view["active_surface"] is None


def test_unavailable_ui_surfaces_still_uses_legacy_queries() -> None:
    harness = _Harness()
    window = harness.runtime.record(
        "os.windows",
        "legacy:window",
        role="window",
        name="Legacy window",
        states={"visible": True, "showing": True, "active": True},
    )
    harness.runtime.observations["os.windows"] = [window]
    harness.runtime.observe_calls.clear()

    def observe_with_missing_surfaces(
        resource: str, *, parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        records = harness.runtime.observe(resource, parameters=parameters)
        if resource == "ui.surfaces":
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, "old guest bundle")
        return records

    harness.facade._observe = observe_with_missing_surfaces  # type: ignore[method-assign]
    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert resources.count("ui.surfaces") == 1
    assert {"os.applications", "os.windows", "os.dialogs"}.issubset(resources)
    assert "browser.tabs" in resources
    assert "browser.elements" not in resources
    assert view["surface_count"] == 1
    assert "Legacy window" in view["text"]


def test_irrelevant_false_states_are_omitted_without_hiding_real_false_controls() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [
            {
                "native": "plain-label",
                "role": "label",
                "name": "Status",
                "states": {
                    "checked": False,
                    "expanded": False,
                    "enabled": True,
                    "required": False,
                    "invalid": False,
                },
            },
            {
                "native": "real-checkbox",
                "role": "check box",
                "name": "Remember me",
                "states": {"checked": False},
                "advertised_actions": ["toggle"],
            },
        ],
    }])

    view = harness.facade.read()
    status_line = next(line for line in view["text"].splitlines() if '"Status"' in line)
    checkbox_line = next(line for line in view["text"].splitlines() if '"Remember me"' in line)
    assert "checked=false" not in status_line
    assert "expanded=false" not in status_line
    assert "disabled" not in status_line
    assert "required" not in status_line
    assert "invalid" not in status_line
    assert "checked=false" in checkbox_line
    _assert_compact_model_text(view["text"])


def test_action_shape_is_honest_for_native_action_and_state_combinations() -> None:
    shape = SimpleComputerFacade._action_shape

    assert shape({
        "role": "button", "advertised_actions": ["scroll_into_view"],
    }, "ui.elements")[:3] == (None, {}, None)
    assert shape({
        "role": "tree item", "states": {"expanded": False},
        "advertised_actions": ["expand", "collapse"],
    }, "ui.elements")[0] == "expand"
    assert shape({
        "role": "tree item", "states": {"expanded": True},
        "advertised_actions": ["expand", "collapse"],
    }, "ui.elements")[0] == "collapse"
    assert shape({
        "role": "spin button", "value": {"current": 3, "minimum": 0},
    }, "ui.elements")[2] == "set_value"
    assert shape({
        "role": "combobox", "advertised_actions": ["select_option"],
    }, "ui.elements")[2] == "select_option"
    assert shape({
        "role": "entry", "states": {"editable": True},
        "advertised_actions": [],
    }, "ui.elements")[2] is None


def test_cross_source_editable_duplicates_keep_only_executable_type_capabilities() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "chrome", "title": "Search", "active": True, "elements": [{
            "native": "chrome-toolbar-search",
            "role": "entry",
            "name": "Shared search",
            # Descriptive AT-SPI state without a proved EditableText interface.
            "states": {"visible": True, "editable": True},
            "advertised_actions": [],
        }],
    }], app="Chrome")
    tab = harness.runtime.record(
        "browser.tabs", "tab:search", adapter_id="browser.cdp@1",
        title="Search", url="https://example.test/search", active=True,
    )
    browser_input = harness.runtime.record(
        "browser.elements", "page:search", adapter_id="browser.cdp@1",
        role="textbox", name="Shared search", states={"editable": True},
        advertised_actions=["set_text", "scroll_into_view"],
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["browser.elements"] = [browser_input]

    view = harness.facade.read()
    matches = [
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.name == "Shared search"
    ]
    assert len(matches) == 2
    page = next(element for element in matches if element.resource == "browser.elements")
    toolbar = next(element for element in matches if element.resource == "ui.elements")
    page_line = next(
        line for line in view["text"].splitlines()
        if f"[{page.public_id}]" in line
    )
    toolbar_line = next(
        line for line in view["text"].splitlines()
        if f"[{toolbar.public_id}]" in line
    )
    assert "type=replace" in page_line
    assert "type=" not in toolbar_line

    before_calls = len(harness.runtime.action_calls)
    with _Raises(ProtocolError) as unsupported:
        harness.facade.type_text(toolbar.public_id, "cannot be dispatched")
    assert unsupported.value.code is ErrorCode.UNSUPPORTED
    assert len(harness.runtime.action_calls) == before_calls

    harness.facade.type_text(page.public_id, "proved interface")
    assert harness.runtime.action_calls[-1]["payload"]["action"] == "set_text"


def test_disabled_and_read_only_state_wins_over_virtual_action_overrides() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main", "title": "Main", "active": True, "elements": [
            {
                "native": "disabled-button", "role": "button", "name": "Disabled",
                "states": {"enabled": False}, "advertised_actions": ["invoke"],
                "_simple_click_action": "invoke",
            },
            {
                "native": "disabled-text", "role": "textbox", "name": "Disabled text",
                "states": {"enabled": False, "editable": True},
                "_simple_type_action": "set_text",
            },
            {
                "native": "readonly-text", "role": "textbox", "name": "Read only",
                "states": {"read_only": True, "editable": True},
                "advertised_actions": ["invoke"], "_simple_type_action": "set_text",
            },
        ],
    }])

    text = harness.facade.read()["text"]
    disabled_button = next(line for line in text.splitlines() if '"Disabled"' in line)
    disabled_text = next(line for line in text.splitlines() if '"Disabled text"' in line)
    readonly_text = next(line for line in text.splitlines() if '"Read only"' in line)
    assert " click" not in disabled_button
    assert "type=" not in disabled_text
    assert "type=" not in readonly_text

    disabled_button_element = _element_named(harness, "Disabled")
    disabled_text_element = _element_named(harness, "Disabled text")
    before_calls = len(harness.runtime.action_calls)
    with _Raises(ProtocolError) as click_error:
        harness.facade.click(disabled_button_element.public_id)
    with _Raises(ProtocolError) as type_error:
        harness.facade.type_text(disabled_text_element.public_id, "must not apply")
    assert click_error.value.code == ErrorCode.UNSUPPORTED
    assert type_error.value.code == ErrorCode.UNSUPPORTED
    assert len(harness.runtime.action_calls) == before_calls


def test_type_dispatches_numeric_value_and_combobox_option_exactly() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main", "title": "Main", "active": True, "elements": [
            {
                "native": "quantity", "role": "spin button", "name": "Quantity",
                "value": {"current": 3, "minimum": 0, "maximum": 100},
            },
            {
                "native": "country", "role": "combobox", "name": "Country",
                "advertised_actions": ["select_option"],
            },
        ],
    }])

    harness.facade.read()
    quantity = _element_named(harness, "Quantity")
    country = _element_named(harness, "Country")
    harness.facade.type_text(quantity.public_id, "42")
    value_call = harness.runtime.action_calls[-1]["payload"]
    assert value_call["action"] == "set_value"
    assert value_call["arguments"] == {"value": 42}

    harness.facade.read()
    country = _element_named(harness, "Country")
    harness.facade.type_text(country.public_id, "Canada")
    option_call = harness.runtime.action_calls[-1]["payload"]
    assert option_call["action"] == "select_option"
    assert option_call["arguments"] == {"value": "Canada"}


def test_same_source_duplicate_siblings_survive_but_browser_wins_cross_source_duplicate() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "chrome",
        "title": "Chrome",
        "active": True,
        "elements": [
            _button("native-repeat-1", "Repeated"),
            _button("native-repeat-2", "Repeated"),
            _button("native-pay", "Pay bill"),
        ],
    }], app="Chrome")
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:bills",
        title="Bills",
        url="https://example.test/bills",
        active=True,
    )
    browser_pay = harness.runtime.record(
        "browser.elements",
        "browser-pay",
        role="button",
        name="Pay bill",
        advertised_actions=["invoke"],
        states={"visible": True},
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["browser.elements"] = [browser_pay]

    view = harness.facade.read()

    repeated = [
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.name == "Repeated"
    ]
    pay = [
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.name == "Pay bill"
    ]
    assert len(repeated) == 2
    assert all(element.resource == "ui.elements" for element in repeated)
    assert len({element.public_id for element in repeated}) == 2
    assert len(pay) == 1
    assert pay[0].resource == "browser.elements"
    assert pay[0].ref == browser_pay["ref"]
    assert view["text"].count('button "Repeated"') == 2
    assert view["text"].count('button "Pay bill"') == 1
    _assert_compact_model_text(view["text"])


def test_browser_tabs_are_titled_and_clickable_instead_of_anonymous_rows() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Chrome", "active": True}],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:quarterly-bills",
        title="Quarterly bills",
        url="https://example.test/bills/quarterly",
        active=True,
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert resources[0] == "ui.surfaces"
    assert resources.count("browser.tabs") == 1
    assert resources.count("browser.elements") == 1
    assert resources.index("ui.surfaces") < resources.index("browser.tabs")
    tab_element = _element_named(harness, "Quarterly bills")
    assert tab_element.role == "tab"
    assert tab_element.click_action == "switch_tab"
    assert 'tab "Quarterly bills"' in view["text"]
    assert 'description="https://example.test/bills/quarterly"' in view["text"]
    assert "Untitled tab" not in view["text"]
    _assert_compact_model_text(view["text"])


def test_nonchrome_active_surface_lists_without_deep_reading_background_chrome() -> None:
    harness = _Harness()
    harness.install_desktop([
        {"key": "calc", "title": "Budget — LibreOffice Calc", "active": True},
        {"key": "chrome", "title": "Google Chrome", "active": False},
    ], app="Desktop Apps")
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert view["active_surface"] == "A"
    assert "LibreOffice Calc" in view["text"]
    assert "browser.tabs" in resources
    assert "browser.elements" not in resources
    assert {
        "resource": "ui.elements",
        "parameters": {"active_surface_only": True, "max_records": 1500},
    } in harness.runtime.observe_calls


def test_native_link_without_safe_exact_uri_does_not_offer_browser_action() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "mail",
        "title": "Message — Thunderbird",
        "active": True,
        "elements": [{
            "native": "mail-link",
            "role": "link",
            "name": "Private target",
            "url": "https://user:secret@example.test/private",
            "states": {"visible": True, "enabled": True},
            "advertised_actions": ["jump"],
        }],
    }], app="Thunderbird")
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    assert "Open in new Chrome tab" not in view["text"]
    assert "browser.tabs" in {
        call["resource"] for call in harness.runtime.observe_calls
    }


def test_desktop_only_surface_probes_browser_and_preserves_cdp_only_chrome() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "ding",
        "title": "@!0,0;BDHF",
        "active": False,
    }], app="gjs")
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:cdp-only",
        title="CDP-only page",
        url="https://example.test/cdp-only",
        active=True,
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    resources = [call["resource"] for call in harness.runtime.observe_calls]
    assert resources.count("browser.tabs") == 1
    assert resources.count("browser.elements") == 1
    assert view["surface_count"] == 2
    assert "[A] Desktop" in view["text"]
    assert "[B] Chrome — CDP-only page — active" in view["text"]
    assert "Active Surface [B] Chrome — CDP-only page — active" in view["text"]
    assert 'tab "CDP-only page"' in view["text"]


def test_browser_page_text_is_fetched_only_for_clear_complete_text_queries() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Chrome", "active": True}],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:article",
        title="Article",
        url="https://example.test/article",
        active=True,
    )
    body = harness.runtime.record(
        "browser.text",
        "page-text:article",
        text="PRIVATE_BODY_MARKER Full article body.",
        url="https://example.test/article",
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["browser.text"] = [body]

    for query in (None, "billing", "complete checkout"):
        harness.runtime.observe_calls.clear()
        view = harness.facade.read(query=query)
        resources = [call["resource"] for call in harness.runtime.observe_calls]
        assert "browser.text" not in resources
        assert "PRIVATE_BODY_MARKER" not in view["text"]

    for query in ("page text", "read the full page", "complete text from the page"):
        harness.runtime.observe_calls.clear()
        view = harness.facade.read(query=query)
        resources = [call["resource"] for call in harness.runtime.observe_calls]
        assert resources.count("browser.text") == 1
        assert "PRIVATE_BODY_MARKER" in view["text"]
        assert 'page text "Page text 1/1"' in view["text"]
        _assert_compact_model_text(view["text"])


def test_complete_browser_page_text_chunks_have_stable_ids_and_cursors() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Chrome", "active": True}],
        app="Chrome",
    )
    tab = harness.runtime.record(
        "browser.tabs",
        "tab:long-article",
        title="Long article",
        url="https://example.test/long",
        active=True,
    )
    long_body = "\n".join(
        f"Paragraph {index:03d} " + (chr(ord("a") + index % 26) * 650)
            for index in range(180)
    )
    body = harness.runtime.record(
        "browser.text",
        "page-text:long-article",
        text=long_body,
        url="https://example.test/long",
    )
    harness.runtime.observations["browser.tabs"] = [tab]
    harness.runtime.observations["browser.text"] = [body]

    first = harness.facade.read(query="complete page text", limit=200)
    first_ids = _element_ids(first["text"])
    cursor = first["next_cursor"]
    assert first_ids
    assert cursor
    assert first["element_count"] > first["returned_elements"]
    assert len(first["text"]) <= MAX_OUTPUT_CHARS
    assert 'read_computer(cursor="' in first["text"]
    page_text_elements = [
        element
        for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.resource == "browser.text"
    ]
    assert page_text_elements
    assert all(
        len(element.text) <= PAGE_TEXT_CHUNK_CHARS
        and element.resource == "browser.text"
        and element.click_action is None
        and element.type_action is None
        for element in page_text_elements
    )
    _assert_compact_model_text(first["text"])

    repeated = harness.facade.read(query="complete page text", limit=200)
    assert _element_ids(repeated["text"]) == first_ids

    harness.runtime.observe_calls.clear()
    second = harness.facade.read(cursor=cursor, limit=200)
    second_ids = _element_ids(second["text"])
    assert second_ids
    assert set(first_ids).isdisjoint(second_ids)
    assert "browser.text" in {
        call["resource"] for call in harness.runtime.observe_calls
    }
    assert len(second["text"]) <= MAX_OUTPUT_CHARS

    with _Raises(ProtocolError) as click_error:
        harness.facade.click(first_ids[0])
    assert click_error.value.code is ErrorCode.UNSUPPORTED
    with _Raises(ProtocolError) as type_error:
        harness.facade.type_text(first_ids[0], "replacement")
    assert type_error.value.code is ErrorCode.UNSUPPORTED
    assert harness.runtime.action_calls == []


def test_combobox_without_advertised_actions_still_routes_invoke() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "main",
        "title": "Main",
        "active": True,
        "elements": [{
            "native": "account-picker",
            "role": "combo box",
            "name": "Account",
            "states": {"visible": True},
        }],
    }])
    view = harness.facade.read()
    picker = _element_named(harness, "Account")
    assert 'combo box "Account" click' in view["text"]

    harness.facade.click(picker.public_id)

    call = harness.runtime.action_calls[-1]["payload"]
    assert call["action"] == "invoke"
    assert call["target"] == {"ref": picker.ref}
    assert call["arguments"] == {}


def test_native_link_action_aliases_are_rendered_and_dispatched_as_clicks() -> None:
    for advertised_action in ("link.open", "open_link", "jump", "do-default"):
        harness = _Harness()
        harness.install_desktop([{
            "key": "mail",
            "title": "Message",
            "active": True,
            "elements": [{
                "native": f"link-{advertised_action}",
                "role": "link",
                "name": "Open invoice",
                "states": {"visible": True, "enabled": True},
                "advertised_actions": [advertised_action],
            }],
        }], app="Thunderbird")

        view = harness.facade.read()
        link = _element_named(harness, "Open invoice")
        assert f'link "Open invoice" click' in view["text"]

        harness.facade.click(link.public_id)

        call = harness.runtime.action_calls[-1]["payload"]
        assert call["action"] == "invoke"
        assert call["target"] == {"ref": link.ref}
        assert call["arguments"] == {"advertised_action": advertised_action}


def test_browser_link_with_invoke_capability_remains_directly_clickable() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Example", "active": True}],
        app="Chrome",
    )
    link = harness.runtime.record(
        "browser.elements",
        "page:details",
        role="link",
        name="Details",
        advertised_actions=["invoke", "scroll_into_view"],
        states={"visible": True},
    )
    harness.runtime.observations["browser.elements"] = [link]

    view = harness.facade.read()
    element = _element_named(harness, "Details")
    assert 'link "Details" click' in view["text"]

    harness.facade.click(element.public_id)

    call = harness.runtime.action_calls[-1]["payload"]
    assert call["action"] == "invoke"
    assert call["arguments"] == {}


def test_browser_generic_node_and_its_text_child_click_the_exact_owning_node() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Calendar", "active": True}],
        app="Chrome",
    )
    day = harness.runtime.record(
        "browser.elements",
        "page:day-10",
        role="generic",
        name="10",
        advertised_actions=["scroll_into_view"],
        states={"visible": True},
    )
    label = harness.runtime.record(
        "browser.elements",
        "page:day-10-label",
        role="statictext",
        name="10 day label",
        parent_ref=day["ref"],
        advertised_actions=["scroll_into_view"],
        states={"visible": True},
    )
    harness.runtime.observations["browser.elements"] = [day, label]

    view = harness.facade.read(query="10")
    parent = next(
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.ref == day["ref"]
    )
    child = next(
        element for element in harness.facade._current_elements.values()  # noqa: SLF001
        if element.ref == label["ref"]
    )
    assert f'[{parent.public_id}] generic "10" click' in view["text"]
    assert child.click_action is None

    harness.facade.click(child.public_id)

    call = harness.runtime.action_calls[-1]["payload"]
    assert call["action"] == "invoke"
    assert call["target"] == {"ref": day["ref"]}
    assert call["arguments"] == {}


def test_file_chooser_exposes_one_virtual_path_input_bound_to_choose_path() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "chooser",
        "role": "file chooser",
        "title": "Open File",
        "active": True,
    }], app="Files")
    chooser = harness.runtime.record(
        "os.file_choosers",
        "chooser:open",
        title="Open File",
        mode="open",
    )
    harness.runtime.observations["os.file_choosers"] = [chooser]

    view = harness.facade.read()
    path_input = _element_named(harness, "Choose exact guest path")
    assert 'input "Choose exact guest path" type=replace' in view["text"]
    _assert_compact_model_text(view["text"])

    harness.facade.type_text(path_input.public_id, "/home/oai/share/invoice.pdf")

    call = harness.runtime.action_calls[-1]["payload"]
    assert call["action"] == "choose_path"
    assert call["target"] == {"ref": chooser["ref"]}
    assert call["arguments"] == {"path": "/home/oai/share/invoice.pdf"}


def test_save_file_chooser_does_not_claim_existing_path_shortcut() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "chooser",
        "role": "dialog",
        "title": "Save",
        "active": True,
        "states": {"modal": True},
    }], app="LibreOffice Writer")
    chooser = harness.runtime.record(
        "os.file_choosers",
        "chooser:save",
        title="Save",
        mode="save",
    )
    harness.runtime.observations["os.file_choosers"] = [chooser]

    view = harness.facade.read()

    assert "Choose exact guest path" not in view["text"]
    assert 'note "Exact path shortcut unavailable"' in view["text"]
    gap = _element_named(harness, "Exact path shortcut unavailable")
    assert gap.ref == chooser["ref"]
    assert gap.click_action is None
    assert gap.type_action is None


def test_unknown_file_chooser_mode_is_an_honest_read_only_gap() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "chooser",
        "role": "file chooser",
        "title": "Choose destination",
        "active": True,
    }], app="Files")
    chooser = harness.runtime.record(
        "os.file_choosers",
        "chooser:unknown-mode",
        title="Choose destination",
    )
    harness.runtime.observations["os.file_choosers"] = [chooser]

    view = harness.facade.read()

    assert "Choose exact guest path" not in view["text"]
    gap = _element_named(harness, "Exact path shortcut unavailable")
    assert 'note "Exact path shortcut unavailable"' in view["text"]
    assert gap.click_action is None
    assert gap.type_action is None


def test_nonmodal_window_with_chooser_like_title_does_not_route_choose_path() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "ordinary",
        "role": "window",
        "title": "Open File",
        "active": True,
    }], app="Notes")
    chooser = harness.runtime.record(
        "os.file_choosers",
        "chooser:stale",
        title="Open File",
        mode="open",
    )
    harness.runtime.observations["os.file_choosers"] = [chooser]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read()

    assert "Choose exact guest path" not in view["text"]
    assert "os.file_choosers" not in {
        call["resource"] for call in harness.runtime.observe_calls
    }


def test_writer_paragraph_and_table_cell_bind_to_exact_native_edit_actions() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "writer",
        "title": "Quarterly.odt — LibreOffice Writer",
        "active": True,
    }], app="LibreOffice Writer")
    document = harness.runtime.record(
        "document.sessions",
        "document:quarterly",
        document_type="writer",
        title="Quarterly.odt",
        url="file:///home/oai/share/Quarterly.odt",
        modified=True,
    )
    paragraph = harness.runtime.record(
        "writer.paragraphs",
        "paragraph:0",
        index=0,
        style="Body Text",
        text="Old paragraph",
        alignment="left",
    )
    table = harness.runtime.record(
        "writer.tables",
        "table:budget",
        name="Budget table",
        cell_count=4,
        cells=[
            {"name": "A1", "text": "Category"},
            {"name": "B2", "text": "100"},
        ],
    )
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["writer.paragraphs"] = [paragraph]
    harness.runtime.observations["writer.tables"] = [table]

    view = harness.facade.read()
    insertion_element = _element_named(harness, "Document end")
    paragraph_element = _element_named(harness, "Body Text")
    cell_element = _element_named(harness, "B2")
    assert (
        'input "Document end" description="Append paragraphs without replacing existing text" '
        'type=insert'
    ) in view["text"]
    assert 'paragraph "Body Text" text="Old paragraph"' in view["text"]
    assert 'cell "B2" text="100"' in view["text"]
    _assert_compact_model_text(view["text"])

    harness.facade.type_text(
        insertion_element.public_id,
        "Added paragraph\r\n\r\nFinal paragraph\n",
    )
    insertion_call = harness.runtime.action_calls[-1]["payload"]
    assert insertion_call["action"] == "insert_paragraphs"
    assert insertion_call["target"] == {"ref": paragraph["ref"]}
    assert insertion_call["arguments"] == {
        "paragraphs": ["Added paragraph", "", "Final paragraph", ""],
        "position": "end",
    }

    harness.facade.type_text(paragraph_element.public_id, "Updated paragraph")
    paragraph_call = harness.runtime.action_calls[-1]["payload"]
    assert paragraph_call["action"] == "replace_text"
    assert paragraph_call["target"] == {"ref": paragraph["ref"]}
    assert paragraph_call["arguments"] == {"text": "Updated paragraph"}

    harness.facade.type_text(cell_element.public_id, "250")
    cell_call = harness.runtime.action_calls[-1]["payload"]
    assert cell_call["action"] == "set_table_cell"
    assert cell_call["target"] == {"ref": table["ref"]}
    assert cell_call["arguments"] == {"cell": "B2", "text": "250"}


def test_writer_and_calc_modified_documents_expose_and_clear_native_save() -> None:
    for app, kind, filename in (
        ("LibreOffice Writer", "writer", "Notes.odt"),
        ("LibreOffice Calc", "calc", "Budget.ods"),
    ):
        harness = _Harness()
        harness.install_desktop([{
            "key": kind,
            "title": f"{filename} — {app}",
            "active": True,
        }], app=app)
        session = harness.runtime.record(
            "document.sessions",
            f"session:{kind}",
            document_type=kind,
            title=filename,
            url=f"file:///home/oai/share/{filename}",
            modified=True,
        )
        state = harness.runtime.record(
            "document.state",
            f"state:{kind}",
            document_type=kind,
            title=filename,
            url=f"file:///home/oai/share/{filename}",
            modified=True,
            has_location=True,
            read_only=False,
            advertised_actions=["save"],
        )
        harness.runtime.observations["document.sessions"] = [session]
        harness.runtime.observations["document.state"] = [state]

        initial = harness.facade.read()
        save = _element_named(harness, "Save document")
        save_line = next(
            line for line in initial["text"].splitlines()
            if f"[{save.public_id}]" in line
        )
        assert 'button "Save document"' in save_line
        assert 'description="Save current changes to the existing file"' in save_line
        assert " click" in save_line
        initial_active = next(
            line for line in initial["text"].splitlines()
            if line.startswith("Active Surface")
        )
        assert "modified" in initial_active.casefold()

        original_act = harness.runtime._act

        def save_and_refresh(payload, *, consume_budget, request_id):
            result = original_act(
                payload, consume_budget=consume_budget, request_id=request_id,
            )
            if payload.get("action") == "save":
                harness.runtime.observations["document.state"][0]["modified"] = False
                harness.runtime.observations["document.sessions"][0]["modified"] = False
            return result

        harness.runtime._act = save_and_refresh  # type: ignore[method-assign]
        refreshed = harness.facade.click(save.public_id)

        call = harness.runtime.action_calls[-1]["payload"]
        assert call["action"] == "save"
        assert call["target"] == {"ref": state["ref"]}
        assert call["arguments"] == {}
        assert refreshed["text"].startswith(
            "After action — surface [A] modified=true→false; "
        )
        fresh_render = refreshed["text"].split("\n\nCOMPUTER", 1)[1]
        assert "Save document" not in fresh_render
        refreshed_active = next(
            line for line in refreshed["text"].splitlines()
            if line.startswith("Active Surface")
        )
        assert "modified" not in refreshed_active.casefold()


def test_save_document_is_hidden_without_location_or_when_read_only() -> None:
    for has_location, read_only, actions in (
        (False, False, ["save"]),
        (True, True, ["save"]),
        (True, False, []),
    ):
        harness = _Harness()
        harness.install_desktop([{
            "key": "writer",
            "title": "Untitled — LibreOffice Writer",
            "active": True,
        }], app="LibreOffice Writer")
        session = harness.runtime.record(
            "document.sessions", "session:writer",
            document_type="writer", title="Untitled", modified=True,
        )
        state = harness.runtime.record(
            "document.state", "state:writer",
            document_type="writer", title="Untitled", modified=True,
            has_location=has_location, read_only=read_only,
            url="file:///home/oai/share/Untitled.odt" if has_location else "",
            advertised_actions=actions,
        )
        harness.runtime.observations["document.sessions"] = [session]
        harness.runtime.observations["document.state"] = [state]

        view = harness.facade.read()

        assert "Save document" not in view["text"]


def test_writer_document_end_id_stays_stable_but_tracks_the_live_last_paragraph() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "writer",
        "title": "Notes.odt — LibreOffice Writer",
        "active": True,
    }], app="LibreOffice Writer")
    document = harness.runtime.record(
        "document.sessions",
        "document:notes",
        document_type="writer",
        title="Notes.odt",
        url="file:///home/oai/share/Notes.odt",
    )
    first = harness.runtime.record(
        "writer.paragraphs",
        "paragraph:0",
        index=0,
        style="Body Text",
        text="Existing text",
    )
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["writer.paragraphs"] = [first]

    first_view = harness.facade.read()
    first_insertion = _element_named(harness, "Document end")
    assert "Existing text" in first_view["text"]

    appended = harness.runtime.record(
        "writer.paragraphs",
        "paragraph:1",
        index=1,
        style="Body Text",
        text="Already appended",
    )
    harness.runtime.observations["writer.paragraphs"] = [first, appended]
    refreshed_view = harness.facade.read()
    refreshed_insertion = _element_named(harness, "Document end")

    assert refreshed_insertion.public_id == first_insertion.public_id
    assert refreshed_insertion.ref == appended["ref"]
    assert "Existing text" in refreshed_view["text"]
    assert "Already appended" in refreshed_view["text"]

    harness.facade.type_text(refreshed_insertion.public_id, "Next paragraph")
    call = harness.runtime.action_calls[-1]["payload"]
    assert call["target"] == {"ref": appended["ref"]}
    assert call["action"] == "insert_paragraphs"
    assert call["arguments"] == {
        "paragraphs": ["Next paragraph"],
        "position": "end",
    }


def test_calc_targeted_query_and_scalar_formula_and_rectangular_paste_routing() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "calc",
        "title": "Budget.ods — LibreOffice Calc",
        "active": True,
    }], app="LibreOffice Calc")
    document = harness.runtime.record(
        "document.sessions",
        "document:budget",
        document_type="calc",
        title="Budget.ods",
        url="file:///home/oai/share/Budget.ods",
    )
    sheet = harness.runtime.record(
        "spreadsheet.sheets",
        "sheet:budget",
        name="Budget",
        index=0,
        active=True,
    )
    cell = harness.runtime.record(
        "spreadsheet.cells",
        "cell:budget:C4",
        sheet="Budget",
        address="C4",
        display="7",
        value=7,
    )
    target_range = harness.runtime.record(
        "spreadsheet.ranges",
        "range:budget:C4:D5",
        sheet="Budget",
        range="C4:D5",
    )
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["spreadsheet.sheets"] = [sheet]
    harness.runtime.observations["spreadsheet.cells"] = [cell]
    harness.runtime.observations["spreadsheet.ranges"] = [target_range]
    harness.runtime.observe_calls.clear()

    view = harness.facade.read(query="Budget!C4")
    cell_element = _element_named(harness, "Budget C4")
    initial_resources = [
        call["resource"] for call in harness.runtime.observe_calls
    ]
    assert "browser.tabs" in initial_resources
    assert "browser.elements" not in initial_resources
    assert {
        "resource": "ui.elements",
        "parameters": {"active_surface_only": True, "max_records": 1500},
    } in harness.runtime.observe_calls
    assert {
        "resource": "spreadsheet.cells",
        "parameters": {"range": "C4", "sheet": "Budget"},
    } in harness.runtime.observe_calls
    assert (
        'cell "Budget C4" value="7" '
        'type=replace-or-grid(tabs=columns,newlines=rows,start=here)'
    ) in view["text"]
    _assert_compact_model_text(view["text"])

    for typed, action, arguments in (
        ("42", "set_value", {"value": 42}),
        ("plain text", "set_text", {"text": "plain text"}),
        ("=SUM(A1:A3)", "set_formula", {"formula": "=SUM(A1:A3)"}),
    ):
        harness.facade.read(query="Budget!C4")
        harness.facade.type_text(cell_element.public_id, typed)
        call = harness.runtime.action_calls[-1]["payload"]
        assert call["action"] == action
        assert call["target"] == {"ref": cell["ref"]}
        assert call["arguments"] == arguments

    harness.facade.read(query="Budget!C4")
    actions_before_grid_paste = len(harness.runtime.action_calls)
    harness.facade.type_text(cell_element.public_id, "1\t2\n3\t4")
    assert len(harness.runtime.action_calls) == actions_before_grid_paste + 1
    values_call = harness.runtime.action_calls[-1]["payload"]
    assert {
        "resource": "spreadsheet.ranges",
        "parameters": {"sheet": "Budget", "range": "C4:D5"},
    } in harness.runtime.observe_calls
    assert values_call["action"] == "set_range_values"
    assert values_call["target"] == {"ref": target_range["ref"]}
    assert values_call["arguments"] == {"values": [[1, 2], [3, 4]]}

    harness.facade.read(query="Budget!C4")
    harness.facade.type_text(cell_element.public_id, "1\t=A1\n2\t=A2")
    formulas_call = harness.runtime.action_calls[-1]["payload"]
    assert formulas_call["action"] == "set_range_formulas"
    assert formulas_call["target"] == {"ref": target_range["ref"]}
    assert formulas_call["arguments"] == {
        "formulas": [["1", "=A1"], ["2", "=A2"]],
    }


def test_calc_sheet_and_cell_virtual_ids_ignore_transient_native_refs() -> None:
    harness = _Harness()
    harness.install_desktop([{
        "key": "calc",
        "title": "Budget.ods — LibreOffice Calc",
        "active": True,
    }], app="LibreOffice Calc")
    document = harness.runtime.record(
        "document.sessions",
        "document:budget",
        document_type="calc",
        title="Budget.ods",
    )
    old_sheet = harness.runtime.record(
        "spreadsheet.sheets", "sheet:old", name="Budget", index=0, active=True,
    )
    old_cell = harness.runtime.record(
        "spreadsheet.cells", "cell:old", sheet="Budget", address="C4", display="7",
    )
    harness.runtime.observations["document.sessions"] = [document]
    harness.runtime.observations["spreadsheet.sheets"] = [old_sheet]
    harness.runtime.observations["spreadsheet.cells"] = [old_cell]

    harness.facade.read(query="Budget!C4")
    first_sheet = _element_named(harness, "Budget")
    first_cell = _element_named(harness, "Budget C4")

    fresh_sheet = harness.runtime.record(
        "spreadsheet.sheets", "sheet:fresh", name="Budget", index=0, active=True,
    )
    fresh_cell = harness.runtime.record(
        "spreadsheet.cells", "cell:fresh", sheet="Budget", address="C4", display="7",
    )
    harness.runtime.observations["spreadsheet.sheets"] = [fresh_sheet]
    harness.runtime.observations["spreadsheet.cells"] = [fresh_cell]
    harness.facade.read(query="Budget!C4")
    second_sheet = _element_named(harness, "Budget")
    second_cell = _element_named(harness, "Budget C4")

    assert second_sheet.public_id == first_sheet.public_id
    assert second_sheet.identity == first_sheet.identity
    assert second_cell.public_id == first_cell.public_id
    assert second_cell.identity == first_cell.identity
    harness.facade.type_text(second_cell.public_id, "9")
    assert harness.runtime.action_calls[-1]["payload"]["target"] == {
        "ref": fresh_cell["ref"],
    }


def test_vlc_virtual_controls_bind_native_buttons_volume_seek_and_shuffle() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "vlc", "title": "VLC media player", "active": True}],
        app="VLC",
    )
    player = harness.runtime.record(
        "vlc.playback",
        "vlc:player",
        identity="VLC",
        title="Blue in Green",
        artists=["Miles Davis"],
        playback_status="playing",
        position_seconds=9.5,
        duration_seconds=329,
        volume=0.8,
        loop="none",
        shuffle=False,
        can_control=True,
        can_seek=True,
    )
    harness.runtime.observations["vlc.playback"] = [player]

    view = harness.facade.read()
    assert 'media "Blue in Green" text="Miles Davis"' in view["text"]
    for name in ("Play", "Pause", "Stop", "Shuffle", "Volume (0–2)", "Position (seconds)"):
        assert name in view["text"]
    _assert_compact_model_text(view["text"])

    for name, action, arguments in (
        ("Play", "play", {}),
        ("Pause", "pause", {}),
        ("Stop", "stop", {}),
        ("Shuffle", "set_shuffle", {"enabled": True}),
    ):
        element = _element_named(harness, name)
        harness.facade.click(element.public_id)
        call = harness.runtime.action_calls[-1]["payload"]
        assert call["action"] == action
        assert call["target"] == {"ref": player["ref"]}
        assert call["arguments"] == arguments

    volume = _element_named(harness, "Volume (0–2)")
    harness.facade.type_text(volume.public_id, "1.25")
    volume_call = harness.runtime.action_calls[-1]["payload"]
    assert volume_call["action"] == "set_volume"
    assert volume_call["target"] == {"ref": player["ref"]}
    assert volume_call["arguments"] == {"volume": 1.25}

    position = _element_named(harness, "Position (seconds)")
    harness.facade.type_text(position.public_id, "12.5")
    seek_call = harness.runtime.action_calls[-1]["payload"]
    assert seek_call["action"] == "seek"
    assert seek_call["target"] == {"ref": player["ref"]}
    assert seek_call["arguments"] == {"position_seconds": 12.5}


def test_chrome_settings_typed_value_and_extension_toggle_use_native_bindings() -> None:
    harness = _Harness()
    harness.install_desktop(
        [{"key": "chrome", "title": "Chrome", "active": True}],
        app="Chrome",
    )
    settings_tab = harness.runtime.record(
        "browser.tabs",
        "tab:chrome-internal",
        title="Settings",
        url="chrome://settings/appearance",
        active=True,
    )
    setting = harness.runtime.record(
        "chrome.settings",
        "setting:home-button",
        key="browser.show_home_button",
        value=True,
        type="boolean",
    )
    harness.runtime.observations["browser.tabs"] = [settings_tab]
    harness.runtime.observations["chrome.settings"] = [setting]

    settings_view = harness.facade.read()
    setting_element = _element_named(harness, "browser.show_home_button")
    assert 'setting "browser.show_home_button" value="True" type=replace' in settings_view["text"]
    _assert_compact_model_text(settings_view["text"])
    harness.facade.type_text(setting_element.public_id, "false")
    setting_call = harness.runtime.action_calls[-1]["payload"]
    assert setting_call["action"] == "set_pref"
    assert setting_call["target"] == {"ref": setting["ref"]}
    assert setting_call["arguments"] == {"value": False}

    extensions_tab = harness.runtime.record(
        "browser.tabs",
        "tab:chrome-internal",
        title="Extensions",
        url="chrome://extensions/",
        active=True,
    )
    extension = harness.runtime.record(
        "chrome.extensions",
        "extension:ghost-helper",
        name="Ghost Helper",
        version="1.2.3",
        enabled=True,
        install_type="normal",
    )
    harness.runtime.observations["browser.tabs"] = [extensions_tab]
    harness.runtime.observations["chrome.extensions"] = [extension]
    extensions_view = harness.facade.read()
    extension_element = _element_named(harness, "Ghost Helper")
    extension_line = next(
        line for line in extensions_view["text"].splitlines()
        if 'extension "Ghost Helper"' in line
    )
    assert 'value="version 1.2.3; enabled True"' in extension_line
    assert extension_line.endswith(" click")
    _assert_compact_model_text(extensions_view["text"])
    harness.facade.click(extension_element.public_id)
    disable_call = harness.runtime.action_calls[-1]["payload"]
    assert disable_call["action"] == "disable_extension"
    assert disable_call["target"] == {"ref": extension["ref"]}
    assert disable_call["arguments"] == {}

    disabled_extension = harness.runtime.record(
        "chrome.extensions",
        "extension:ghost-helper",
        name="Ghost Helper",
        version="1.2.3",
        enabled=False,
        install_type="normal",
    )
    harness.runtime.observations["chrome.extensions"] = [disabled_extension]
    harness.facade.read()
    disabled_element = _element_named(harness, "Ghost Helper")
    harness.facade.click(disabled_element.public_id)
    enable_call = harness.runtime.action_calls[-1]["payload"]
    assert enable_call["action"] == "enable_extension"
    assert enable_call["target"] == {"ref": disabled_extension["ref"]}
    assert enable_call["arguments"] == {}


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    del loader, standard_tests, pattern
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
