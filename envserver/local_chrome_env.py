"""A DesktopEnv-compatible environment backed by a LOCAL Chrome.

Why this exists
---------------
The AWS account is blocked, so no OSWorld VM can start. But most Chrome tasks
never needed the whole desktop: 44 of 46 launch Chrome with remote debugging and
socat 9222->1337, and their evaluators read final state over CDP. If the task
setup and its grader only touch Chrome, the Chrome does not have to live in a VM.

This class presents the slice of the DesktopEnv surface that the server and the
real evaluators use, backed by a locally launched Chrome.

What is REAL here
-----------------
- The tasks: unmodified OSWorld task JSON, including their setup steps.
- The grading: OSWorld's own `DesktopEnv.evaluate` and `_set_evaluator_info` are
  called directly on this object, so metric selection, conjunction handling,
  the infeasible rule and the FAIL rule are the benchmark's code, not a
  reimplementation. The getters are OSWorld's too.

What is NOT the same as stock OSWorld — read before quoting any number
----------------------------------------------------------------------
1. Chrome runs on the host, not inside the VM, and starts from a fresh profile.
2. There is no desktop. pyautogui actions cannot run, so tasks needing an OS-level
   route (the classic one is ctrl+shift+T to reopen a closed tab) are
   unreachable here. That pushes scores DOWN, not up, but it makes this an
   easier-to-fail environment rather than a faithful one.
3. `get_active_url_from_accessTree` reads Chrome's omnibox out of the AT-SPI
   tree. There is no AT-SPI on macOS, so `controller.get_accessibility_tree`
   synthesises the one node that getter looks for, with the active tab's URL
   taken from CDP. The URL is real; the transport is not.

So results from this runner are "OSWorld browser tasks, OSWorld graders, local
Chrome" — a legitimate measurement of harness changes, and NOT a stock OSWorld
score. Label them that way every time.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "OSWorld"))

# OSWorld imports the old 'pydrive'; only 'pydrive2' installs on current Python.
# Aliasing is enough because nothing on the Chrome path actually uses Drive.
try:  # pragma: no cover
    import pydrive  # noqa: F401
except ImportError:  # pragma: no cover
    import pydrive2, pydrive2.auth, pydrive2.drive
    sys.modules.setdefault("pydrive", pydrive2)
    sys.modules.setdefault("pydrive.auth", pydrive2.auth)
    sys.modules.setdefault("pydrive.drive", pydrive2.drive)

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

logger = logging.getLogger("envserver.localchrome")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Namespaces the real getter's CSSSelector is built with.
NS = {
    "st": "uri:deskat:state.at-spi.gnome.org",
    "attr": "uri:deskat:attributes.at-spi.gnome.org",
    "cp": "uri:deskat:component.at-spi.gnome.org",
}


_PORT_LOCK = threading.Lock()
_CLAIMED: set[int] = set()


def _free_port(start: int = 9300) -> int:
    """Allocate a debugging port for one Chrome.

    Episodes run concurrently, and a bare "is anything listening?" check races:
    two envs probe the same port before either Chrome has bound it, then the
    second Chrome fails to start or -- worse -- both agents drive the same
    browser and the results are silently entangled. Claimed ports are tracked
    under a lock so a port is handed out once.
    """
    with _PORT_LOCK:
        for port in range(start, start + 400):
            if port in _CLAIMED:
                continue
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    _CLAIMED.add(port)
                    return port
    raise RuntimeError("no free port for Chrome")


class _Controller:
    """Stands in for DesktopEnv.controller.

    Only `get_accessibility_tree` is used by the Chrome getters, and only to
    find the omnibox. Synthesise exactly that node, with the URL read from CDP,
    and let OSWorld's own parsing and normalisation run on it unchanged.
    """

    def __init__(self, env: "LocalChromeEnv"):
        self._env = env

    def get_accessibility_tree(self) -> str | None:
        url = self._env.active_url()
        if url is None:
            return None
        return (
            '<desktop-frame xmlns:st="{st}" xmlns:attr="{attr}" xmlns:cp="{cp}">'
            '<application name="Google Chrome">'
            '<frame name="Chrome">'
            '<entry name="Address and search bar" st:focused="true" focused="true">'
            "{url}"
            "</entry></frame></application></desktop-frame>"
        ).format(url=_xml_escape(url), **NS)


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


class _SetupController:
    """DesktopEnv calls setup_controller.setup(postconfig) inside evaluate().

    Chrome-only postconfig steps are applied; anything else is refused loudly
    rather than skipped, so a task cannot be silently graded against a state that
    was never set up.
    """

    def __init__(self, env: "LocalChromeEnv"):
        self._env = env

    def setup(self, config: list[dict] | None, enable_proxy: bool = False) -> None:
        for step in config or []:
            kind = step.get("type")
            if kind in ("chrome_open_tabs", "launch", "activate_window",
                        "chrome_close_tabs", "sleep"):
                self._env.apply_config_step(step)
            else:
                raise RuntimeError(
                    f"postconfig step '{kind}' cannot run against local Chrome; "
                    "this task must not be scored here"
                )


class LocalChromeEnv:
    """The DesktopEnv slice the env server and the real evaluators touch."""

    def __init__(self, headless: bool = True, **_ignored: Any):
        self.vm_ip = "127.0.0.1"
        self.chromium_port = _free_port()
        self.server_port = None
        self.vm_machine = "x86_64"      # selects the "Google Chrome" a11y selector
        self.vm_platform = "Linux"
        self.action_history: list[Any] = []
        self.enable_proxy = False
        self.cache_dir = tempfile.mkdtemp(prefix="localchrome-cache-")
        self.controller = _Controller(self)
        self.setup_controller = _SetupController(self)
        self._profile = tempfile.mkdtemp(prefix="localchrome-profile-")
        self._proc: subprocess.Popen | None = None
        self._pw = None
        self._browser = None
        self._headless = headless
        self.instruction = ""
        self.task_id = ""
        # All Playwright work for this env happens on one thread: the sync API
        # is bound to its creating thread and FastAPI hands each request to an
        # arbitrary threadpool thread. Public methods below submit to this pool;
        # the _impl methods they call must NOT submit again or the single worker
        # deadlocks on itself.
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="localchrome")

    def _call(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    # ---- lifecycle -------------------------------------------------------
    def _launch(self) -> None:
        args = [
            CHROME,
            f"--remote-debugging-port={self.chromium_port}",
            f"--user-data-dir={self._profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,ChromeWhatsNewUI",
            "--window-size=1280,800",
            # Several task sites (accuweather, retail) reject headless Chrome
            # outright -- accuweather returns ERR_HTTP2_PROTOCOL_ERROR. Stock
            # OSWorld runs a headed Chrome on a real desktop, so headed is both
            # the more faithful choice and the one those sites accept. The
            # window is parked off-screen so an overnight run does not take over
            # the display.
            "--window-position=-3000,-3000",
            "--disable-blink-features=AutomationControlled",
            "about:blank",
        ]
        if self._headless:
            args.insert(1, "--headless=new")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        for _ in range(60):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.chromium_port)) == 0:
                    time.sleep(1.0)
                    return
            time.sleep(0.5)
        raise RuntimeError(f"Chrome did not open CDP on {self.chromium_port}")

    def _page(self):
        from playwright.sync_api import sync_playwright
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.chromium_port}", timeout=20000)
        ctx = self._browser.contexts[0]
        return ctx.pages[-1] if ctx.pages else ctx.new_page()

    def active_url(self) -> str | None:
        return self._call(self._active_url_impl)

    def _active_url_impl(self) -> str | None:
        try:
            return self._page().url
        except Exception:
            logger.warning("could not read active url", exc_info=True)
            return None

    # ---- task setup ------------------------------------------------------
    def apply_config_step(self, step: dict) -> None:
        self._call(self._apply_config_step_impl, step)

    def _apply_config_step_impl(self, step: dict) -> None:
        kind = step.get("type")
        params = step.get("parameters", {}) or {}
        if kind == "chrome_open_tabs":
            urls = params.get("urls_to_open", []) or []
            ctx = self._browser.contexts[0] if self._browser else None
            for i, url in enumerate(urls):
                page = (self._page() if i == 0 and ctx and ctx.pages
                        else (ctx.new_page() if ctx else self._page()))
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    logger.warning("setup: could not load %s", url)
                page.wait_for_timeout(800)
        elif kind == "chrome_close_tabs":
            pass
        elif kind == "launch":
            # Chrome and socat only; the real launch is done by _launch(), and
            # socat is just the VM's 9222->1337 forward, which has no local
            # equivalent because Chrome binds our port directly.
            cmd = params.get("command") or []
            head = str(cmd[0]) if cmd else ""
            if "chrome" not in head.lower() and head != "socat":
                raise RuntimeError(f"cannot launch '{head}' locally")
        elif kind in ("activate_window", "sleep"):
            pass
        else:
            raise RuntimeError(f"config step '{kind}' cannot run against local Chrome")

    def reset(self, task_config: dict | None = None) -> dict:
        return self._call(self._reset_impl, task_config)

    def _reset_impl(self, task_config: dict | None = None) -> dict:
        assert task_config is not None
        self.task_id = task_config.get("id", "")
        self.instruction = task_config.get("instruction", "")
        self.action_history = []
        # Use OSWorld's own evaluator wiring so metric/getter/conjunction
        # resolution is the benchmark's logic rather than a copy of it.
        DesktopEnv._set_evaluator_info(self, task_config)
        self._launch()
        self._page()
        for step in task_config.get("config", []) or []:
            self._apply_config_step_impl(step)
        return self._obs_impl()

    # ---- observation / actions ------------------------------------------
    def _get_obs(self) -> dict:
        return self._call(self._obs_impl)

    def _obs_impl(self) -> dict:
        shot = None
        try:
            shot = self._page().screenshot(type="png")
        except Exception:
            logger.debug("screenshot failed", exc_info=True)
        return {"screenshot": shot, "accessibility_tree": None}

    def step(self, command: str, pause: float = 1.0):
        return self._call(self._step_impl, command, pause)

    def _step_impl(self, command: str, pause: float = 1.0):
        """Desktop actions have no local equivalent.

        Recorded in action_history (DONE/FAIL drive the real evaluate() rules)
        but otherwise refused explicitly. Silently accepting them would let a
        task look attempted when nothing happened.
        """
        self.action_history.append(command)
        if command in ("DONE", "FAIL", "WAIT"):
            if command == "WAIT":
                time.sleep(min(5.0, max(0.0, pause)))
            return self._obs_impl(), 0, command in ("DONE", "FAIL"), {}
        obs = self._obs_impl()
        obs["error"] = ("This environment has no desktop: pyautogui and key actions "
                        "are unavailable. Use the web_* tools.")
        return obs, 0, False, {}

    def evaluate(self):
        # Let the page settle first. Observed on a real episode: the agent had
        # reached the correct article, and the grader still returned 0 because
        # reading body text raced an in-flight navigation ("Execution context
        # was destroyed"). That is a measurement failure being recorded as an
        # agent failure. Settling is environment-side -- the grader itself is
        # untouched, and the real VM has the same race.
        try:
            self._call(self._settle)
        except Exception:
            logger.debug("settle before evaluate failed", exc_info=True)
        # OSWorld's own evaluate, unmodified, bound to this object.
        return DesktopEnv.evaluate(self)

    def _settle(self, timeout_ms: int = 8000) -> None:
        page = self._page()
        for state in ("domcontentloaded", "load"):
            try:
                page.wait_for_load_state(state, timeout=timeout_ms)
            except Exception:
                pass
        page.wait_for_timeout(700)

    def close(self) -> None:
        """Guarantee the browser dies.

        The earlier version routed teardown through the pinned thread. If that
        thread was mid page operation the close blocked forever, the episode was
        never removed from the registry, and its Chrome stayed resident: 14
        Chromes and 5 "open" episodes for 2 concurrent workers, ending in an
        out-of-memory kill. Almost certainly the same leak behind the orphaned
        AWS VMs that had to be swept by hand.

        So: kill the process FIRST and unconditionally, since terminating a
        subprocess needs no cooperation from the Playwright thread. The tidy
        Playwright shutdown is best-effort and time-boxed after that.
        """
        proc, self._proc = self._proc, None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    logger.warning("could not kill Chrome pid %s", getattr(proc, "pid", "?"))
        try:
            self._pool.submit(self._close_impl).result(timeout=5)
        except Exception:
            logger.debug("playwright teardown skipped", exc_info=True)
        finally:
            self._pool.shutdown(wait=False)
        shutil.rmtree(self._profile, ignore_errors=True)
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        with _PORT_LOCK:
            _CLAIMED.discard(self.chromium_port)

    def _close_impl(self) -> None:
        for shut in (lambda: self._browser and self._browser.close(),
                     lambda: self._pw and self._pw.stop()):
            try:
                shut()
            except Exception:
                pass
        self._browser = self._pw = None


if __name__ == "__main__":
    task = json.load(open(sys.argv[1]))
    env = LocalChromeEnv(headless=True)
    try:
        env.reset(task_config=task)
        print("instruction:", env.instruction)
        print("active url:", env.active_url())
        print("score with no agent actions:", env.evaluate())
    finally:
        env.close()
