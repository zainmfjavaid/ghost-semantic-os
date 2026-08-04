"""Task-agnostic conformance checks for the semantic desktop runtime."""
from __future__ import annotations

import base64
import json
import random
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)


class FakeEnv:
    def __init__(self):
        self.actions = 0
        self.commands = []
        self.exec_calls = []
        self.observation = {"screenshot": None, "accessibility_tree": None}
        self.controller = self

    def _get_obs(self):
        return self.observation

    def step(self, command, pause=1.0):
        self.actions += 1
        self.commands.append(command)
        return self._get_obs(), 0, False, {}

    def execute_python_command(self, command):
        self.exec_calls.append(command)
        return {
            "status": "success",
            "output": (
                server._GUEST_EXEC_MARKER
                + '{"status":"success","output":"hello",'
                '"error":"","returncode":0}'
            ),
            "error": "",
            "returncode": 0,
        }


desktop_env = types.ModuleType("desktop_env")
desktop_env_module = types.ModuleType("desktop_env.desktop_env")
desktop_env_module.DesktopEnv = FakeEnv
desktop_env.desktop_env = desktop_env_module
sys.modules["desktop_env"] = desktop_env
sys.modules["desktop_env.desktop_env"] = desktop_env_module

import server  # noqa: E402


SEMANTIC_XML = """<desktop
  xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
  xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
  xmlns:val="https://accessibility.ubuntu.example.org/ns/value"
  xmlns:act="https://accessibility.ubuntu.example.org/ns/action">
  <application name="Example App" st:showing="true" st:visible="true"
    st:enabled="true" cp:screencoord="(0, 0)" cp:size="(1280, 720)">
    <frame name="Primary Window" st:showing="true" st:visible="true"
      st:enabled="true" cp:screencoord="(0, 0)" cp:size="(1280, 720)">
      <push-button name="Uniform action" st:showing="true" st:visible="true"
        st:enabled="true" st:focusable="true" act:click_desc="activate"
        cp:screencoord="(12, 20)" cp:size="(80, 24)" />
      <combo-box name="Mode" st:showing="true" st:visible="true"
        st:enabled="true" st:expanded="true" val:value="Advanced"
        cp:screencoord="(100, 60)" cp:size="(120, 28)">Advanced</combo-box>
      <menu-item name="Open advanced panel" st:showing="true" st:visible="true"
        st:enabled="true" act:click_desc="open"
        cp:screencoord="(400, 200)" cp:size="(40, 20)" />
      <menu name="Open advanced panel" st:showing="true" st:visible="true"
        st:enabled="true" cp:screencoord="(400, 200)" cp:size="(40, 20)" />
    </frame>
  </application>
</desktop>"""

TRANSIENT_XML = """<desktop
  xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
  xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
  xmlns:act="https://accessibility.ubuntu.example.org/ns/action">
  <application name="Example App" st:showing="true" st:visible="true"
    cp:screencoord="(0, 0)" cp:size="(1280, 720)">
    <frame name="Primary Window" st:showing="true" st:visible="true"
      cp:screencoord="(0, 0)" cp:size="(1280, 720)">
      <alert name="Confirm operation" st:showing="true" st:visible="true"
        cp:screencoord="(300, 200)" cp:size="(400, 220)">
        <push-button name="Confirm" st:showing="true" st:visible="true"
          st:enabled="true" act:click_desc="press"
          cp:screencoord="(520, 350)" cp:size="(100, 30)" />
      </alert>
    </frame>
  </application>
</desktop>"""

CONTEXT_XML = """<desktop
  xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
  xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
  xmlns:act="https://accessibility.ubuntu.example.org/ns/action">
  <application name="Alpha App" st:showing="true" st:visible="true"
    cp:screencoord="(0, 0)" cp:size="(600, 500)">
    <frame name="Alpha Window" st:showing="true" st:visible="true"
      cp:screencoord="(0, 0)" cp:size="(600, 500)">
      <panel><panel><panel><panel><panel>
        <push-button name="Save" st:showing="true" st:visible="true"
          st:enabled="true" st:focused="true" act:click_desc="activate"
          cp:screencoord="(20, 30)" cp:size="(80, 24)" />
      </panel></panel></panel></panel></panel>
    </frame>
  </application>
  <application name="Beta App" st:showing="true" st:visible="true"
    cp:screencoord="(620, 0)" cp:size="(600, 500)">
    <frame name="Beta Window" st:showing="true" st:visible="true"
      cp:screencoord="(620, 0)" cp:size="(600, 500)">
      <panel><panel><panel><panel><panel>
        <push-button name="Save" st:showing="true" st:visible="true"
          st:enabled="true" act:click_desc="activate"
          cp:screencoord="(650, 30)" cp:size="(80, 24)" />
      </panel></panel></panel></panel></panel>
    </frame>
  </application>
</desktop>"""


def main():
    episode_id = "desktop-guard-test"
    env = FakeEnv()
    pool = ThreadPoolExecutor(max_workers=1)
    server._episodes[episode_id] = {
        "env": env,
        "steps": 0,
        "som": True,
        "marks": [[10, 10, 20, 20]],
        "semantic_elements": [],
        "web": True,
        "pool": pool,
    }
    command = "import pyautogui; pyautogui.press('down')"
    try:
        diagnostic = None
        for _ in range(server.BLIND_DESKTOP_ACTION_LIMIT):
            diagnostic = server.step(
                episode_id, server.StepRequest(command=command, pause=0),
            )
            assert not diagnostic.get("errors")
        assert diagnostic is not None
        assert env.actions == server.BLIND_DESKTOP_ACTION_LIMIT
        steps_at_checkpoint = server._episodes[episode_id]["steps"]
        checkpoint = server.step(
            episode_id, server.StepRequest(command=command, pause=0),
        )
        assert checkpoint.get("errors")
        assert "was not executed" in checkpoint["errors"][0]
        assert checkpoint["blind_action_streak"] == server.BLIND_DESKTOP_ACTION_LIMIT
        assert checkpoint["steps"] == steps_at_checkpoint
        assert env.actions == server.BLIND_DESKTOP_ACTION_LIMIT
        resumed = server.step(
            episode_id, server.StepRequest(command=command, pause=0),
        )
        assert not resumed.get("errors")
        assert env.actions == server.BLIND_DESKTOP_ACTION_LIMIT + 1
        print("PASS long blind keyboard streak forces a re-grounding checkpoint")

        server._episodes[episode_id]["blind_desktop_streak"] = (
            server.BLIND_DESKTOP_ACTION_LIMIT
        )
        server.get_obs(episode_id)
        assert server._episodes[episode_id]["blind_desktop_streak"] == 0
        server._episodes[episode_id]["blind_desktop_streak"] = (
            server.BLIND_DESKTOP_ACTION_LIMIT
        )
        env.observation = {"screenshot": None, "accessibility_tree": SEMANTIC_XML}
        found_after_blind = server.element_find(
            episode_id, server.ElementFind(query="Uniform action"),
        )
        assert not found_after_blind.get("errors")
        assert server._episodes[episode_id]["blind_desktop_streak"] == 0
        print("PASS screenshot and desktop_find explicitly reset the blind checkpoint")

        steps_before_exec = server._episodes[episode_id]["steps"]
        executed = server.computer_exec(
            episode_id,
            server.ComputerExecRequest(
                script="printf 'hello'", timeout_seconds=12,
                working_dir="/home/oai/share",
            ),
        )
        assert not executed.get("errors")
        assert executed["steps"] == steps_before_exec + 1
        assert "printf 'hello'" in env.exec_calls[-1]
        assert "set -o pipefail" in env.exec_calls[-1]
        assert "timeout=12" in env.exec_calls[-1]
        assert "cwd='/home/oai/share'" in env.exec_calls[-1]
        assert "start_new_session=True" in env.exec_calls[-1]
        assert '"returncode":0' in executed["result"]
        assert '"stdout":"hello"' in executed["result"]
        assert "reload or reopen it before the final UI save" in executed["result"]
        print("PASS bounded guest CLI execution is traced as one computer step")

        try:
            server.computer_exec(
                episode_id,
                server.ComputerExecRequest(script="true", timeout_seconds=301),
            )
            raise AssertionError("overlong guest execution should be rejected")
        except server.HTTPException as exc:
            assert exc.status_code == 400
            assert "between 1 and 300" in exc.detail
        print("PASS guest execution remains bounded at five minutes")

        python_executed = server.computer_exec(
            episode_id,
            server.ComputerExecRequest(
                script="print(6 * 7)", language="python", timeout_seconds=9,
            ),
        )
        assert not python_executed.get("errors")
        assert "['/usr/bin/env', 'python3', '-c', 'print(6 * 7)']" in env.exec_calls[-1]
        assert "timeout=9" in env.exec_calls[-1]
        print("PASS bounded guest Python uses a real Python interpreter")

        calls_before_language_rejection = len(env.exec_calls)
        try:
            server.computer_exec(
                episode_id,
                server.ComputerExecRequest(script="puts 42", language="ruby"),
            )
            raise AssertionError("unknown guest language should be rejected")
        except server.HTTPException as exc:
            assert exc.status_code == 400
            assert "bash" in exc.detail and "python" in exc.detail
        assert len(env.exec_calls) == calls_before_language_rejection
        print("PASS guest execution rejects unadvertised languages before stepping")

        default_cwd = server.computer_exec(
            episode_id,
            server.ComputerExecRequest(script="pwd", timeout_seconds=5),
        )
        assert not default_cwd.get("errors")
        assert "cwd='/home/user'" in env.exec_calls[-1]
        print("PASS guest CLI defaults to the real guest user home")

        expected_envelope = {
            "status": "success", "output": "ok", "error": "", "returncode": 0,
        }
        noisy_envelope = server._decode_guest_exec_result({
            "output": (
                f"user process text ... {server._GUEST_EXEC_MARKER}not-json\n"
                f"{server._GUEST_EXEC_MARKER}also-not-json\n"
                f"{server._GUEST_EXEC_MARKER}{json.dumps(expected_envelope)}\n"
            ),
        })
        assert noisy_envelope == expected_envelope
        print("PASS guest result decoder ignores marker text in command output")

        exec_calls_before = len(env.exec_calls)
        steps_before_rejection = server._episodes[episode_id]["steps"]
        try:
            server.computer_exec(
                episode_id,
                server.ComputerExecRequest(script="xdotool key Return"),
            )
            raise AssertionError("invisible UI automation should be rejected")
        except server.HTTPException as exc:
            assert exc.status_code == 400
            assert "cannot automate browser or desktop UI invisibly" in exc.detail
        assert len(env.exec_calls) == exec_calls_before
        assert server._episodes[episode_id]["steps"] == steps_before_rejection
        print("PASS guest code cannot become an invisible browser/desktop action path")

        actions_before_semantic = env.actions
        server._episodes[episode_id]["marks"] = [[10, 10, 20, 20]]
        server._episodes[episode_id]["semantic_elements"] = []
        semantic = server.element_action(
            episode_id,
            server.ElementAction(index=1, action="click", pause=0),
        )
        assert not semantic.get("errors")
        allowed = server.step(
            episode_id, server.StepRequest(command=command, pause=0),
        )
        assert not allowed.get("errors")
        assert "blind_action_streak" not in allowed
        assert env.actions == actions_before_semantic + 2
        print("PASS semantic desktop action resets blind-strategy telemetry")

        server._episodes[episode_id]["marks"] = [
            [10, 10, 20, 20] for _ in range(154)
        ]
        server._episodes[episode_id]["marks"][151] = [400, 200, 40, 20]
        server._episodes[episode_id]["marks"][152] = [900, 600, 40, 20]
        server._episodes[episode_id]["semantic_elements"] = []
        acted = server.element_action(
            episode_id,
            server.ElementAction(index=152, action="click", pause=0),
        )
        assert not acted.get("errors")
        assert "pyautogui.click(420, 210)" in env.commands[-1]
        assert acted["acted_on"]["index"] == 152
        assert acted["acted_on"]["x"] == 420
        assert acted["acted_on"]["y"] == 210
        print("PASS visible one-based label resolves to matching private bound")

        extracted = server._extract_semantic_elements(SEMANTIC_XML)
        assert extracted is not None
        element_text, semantic_marks, records = extracted
        uniform = next(record for record in records if record["name"] == "Uniform action")
        mode = next(record for record in records if record["name"] == "Mode")
        assert uniform["bounds"] == [12, 20, 80, 24]
        assert uniform["path"] == [0, 0, 0]
        assert uniform["actions"] == ["click"]
        assert uniform["context"][-2:] == [
            "application:Example App", "frame:Primary Window",
        ]
        assert mode["value"] == "Advanced"
        assert "expanded" in mode["states"]
        assert uniform["bounds"] in semantic_marks
        assert "Uniform action" in element_text
        print("PASS typed graph preserves hierarchy, value, state, action and bounds")

        env.observation = {"screenshot": None, "accessibility_tree": SEMANTIC_XML}
        found = server.element_find(
            episode_id,
            server.ElementFind(query="Mode", role="combo-box", state="expanded"),
        )
        assert not found.get("errors")
        assert found["candidate_count"] == 1
        assert '"value":"Advanced"' in found["result"]
        assert '"context"' in found["result"]
        print("PASS live semantic queries filter by label, role and state")

        listed = server.element_find(episode_id, server.ElementFind())
        assert not listed.get("errors")
        assert listed["candidate_count"] >= 3
        assert "showing" in listed["result"]
        assert '"name":"Uniform action"' in listed["result"]
        assert '"name":"Primary Window"' in listed["result"]
        print("PASS empty semantic query lists current surfaces and controls")

        duplicate_wrapper = server.element_find(
            episode_id,
            server.ElementFind(query="Open   advanced panel"),
        )
        assert duplicate_wrapper["candidate_count"] == 1
        assert '"actions":["click"]' in duplicate_wrapper["result"]
        print("PASS same-bound wrapper duplicates collapse to advertised action node")

        native = server.element_match(
            episode_id,
            server.ElementMatch(
                query="Uniform action", role="push-button", action="click", pause=0,
            ),
        )
        assert not native.get("errors")
        assert "pyatspi.Registry.getDesktop" in env.commands[-1]
        assert "pyautogui.click(52, 32)" in env.commands[-1]
        assert "bool(_semantic_actions.doAction(" in env.commands[-1]
        assert "_semantic_same_transient_target" not in env.commands[-1]
        assert native["acted_on"]["execution"] == (
            "native-accessibility-with-pointer-fallback"
        )
        print("PASS advertised native action is preferred with guarded pointer fallback")

        env.observation = {
            "screenshot": None, "accessibility_tree": TRANSIENT_XML,
        }
        transient = server.element_match(
            episode_id,
            server.ElementMatch(
                query="Confirm", role="push-button", action="click", pause=0,
            ),
        )
        assert not transient.get("errors")
        assert "_semantic_pointer_first = True" in env.commands[-1]
        assert "_semantic_time.sleep(0.6)" in env.commands[-1]
        assert "pyautogui.click(570, 365)" in env.commands[-1]
        assert "def _semantic_activate_live" in env.commands[-1]
        assert "_semantic_after_native" in env.commands[-1]
        assert "def _semantic_find_transient_target" in env.commands[-1]
        assert "_semantic_owner_name == 'Confirm operation'" in env.commands[-1]
        assert "_semantic_seen < 5000" in env.commands[-1]
        assert "for _semantic_index in" not in env.commands[-1]
        assert env.commands[-1].index("pyautogui.click(570, 365)") < (
            env.commands[-1].index("_semantic_activate_live(_semantic_after_pointer)")
        )
        assert "pyautogui.press('enter')" in env.commands[-1]
        assert "transient desktop control did not dismiss" in env.commands[-1]
        print("PASS transient activation avoids stale paths and verifies every fallback")

        ambiguous_xml = SEMANTIC_XML.replace(
            "    </frame>",
            """      <push-button name="Uniform action" st:showing="true"
        st:visible="true" st:enabled="true" act:click_desc="activate"
        cp:screencoord="(700, 300)" cp:size="(80, 24)" />
    </frame>""",
        )
        env.observation = {"screenshot": None, "accessibility_tree": ambiguous_xml}
        ambiguous = server.element_match(
            episode_id,
            server.ElementMatch(query="Uniform action", role="push-button", pause=0),
        )
        assert any("ambiguous (2 matches)" in error for error in ambiguous["errors"])
        assert ambiguous["result"].count('"name":"Uniform action"') == 2
        print("PASS equal labels at distinct bounds remain structured ambiguity")

        ref_listing = server.element_find(
            episode_id,
            server.ElementFind(query="Uniform action", role="push-button"),
        )
        ref_records = [
            json.loads(line)
            for line in ref_listing["result"].splitlines()[1:]
            if line.startswith("{")
        ]
        assert len(ref_records) == 2
        assert len({record["ref"] for record in ref_records}) == 2
        assert all("bounds" not in record and "path" not in record for record in ref_records)
        exact_ref = server.element_match(
            episode_id,
            server.ElementMatch(ref=ref_records[1]["ref"], action="click", pause=0),
        )
        assert not exact_ref.get("errors"), exact_ref
        assert exact_ref["acted_on"]["x"] == 740
        print("PASS opaque ref resolves an otherwise unnameable duplicate")

        refreshed_listing = server.element_find(
            episode_id,
            server.ElementFind(query="Uniform action", role="push-button"),
        )
        old_second_ref = [
            json.loads(line)["ref"]
            for line in refreshed_listing["result"].splitlines()[1:]
            if line.startswith("{")
        ][1]
        actions_before_stale = env.actions
        env.observation = {"screenshot": None, "accessibility_tree": SEMANTIC_XML}
        stale_ref = server.element_match(
            episode_id,
            server.ElementMatch(ref=old_second_ref, action="click", pause=0),
        )
        assert any("became stale" in error for error in stale_ref["errors"])
        assert env.actions == actions_before_stale
        print("PASS stale desktop ref fails closed after a UI change")

        env.observation = {"screenshot": None, "accessibility_tree": CONTEXT_XML}
        context_records = server._extract_semantic_elements(CONTEXT_XML)
        assert context_records is not None
        save_records = [
            record for record in context_records[2] if record["name"] == "Save"
        ]
        assert len(save_records) == 2
        assert "application:Alpha App" in save_records[0]["context"]
        assert "frame:Alpha Window" in save_records[0]["context"]
        scoped = server.element_match(
            episode_id,
            server.ElementMatch(
                query="Save", role="push-button", context="Alpha Window",
                action="click", pause=0,
            ),
        )
        assert not scoped.get("errors")
        assert "Alpha Window" in scoped["result"]
        assert "pyautogui.click(60, 42)" in env.commands[-1]
        context_listing = server.element_find(episode_id, server.ElementFind())
        first_save = context_listing["result"].find('"name":"Save"')
        alpha_window = context_listing["result"].find("Alpha Window")
        beta_window = context_listing["result"].find("Beta Window")
        assert alpha_window >= 0 and beta_window >= 0 and first_save >= 0
        assert first_save < beta_window
        print("PASS owner context survives deep nesting and scopes duplicate labels")

        rng = random.Random(20260731)
        for case_index in range(32):
            label = f"Generated-{rng.randrange(10**9):09d}"
            x = rng.randrange(10, 1100)
            y = rng.randrange(10, 650)
            width = rng.randrange(20, 140)
            height = rng.randrange(16, 60)
            generated_xml = f"""<desktop
              xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
              xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
              xmlns:act="https://accessibility.ubuntu.example.org/ns/action">
              <push-button name="{label}" st:showing="true" st:visible="true"
                st:enabled="true" st:focusable="true" act:click_desc="activate"
                cp:screencoord="({x}, {y})" cp:size="({width}, {height})" />
            </desktop>"""
            env.observation = {
                "screenshot": None, "accessibility_tree": generated_xml,
            }
            generated = server.element_match(
                episode_id,
                server.ElementMatch(
                    query=label, role="push-button", action="click", pause=0,
                ),
            )
            assert not generated.get("errors"), (case_index, generated)
            assert generated["acted_on"]["x"] == int(x + width / 2)
            assert generated["acted_on"]["y"] == int(y + height / 2)
            assert label in generated["result"]
        print("PASS generated labels and layouts resolve without benchmark vocabulary")

        image_buffer = BytesIO()
        Image.new("RGB", (1280, 720), "white").save(image_buffer, format="PNG")
        hybrid_entry = {"som": True, "web": True}
        hybrid = server._encode(
            {
                "screenshot": image_buffer.getvalue(),
                "accessibility_tree": SEMANTIC_XML,
            },
            hybrid_entry,
            compare_change=False,
        )
        rendered = Image.open(
            BytesIO(base64.b64decode(hybrid["screenshot"])),
        ).convert("RGB")
        assert hybrid.get("desktop_accessibility_ready") is True
        assert any(
            surface["name"] == "Primary Window"
            for surface in hybrid.get("desktop_surfaces", [])
        )
        assert "elements" not in hybrid
        assert rendered.getpixel((640, 360))[0] > 200
        assert any(
            record["name"] == "Uniform action"
            for record in hybrid_entry["semantic_elements"]
        )
        print("PASS flat-color controls survive while hybrid screenshot stays plain")

        semantic_only_entry = {
            "som": True, "web": False, "semantic_only": True,
        }
        semantic_only = server._encode(
            {
                "screenshot": image_buffer.getvalue(),
                "accessibility_tree": SEMANTIC_XML,
            },
            semantic_only_entry,
            compare_change=False,
        )
        semantic_only_rendered = Image.open(
            BytesIO(base64.b64decode(semantic_only["screenshot"])),
        ).convert("RGB")
        assert semantic_only.get("desktop_accessibility_ready") is True
        assert any(
            surface["name"] == "Example App"
            for surface in semantic_only.get("desktop_surfaces", [])
        )
        assert "elements" not in semantic_only
        assert semantic_only_entry["marks_are_semantic"] is True
        assert semantic_only_rendered.getpixel((640, 360))[0] > 200
        print("PASS semantic-only desktop keeps screenshots plain and targets private")

        original_apply_som = server._apply_som
        try:
            # Deliberately make the visual mark index disagree with the typed
            # semantic graph. Named actions must use their selected live record,
            # while the legacy numbered fallback must keep using its own mark.
            server._apply_som = lambda _shot, _tree: (
                image_buffer.getvalue(),
                "index\trole\tname\n1\tpush-button\tVisual-only mark",
                [[900, 600, 40, 20]],
            )
            pure_entry = {
                "som": True, "web": False,
                "marks": [], "semantic_elements": [],
            }
            pure = server._encode(
                {
                    "screenshot": image_buffer.getvalue(),
                    "accessibility_tree": SEMANTIC_XML,
                },
                pure_entry,
                compare_change=False,
            )
            assert pure_entry["marks"] == [[900, 600, 40, 20]]
            assert pure_entry["marks_are_semantic"] is False
            assert any(
                record["name"] == "Uniform action"
                for record in pure_entry["semantic_elements"]
            )
            server._episodes[episode_id].update(pure_entry)
            env.observation = {
                "screenshot": image_buffer.getvalue(),
                "accessibility_tree": SEMANTIC_XML,
            }
            pure_named = server.element_match(
                episode_id,
                server.ElementMatch(
                    query="Uniform action", role="push-button",
                    action="click", pause=0,
                ),
            )
            assert not pure_named.get("errors")
            assert pure_named["acted_on"]["x"] == 52
            assert "pyatspi.Registry.getDesktop" in env.commands[-1]
            pure_numbered = server.element_action(
                episode_id,
                server.ElementAction(index=1, action="click", pause=0),
            )
            assert not pure_numbered.get("errors")
            assert pure_numbered["acted_on"]["x"] == 920
            assert env.commands[-1] == "pyautogui.click(920, 610)"
            print("PASS pure desktop semantic graph is exhaustive and mark-independent")
        finally:
            server._apply_som = original_apply_som

        env.observation = {"screenshot": None, "accessibility_tree": None}
        stale = server.element_find(
            episode_id,
            server.ElementFind(query="Uniform action"),
        )
        assert any("stale targets were discarded" in error for error in stale["errors"])
        assert server._episodes[episode_id]["semantic_elements"] == []
        assert server._episodes[episode_id]["marks"] == []
        print("PASS failed refresh discards stale semantic references")

        env.observation = {"screenshot": None, "accessibility_tree": SEMANTIC_XML}
        server._episodes[episode_id]["som_elements"] = [
            "1\tcombo-box\tUnrelated Panel\t\t\t\t\t",
        ]
        actions_before = env.actions
        key_allowed = server.step(
            episode_id,
            server.StepRequest(
                command="import pyautogui; pyautogui.hotkey('ctrl', 'l')", pause=0,
            ),
        )
        assert not key_allowed.get("errors")
        assert env.actions == actions_before + 1

        class FakeWebProvider:
            def run_js(self, code, frame):
                return '{"ok": true}'

            def elements(self):
                return []

        server._episodes[episode_id]["web_provider"] = FakeWebProvider()
        for expected_streak in range(1, server.MAX_CONSECUTIVE_READONLY_JS + 1):
            web_allowed = server.web_action(
                episode_id,
                server.WebAction(
                    action="js", code="document.title", observe=False,
                ),
            )
            assert not web_allowed.get("errors")
            if expected_streak > 2:
                assert web_allowed["readonly_js_streak"] == expected_streak
        web_blocked = server.web_action(
            episode_id,
            server.WebAction(
                action="js", code="document.body.innerText", observe=False,
            ),
        )
        assert web_blocked.get("errors")
        assert "result" not in web_blocked
        server.web_action(
            episode_id,
            server.WebAction(action="elements", observe=False),
        )
        web_resumed = server.web_action(
            episode_id,
            server.WebAction(action="js", code="document.title", observe=False),
        )
        assert not web_resumed.get("errors")
        assert web_resumed.get("result")
        print("PASS generic strategy gate is task/site/evaluator independent")
    finally:
        server._episodes.pop(episode_id, None)
        pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
