"""Web element provider over Chrome DevTools Protocol.

Why this exists
---------------
A controlled experiment on the mock env showed element-index actions solving a
task in 3 tool calls where raw coordinates flailed for 16 and failed. So the
interface works — but Set-of-Marks over the AT-SPI tree gave no benefit on real
browser tasks, most likely because Chrome does not publish page content to AT-SPI
unless launched with --force-renderer-accessibility (a flag OSWorld never sets).
The tree the agent was shown was probably browser furniture: tabs and toolbar.

CDP sidesteps that entirely. It reads the live DOM, so page content is always
available, and it is already part of this environment: 44 of 46 Chrome tasks
launch Chrome with --remote-debugging-port=1337, and OSWorld's own evaluators
connect over CDP to grade page state.

Scope and honesty
-----------------
This is a *browser* capability, not a general desktop one. Clicks dispatched
in-page do not move the OS cursor and cannot touch browser chrome (bookmarks bar,
settings dialogs), so it complements pyautogui rather than replacing it. Any
result produced with it must be reported as using a CDP-assisted harness, since
that is a deviation from the stock pyautogui-only action space — legitimate
(no task-specific knowledge, and the benchmark itself speaks CDP) but material.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import os
from urllib.parse import quote_plus, urlsplit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

logger = logging.getLogger("envserver.web")


class WebProviderStalled(RuntimeError):
    """A pinned Playwright call exceeded its wall-clock safety deadline."""

    provider_stalled = True


def _json_text(value) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return json.dumps({"ok": False, "error": str(value)})


def _url_key(value: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


def _public_http_url(value: str) -> str:
    """Validate a model-provided research URL without creating a hidden LAN API.

    Batch reading is a browser capability, not an unrestricted network client.
    Keep it on ordinary public HTTP(S) pages and reject literal/private or
    special-use hostnames. The page still loads through the task browser's own
    Chrome context, so its normal cookies, network policy and observable traffic
    apply.
    """
    url = (value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("research URLs must be absolute public http(s) URLs")
    hostname = parsed.hostname.casefold().rstrip(".")
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError(f"local research URL is not allowed: {hostname}")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError(f"non-public research address is not allowed: {hostname}")
    return url

# Elements a user can actually act on. Deliberately broad-but-bounded: too narrow
# and the agent cannot reach real controls, too wide and the list becomes noise.
INTERACTIVE_HANDLES_JS = r"""
() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, ' +
              '[role=button], [role=link], [role=textbox], [role=combobox], ' +
              '[role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
              '[role=menuitemcheckbox], [role=menuitemradio], [role=option], ' +
              '[role=switch], [role=slider], [role=spinbutton], [role=treeitem], ' +
              '[onclick], [contenteditable]:not([contenteditable=false]), ' +
              '[tabindex]:not([tabindex="-1"]), summary';
  const roots = [document];
  const elements = [];
  for (let i = 0; i < roots.length; i++) {
    const root = roots[i];
    elements.push(...root.querySelectorAll(sel));
    for (const candidate of root.querySelectorAll('*')) {
      if (candidate.shadowRoot) roots.push(candidate.shadowRoot);
    }
  }
  const out = [];
  for (const el of elements) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    out.push(el);
  }
  return out.slice(0, 400);
}
"""

INTERACTIVE_DESCRIPTORS_JS = r"""
(elements) => {
  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'button';
    if (tag !== 'input') return '';
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    return 'textbox';
  };
  const out = [];
  for (const el of elements) {
    const r = el.getBoundingClientRect();
    const root = el.getRootNode();
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\s+/).filter(Boolean)
      .map(id => {
        const label = root.getElementById?.(id) || document.getElementById(id);
        return label?.innerText || label?.textContent || '';
      })
      .join(' ');
    const associatedLabel = el.labels
      ? Array.from(el.labels).map(label => label.innerText || label.textContent || '')
          .join(' ')
      : '';
    const label = (
      el.getAttribute('aria-label') || labelledBy || el.getAttribute('placeholder') ||
      el.getAttribute('title') || associatedLabel || el.value || el.innerText ||
      el.getAttribute('alt') || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 200);
    const onscreen = !(r.bottom < 0 || r.top > innerHeight ||
                       r.right < 0 || r.left > innerWidth);
    out.push({
      onscreen,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || implicitRole(el),
      type: el.getAttribute('type') || '',
      label,
      value: ('value' in el ? String(el.value) : '').slice(0, 120),
      checked: el.matches(
        'input[type=checkbox], input[type=radio], [role=checkbox], [role=radio]'
      ) ? Boolean(el.checked || el.getAttribute('aria-checked') === 'true') : null,
      selected: el.hasAttribute('aria-selected')
        ? el.getAttribute('aria-selected') === 'true' : null,
      expanded: el.hasAttribute('aria-expanded')
        ? el.getAttribute('aria-expanded') === 'true' : null,
      pressed: el.hasAttribute('aria-pressed')
        ? el.getAttribute('aria-pressed') === 'true' : null,
      active_descendant: el.getAttribute('aria-activedescendant') || '',
      disabled: ('disabled' in el && Boolean(el.disabled)) ||
        el.getAttribute('aria-disabled') === 'true',
      href: (el.getAttribute('href') || '').slice(0, 120),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  return out;
}
"""

# A small, page-local browser API installed before every model-written script.
# Raw querySelector is general but needlessly brittle for a small model:
# React-controlled inputs need the native value setter, useful observations
# need stable selectors, and a single inspect/fill/click program should replace
# a half-dozen discovery turns. The helpers contain no site or task knowledge.
GHOST_HELPERS_JS = r"""
() => {
  const interactive = 'a[href], button, input:not([type=hidden]), select, textarea, ' +
    '[role=button], [role=link], [role=textbox], [role=combobox], [role=checkbox], ' +
    '[role=radio], [role=tab], [role=menuitem], [role=menuitemcheckbox], ' +
    '[role=menuitemradio], [role=option], [role=switch], [role=slider], ' +
    '[role=spinbutton], [role=treeitem], [onclick], ' +
    '[contenteditable]:not([contenteditable=false]), ' +
    '[tabindex]:not([tabindex="-1"]), summary';

  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width >= 2 && r.height >= 2 && st.visibility !== 'hidden' &&
      st.display !== 'none' && st.opacity !== '0';
  };

  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'button';
    if (tag !== 'input') return '';
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    return 'textbox';
  };

  const path = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 7) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          child => child.tagName === node.tagName
        );
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (parent && parent.id) {
        parts.unshift('#' + CSS.escape(parent.id));
        break;
      }
      node = parent;
    }
    return parts.join(' > ');
  };

  const describe = (el) => {
    const r = el.getBoundingClientRect();
    const root = el.getRootNode();
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\s+/).filter(Boolean)
      .map(id => {
        const label = root.getElementById?.(id) || document.getElementById(id);
        return label?.innerText || label?.textContent || '';
      })
      .join(' ');
    const associatedLabel = el.labels
      ? Array.from(el.labels).map(label => label.innerText || label.textContent || '')
          .join(' ')
      : '';
    return {
      selector: path(el),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || implicitRole(el),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      label: (
        el.getAttribute('aria-label') || labelledBy || el.getAttribute('placeholder') ||
        el.getAttribute('title') || associatedLabel || el.innerText || el.value || ''
      ).replace(/\s+/g, ' ').trim().slice(0, 160),
      value: 'value' in el ? String(el.value).slice(0, 300) : '',
      checked: el.matches(
        'input[type=checkbox], input[type=radio], [role=checkbox], [role=radio]'
      ) ? Boolean(el.checked || el.getAttribute('aria-checked') === 'true') : undefined,
      selected: el.hasAttribute('aria-selected')
        ? el.getAttribute('aria-selected') === 'true' : undefined,
      expanded: el.hasAttribute('aria-expanded')
        ? el.getAttribute('aria-expanded') === 'true' : undefined,
      pressed: el.hasAttribute('aria-pressed')
        ? el.getAttribute('aria-pressed') === 'true' : undefined,
      activeDescendant: el.getAttribute('aria-activedescendant') || '',
      disabled: ('disabled' in el && Boolean(el.disabled)) ||
        el.getAttribute('aria-disabled') === 'true',
      href: (el.getAttribute('href') || '').slice(0, 300),
      onscreen: !(r.bottom < 0 || r.top > innerHeight ||
                  r.right < 0 || r.left > innerWidth),
    };
  };

  // Native querySelector does not cross a shadow boundary. Playwright's CSS
  // locators do, and the page-side helpers should see the same controls. Walk
  // every open shadow root while keeping ordinary CSS selector semantics
  // within each root. Closed roots remain a screenshot/indexed-action fallback.
  const roots = () => {
    const out = [document];
    for (let i = 0; i < out.length; i++) {
      for (const candidate of out[i].querySelectorAll('*')) {
        if (candidate.shadowRoot) out.push(candidate.shadowRoot);
      }
    }
    return out;
  };
  const all = (selector) => {
    // Small models routinely emit jQuery/Playwright text pseudos in otherwise
    // valid CSS (`button:contains("Search")`, `:has-text(...)`) or pass an
    // empty selector to mean "all interactive controls". Normalize those
    // common shapes rather than turning an obvious intent into a syntax-error
    // turn. This carries no site/task knowledge; it is just locator syntax.
    const source = String(selector || '').trim() || interactive;
    const groups = source.split(/,(?![^()]*\))/).map(part => part.trim()).filter(Boolean);
    const found = [];
    for (const group of groups) {
      for (const root of roots()) {
        try {
          found.push(...root.querySelectorAll(group));
          continue;
        } catch (error) {
          const match = group.match(
            /^(.*?):(?:contains|has-text)\(\s*(['"])(.*?)\2\s*\)\s*$/
          );
          if (!match) throw error;
          const base = match[1].trim() || '*';
          const wanted = match[3].toLowerCase();
          found.push(...Array.from(root.querySelectorAll(base)).filter(
            el => (el.innerText || el.textContent || '').toLowerCase().includes(wanted)
          ));
        }
      }
    }
    return Array.from(new Set(found));
  };
  const target = (selector, index = 0) => {
    const matches = all(selector);
    const el = matches[index];
    if (!el) throw new Error(
      `No element ${index} for selector ${JSON.stringify(selector)} (${matches.length} match)`
    );
    return el;
  };
  const events = (el) => {
    el.dispatchEvent(new InputEvent('input', {
      bubbles: true, inputType: 'insertText', data: null,
    }));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };

  globalThis.ghost = Object.freeze({
    inspect(selector = interactive, limit = 40) {
      return all(selector).filter(visible).slice(0, limit).map(describe);
    },
    find(text, selector = interactive, limit = 40) {
      const q = String(text).toLowerCase();
      return all(selector).filter(visible).filter(el => {
        const root = el.getRootNode();
        const labelledBy = (el.getAttribute('aria-labelledby') || '')
          .split(/\s+/).filter(Boolean)
          .map(id => {
            const label = root.getElementById?.(id) || document.getElementById(id);
            return label?.innerText || label?.textContent || '';
          })
          .join(' ');
        const associatedLabel = el.labels
          ? Array.from(el.labels).map(label => label.innerText || label.textContent || '')
              .join(' ')
          : '';
        const haystack = [
          el.innerText, el.value, el.getAttribute('aria-label'),
          el.getAttribute('placeholder'), el.getAttribute('title'),
          el.getAttribute('name'), el.getAttribute('href'), labelledBy, associatedLabel,
        ].filter(Boolean).join(' ').toLowerCase();
        return haystack.includes(q);
      }).slice(0, limit).map(describe);
    },
    fill(selector, value, index = 0) {
      const el = target(selector, index);
      el.focus();
      if (el.isContentEditable) {
        el.textContent = String(value);
      } else {
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (!setter) throw new Error(`Element ${selector} cannot be filled`);
        setter.call(el, String(value));
      }
      events(el);
      return describe(el);
    },
    click(selector, index = 0) {
      const el = target(selector, index);
      const before = describe(el);
      el.scrollIntoView({block: 'center', inline: 'center'});
      el.click();
      return before;
    },
    select(selector, value, index = 0) {
      const el = target(selector, index);
      if (!(el instanceof HTMLSelectElement)) {
        throw new Error(`Element ${selector} is not a select`);
      }
      const wanted = String(value);
      const option = Array.from(el.options).find(
        o => o.value === wanted || o.text.trim() === wanted
      );
      if (!option) throw new Error(`No option ${JSON.stringify(wanted)} in ${selector}`);
      const setter = Object.getOwnPropertyDescriptor(
        HTMLSelectElement.prototype, 'value'
      )?.set;
      setter.call(el, option.value);
      events(el);
      return describe(el);
    },
    check(selector, wanted = true, index = 0) {
      const el = target(selector, index);
      const nativeCheck = 'checked' in el;
      const ariaCheck = el.hasAttribute('aria-checked');
      if (!nativeCheck && !ariaCheck) {
        throw new Error(`Element ${selector} is not checkable`);
      }
      const current = nativeCheck
        ? Boolean(el.checked) : el.getAttribute('aria-checked') === 'true';
      if (current !== Boolean(wanted)) el.click();
      return describe(el);
    },
    submit(selector = 'form', index = 0) {
      const el = target(selector, index);
      const form = el instanceof HTMLFormElement ? el : el.form;
      if (!form) throw new Error(`Element ${selector} has no form`);
      form.requestSubmit();
      return {submitted: true, selector: path(form)};
    },
    value(selector, index = 0) {
      return describe(target(selector, index));
    },
    text(selector = 'body', limit = 4000) {
      const el = target(selector, 0);
      return (el.innerText || el.textContent || '').trim().slice(0, limit);
    },
    wait(ms = 500) {
      return new Promise(resolve => setTimeout(
        resolve, Math.min(5000, Math.max(0, ms))
      ));
    },
  });
  return Object.keys(globalThis.ghost);
}
"""


def _pinned(method):
    """Run a WebProvider method on the provider's own thread.

    Playwright's sync API is bound to the thread that created it, while FastAPI
    serves each request from an arbitrary threadpool thread. Above concurrency 1
    that raises "greenlet.error: Cannot switch to a different thread" and the
    episode 500s. Pinning is done here rather than at the endpoints so every
    caller gets it, and it matters on the real VM too -- the queue's web arms run
    at concurrency 5 and would have hit exactly this.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        future = self._pool.submit(lambda: method(self, *args, **kwargs))
        try:
            return future.result(timeout=self._call_timeout_s)
        except FutureTimeout as exc:
            # Aborting the HTTP request is not enough: the Playwright call keeps
            # occupying this provider's only legal thread, and every later DOM
            # action queues behind it forever. Terminate any renderer execution
            # through an independent CDP connection, then tell the server to
            # retire this provider and reconnect on the next action.
            future.cancel()
            terminated = self._terminate_execution_out_of_band()
            raise WebProviderStalled(
                f"{method.__name__} exceeded {self._call_timeout_s:.0f}s; "
                "the browser connection will be replaced"
                + (" after terminating page JavaScript" if terminated else "")
            ) from exc
    return wrapper


class WebProvider:
    """Thin CDP wrapper. Connects lazily so a non-browser task pays nothing."""

    def __init__(
        self,
        vm_ip: str,
        port: int = 9222,
        fallback_ports: tuple[int, ...] = (1337,),
        script_timeout_ms: int = 10_000,
        call_timeout_s: float | None = None,
        initial_active_url: str | None = None,
    ):
        # OSWorld's own chrome getters connect to env.chromium_port, which is
        # 9222 — that is also the port opened in the security group. Task configs
        # mention --remote-debugging-port=1337, but that is a fallback relaunch
        # path, not where Chrome normally listens. Defaulting to 1337 makes every
        # connection fail in a way that looks like "CDP does not work".
        self.vm_ip = vm_ip
        self.ports = (port, *fallback_ports)
        self.endpoint = f"http://{vm_ip}:{port}"
        self._pw = None
        self._browser = None
        self._initial_active_url = initial_active_url
        self._script_timeout_ms = max(100, min(30_000, script_timeout_ms))
        configured_timeout = (
            float(os.environ.get("WEB_PROVIDER_CALL_TIMEOUT_SECONDS", "45"))
            if call_timeout_s is None else float(call_timeout_s)
        )
        self._call_timeout_s = max(0.1, min(180.0, configured_timeout))
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix=f"web-{vm_ip}-{port}")

    def _terminate_execution_out_of_band(self) -> bool:
        """Best-effort renderer interrupt that never uses the wedged pool.

        Runtime.evaluate normally enforces its own 10-second deadline, but a
        hostile or unusually busy page can wedge Playwright before that command
        returns. A fresh CDP connection can still send terminateExecution. The
        temporary Playwright driver is stopped without closing the remote
        browser; OSWorld's final state therefore survives for the replacement
        provider.
        """

        def terminate() -> int:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            count = 0
            try:
                browser = playwright.chromium.connect_over_cdp(
                    self.endpoint, timeout=5000
                )
                for context in browser.contexts:
                    for page in context.pages:
                        session = None
                        try:
                            session = context.new_cdp_session(page)
                            session.send("Runtime.terminateExecution")
                            count += 1
                        except Exception:
                            continue
                        finally:
                            if session is not None:
                                try:
                                    session.detach()
                                except Exception:
                                    pass
                return count
            finally:
                # browser.close() would close Chrome itself. Stopping this
                # temporary Playwright driver only disconnects its CDP client.
                playwright.stop()

        recovery_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="web-cdp-recovery"
        )
        recovery = recovery_pool.submit(terminate)
        try:
            return recovery.result(timeout=8) > 0
        except Exception:
            logger.warning(
                "out-of-band JavaScript termination failed on %s",
                self.endpoint,
                exc_info=True,
            )
            return False
        finally:
            recovery_pool.shutdown(wait=False, cancel_futures=True)

    def retire(self) -> None:
        """Stop accepting work; the in-flight call may unwind asynchronously."""

        self._pool.shutdown(wait=False, cancel_futures=True)

    def _frames(self):
        """Main document plus sub-frames. Booking widgets, date pickers and
        payment forms routinely live in iframes; ignoring them makes the element
        list silently incomplete on exactly the tasks that matter most."""
        page = self._page()
        frames = [page.main_frame]
        for f in page.frames:
            if f is not page.main_frame:
                frames.append(f)
        return frames

    def _page(self):
        from playwright.sync_api import sync_playwright

        if self._browser is None:
            # A task reset can return before Chrome's debug endpoint is ready.
            # If every connect candidate fails, the Playwright sync loop must
            # be stopped before this method returns. Leaving `_pw` alive while
            # `_browser` remains None makes the next call start a second sync
            # loop on the same thread, which Playwright rejects as "Sync API
            # inside the asyncio loop". That poisoned every later DOM action
            # in the episode.
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            self._pw = sync_playwright().start()
            last: Exception | None = None
            for candidate in self.ports:
                endpoint = f"http://{self.vm_ip}:{candidate}"
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(endpoint, timeout=15000)
                    self.endpoint = endpoint
                    logger.info("connected to Chrome over CDP at %s", endpoint)
                    break
                except Exception as exc:
                    last = exc
                    logger.warning("CDP connect failed on %s: %s", endpoint, str(exc)[:120])
            if self._browser is None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                raise RuntimeError(
                    f"could not reach Chrome over CDP on {self.ports} at {self.vm_ip}: {last}"
                )
        ctx = self._browser.contexts[0] if self._browser.contexts else None
        if ctx is None or not ctx.pages:
            raise RuntimeError("no open page in the browser")
        # Honour an explicit switch_tab; otherwise the last page is the active
        # tab in practice (a click that opens a tab makes it the last one).
        active = getattr(self, "_active", None)
        if active is not None and active in ctx.pages and not active.is_closed():
            return active
        if self._initial_active_url:
            expected = _url_key(self._initial_active_url)
            for candidate in reversed(ctx.pages):
                try:
                    if _url_key(candidate.url) == expected:
                        self._active = candidate
                        self._initial_active_url = None
                        return candidate
                except Exception:
                    continue
        self._active = None
        return ctx.pages[-1]

    def _dispose_handles(self, handles=None) -> None:
        for handle in handles if handles is not None else getattr(self, "_handles", []):
            if handle is None:
                continue
            try:
                handle.dispose()
            except Exception:
                pass

    def _pages(self) -> list:
        if self._browser is None or not self._browser.contexts:
            return []
        return list(self._browser.contexts[0].pages)

    def _adopt_new_page(self, before: list) -> bool:
        """Follow a popup/new tab created by the action that just ran."""
        created = [
            page for page in self._pages()
            if page not in before and not page.is_closed()
        ]
        if not created:
            return False
        self._active = created[-1]
        self._initial_active_url = None
        try:
            self._active.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return True

    def _evaluate_model_code(self, page, frame_index: int, code: str):
        """Evaluate model code with Chrome's renderer execution deadline.

        Playwright's normal evaluate() has no timeout for synchronous
        JavaScript: `while (true) {}` permanently occupies the provider's only
        thread. Runtime.evaluate's `timeout` parameter terminates that execution
        inside Chrome. We map Playwright's frame ordering to CDP's frame tree so
        the same protection covers embedded widgets.
        """
        session = page.context.new_cdp_session(page)
        contexts: dict[str, int] = {}

        def remember_context(event):
            context = event.get("context", {})
            aux = context.get("auxData") or {}
            frame_id = aux.get("frameId")
            if frame_id and aux.get("isDefault"):
                contexts[frame_id] = context.get("id")

        def frame_ids(tree):
            result = [tree["frame"]["id"]]
            for child in tree.get("childFrames") or []:
                result.extend(frame_ids(child))
            return result

        session.on("Runtime.executionContextCreated", remember_context)
        try:
            session.send("Runtime.enable")
            tree = session.send("Page.getFrameTree")["frameTree"]
            ids = frame_ids(tree)
            if not (0 <= frame_index < len(ids)):
                raise IndexError(
                    f"CDP frame {frame_index} out of range (0..{max(0, len(ids) - 1)})"
                )
            context_id = contexts.get(ids[frame_index])
            if frame_index and context_id is None:
                raise RuntimeError(
                    f"default JavaScript context unavailable for frame {frame_index}"
                )

            # Playwright accepts a JavaScript program (`statement; expression`)
            # as well as an arrow/function expression. Evaluate the original
            # source verbatim so code-first keeps that surface, then invoke a
            # returned function just as Playwright does. Smaller models also
            # frequently write `return {...}` at top level or only console.log
            # their finding. Browsers reject the former and Runtime.evaluate
            # discards the latter, even though the intent is unambiguous, so
            # normalize both forms into useful tool results.
            source = code.strip()
            expression = (
                "(async () => {"
                "const __ghostLogs = [];"
                "const __ghostOriginalLog = console.log;"
                "const __ghostString = value => {"
                "try { const text = JSON.stringify(value); "
                "return text === undefined ? String(value) : text; } "
                "catch (_) { return String(value); }};"
                "console.log = (...args) => {"
                "__ghostLogs.push(args.map(__ghostString).join(' '));"
                "__ghostOriginalLog(...args);};"
                "try {"
                "let __ghostResult;"
                "try {"
                f"__ghostResult = (0, eval)({json.dumps(source)});"
                "} catch (__ghostEvalError) {"
                "if (__ghostEvalError instanceof SyntaxError && "
                "(/Illegal return statement/i.test(String(__ghostEvalError)) || "
                "/await is only valid/i.test(String(__ghostEvalError)))) {"
                "const __GhostAsyncFunction = Object.getPrototypeOf("
                "async function(){}).constructor;"
                f"__ghostResult = new __GhostAsyncFunction({json.dumps(source)})();"
                "} else { throw __ghostEvalError; }"
                "}"
                "const __ghostWork = typeof __ghostResult === 'function' "
                "? __ghostResult() : __ghostResult;"
                "const __ghostDeadline = new Promise((_, reject) => "
                f"setTimeout(() => reject(new Error('script exceeded "
                f"{self._script_timeout_ms}ms deadline')), {self._script_timeout_ms}));"
                "const __ghostValue = await Promise.race(["
                "Promise.resolve(__ghostWork), __ghostDeadline]);"
                "return (__ghostValue === undefined || __ghostValue === null) "
                "&& __ghostLogs.length ? {logs: __ghostLogs} : __ghostValue;"
                "} finally { console.log = __ghostOriginalLog; }"
                "})()"
            )
            params = {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
                "allowUnsafeEvalBlockedByCSP": True,
                "timeout": self._script_timeout_ms,
            }
            if context_id is not None:
                params["contextId"] = context_id
            response = session.send("Runtime.evaluate", params)
            if response.get("exceptionDetails"):
                details = response["exceptionDetails"]
                exception = details.get("exception") or {}
                raise RuntimeError(
                    exception.get("description")
                    or details.get("text")
                    or "JavaScript evaluation failed"
                )
            remote = response.get("result") or {}
            if "value" in remote:
                return remote["value"]
            if remote.get("unserializableValue") is not None:
                return remote["unserializableValue"]
            return None
        finally:
            try:
                session.detach()
            except Exception:
                pass

    @_pinned
    def elements(self) -> list[dict]:
        """Collect interactive elements across all frames.

        Handles are retained alongside the descriptors so actions click the real
        element rather than a coordinate. That is frame-safe (a sub-frame's
        rect is frame-relative, so coordinates would land in the wrong place)
        and survives layout shifts between listing and clicking.
        """
        self._dispose_handles()
        out: list[dict] = []
        self._handles = []
        for depth, frame in enumerate(self._frames()):
            array_handle = None
            try:
                array_handle = frame.evaluate_handle(INTERACTIVE_HANDLES_JS)
                # Describe the SAME retained array we will act through. Taking
                # two document snapshots lets dynamic pages reorder controls
                # between calls, silently mapping descriptor N to handle N+1.
                described = array_handle.evaluate(INTERACTIVE_DESCRIPTORS_JS)
                props = array_handle.get_properties()
            except Exception:
                if array_handle is not None:
                    try:
                        array_handle.dispose()
                    except Exception:
                        pass
                continue  # a frame can be cross-origin or mid-navigation
            values = list(props.values())
            try:
                array_handle.dispose()
            except Exception:
                pass
            remaining = max(0, 400 - len(out))
            for i, d in enumerate(described[:remaining]):
                if i >= len(values):
                    break
                d["frame"] = depth
                out.append(d)
                self._handles.append(values[i].as_element())
            self._dispose_handles(values[remaining:])
            if len(out) >= 400:
                break
        return out

    def describe(self, els: list[dict], limit: int | None = None,
                 label_chars: int = 80) -> str:
        """Render the element list.

        `limit`/`label_chars` exist because context growth, not screenshots, is
        the dominant cost: a full listing is re-sent with every subsequent turn,
        so a 120-row table repeated across an episode is what pushed one run to
        1.49M tokens. Trimming is done by truncation only — no relevance
        ranking, which would smuggle task-specific judgement into the harness.
        """
        # A hard display cap is necessary (a 400-row table is unreadable and
        # expensive) but it MUST be announced. Previously the list was cut at
        # 120 with nothing said, so on a large retail or search page a control
        # past that point was invisible to the agent forever and no amount of
        # re-listing would reveal it. Silent truncation reads as "this is
        # everything on the page", which is a lie.
        cap = limit if limit is not None else 120
        rows = els[:cap]
        lines = ["idx\twhere\ttag\trole/type\tstate\tlabel"]
        for i, e in enumerate(rows):
            kind = e["role"] or e["type"] or ""
            state = []
            value = str(e.get("value") or "")
            if value and value != e.get("label"):
                state.append(f"value={value[:40]}")
            if e.get("checked") is not None:
                state.append("checked" if e["checked"] else "unchecked")
            if e.get("selected") is not None:
                state.append("selected" if e["selected"] else "not-selected")
            if e.get("expanded") is not None:
                state.append("expanded" if e["expanded"] else "collapsed")
            if e.get("pressed") is not None:
                state.append("pressed" if e["pressed"] else "not-pressed")
            if e.get("active_descendant"):
                state.append(f"active={str(e['active_descendant'])[:32]}")
            if e.get("disabled"):
                state.append("disabled")
            location = []
            if e.get("frame", 0):
                location.append(f"frame={e['frame']}")
            if not e.get("onscreen", True):
                location.append("below/above fold")
            where = ",".join(location)
            lines.append(
                f"{i}\t{where}\t{e['tag']}\t{kind}\t{','.join(state)}\t"
                f"{e['label'][:label_chars]}"
            )
        if len(els) > cap:
            lines.append(
                f"... {len(els) - cap} more interactive elements exist but are not "
                f"listed. Use web_find with a word from the control you want (its "
                f"label, button text or link text) to search ALL of them.")
        return "\n".join(lines)

    @_pinned
    def tabs(self) -> list[dict]:
        """List open tabs.

        Several chrome tasks are graded on tab state (open_tabs_info,
        is_expected_tabs), and clicking a link can silently open a new one --
        _page() returns the LAST page, so the agent could be acting in a tab it
        does not know it moved to. Without this it cannot see or control that.
        """
        page = self._page()
        ctx = page.context
        active = getattr(self, "_active", None)
        if active is None or active not in ctx.pages or active.is_closed():
            active = ctx.pages[-1] if ctx.pages else None
        out = []
        for i, page in enumerate(ctx.pages):
            try:
                out.append({"index": i, "url": page.url,
                            "title": (page.title() or "")[:80],
                            "active": page is active})
            except Exception:
                out.append({"index": i, "url": "(unavailable)", "title": "",
                            "active": False})
        return out

    @_pinned
    def switch_tab(self, index: int) -> str:
        ctx = self._browser.contexts[0]
        if not (0 <= index < len(ctx.pages)):
            raise IndexError(f"tab {index} out of range (0..{len(ctx.pages) - 1})")
        page = ctx.pages[index]
        page.bring_to_front()
        # Record the choice explicitly. ctx.pages returns a fresh list each call,
        # so reordering it does nothing -- _page() honours self._active instead.
        # Without that, "switch" would report success while every later action
        # still landed on the previous tab.
        self._active = page
        self._initial_active_url = None
        return f"switched to tab {index}: {page.url[:70]}"

    @_pinned
    def close_tab(self, index: int) -> str:
        ctx = self._browser.contexts[0]
        if not (0 <= index < len(ctx.pages)):
            raise IndexError(f"tab {index} out of range (0..{len(ctx.pages) - 1})")
        url = ctx.pages[index].url
        page = ctx.pages[index]
        page.close()
        if getattr(self, "_active", None) is page:
            self._active = None
        return f"closed tab {index} ({url[:60]})"

    @_pinned
    def frames(self) -> list[dict]:
        """List scriptable frames so code can target embedded booking widgets."""
        out = []
        for index, frame in enumerate(self._frames()):
            try:
                title = frame.evaluate("() => document.title || ''")
            except Exception:
                title = ""
            out.append({
                "index": index,
                "name": frame.name or "",
                "url": frame.url,
                "title": str(title)[:120],
            })
        return out

    @_pinned
    def run_js(self, code: str, frame_index: int = 0) -> str:
        """Execute JavaScript in the page and return the result as JSON text.

        This is the code-mode reduction: compute dates, fill inputs, query the
        DOM, extract structured data — one call instead of a click-and-pray
        sequence. General by construction: the harness supplies no selectors and
        no task knowledge; the model writes whatever it needs.
        """
        if len(code) > 20_000:
            return _json_text({
                "ok": False,
                "error": f"script is too large ({len(code)} chars; limit is 20000)",
            })
        page = self._page()
        frames = self._frames()
        if not (0 <= frame_index < len(frames)):
            return _json_text({
                "ok": False,
                "error": f"frame {frame_index} out of range (0..{max(0, len(frames) - 1)})",
            })
        frame = frames[frame_index]
        pages_before = self._pages()
        try:
            frame.evaluate(GHOST_HELPERS_JS)
            result = self._evaluate_model_code(page, frame_index, code)
        except Exception as exc:
            self._adopt_new_page(pages_before)
            page = self._page()
            return _json_text({
                "ok": False,
                "frame": frame_index,
                "frame_url": frame.url,
                "page_url": page.url,
                "error": (
                    f"{type(exc).__name__}: {str(exc)[:800]}; "
                    f"script deadline is {self._script_timeout_ms}ms"
                ),
            })
        # A synthetic click/window.open can return from Runtime.evaluate a few
        # milliseconds before the new Page object is published on the CDP
        # connection. Check immediately, then allow one short registration
        # window; without it code-first opens the right tab but keeps acting on
        # the old one.
        if not self._adopt_new_page(pages_before):
            page.wait_for_timeout(150)
            self._adopt_new_page(pages_before)
        page = self._page()
        text = _json_text({
            "ok": True,
            "frame": frame_index,
            "frame_url": frame.url,
            "page_url": page.url,
            "result": result,
        })
        if len(text) > 6000:
            text = text[:6000] + f"... [TRUNCATED: {len(text)} chars total — return less from your script]"
        return text

    @_pinned
    def run_actions(self, actions: list[dict], frame_index: int = 0) -> str:
        """Execute a short ordered semantic/selector program through Playwright.

        JavaScript is ideal for DOM inspection and computation, but el.click()
        is an untrusted synthetic event and direct value assignment can bypass a
        framework's controlled state. These operations use Playwright's trusted
        actionability/input path. Accessible-name locators are preferred because
        they are re-resolved after each reactive render; CSS remains available
        for controls that expose no useful semantics.
        """
        page = self._page()
        self._initial_active_url = None
        frames = self._frames()
        if not (0 <= frame_index < len(frames)):
            return _json_text({
                "ok": False,
                "error": f"frame {frame_index} out of range (0..{max(0, len(frames) - 1)})",
            })
        if not actions:
            return _json_text({"ok": False, "error": "actions must not be empty"})
        if len(actions) > 20:
            return _json_text({"ok": False, "error": "at most 20 actions per program"})

        frame = frames[frame_index]
        pages_before = self._pages()
        completed = []
        for step, action in enumerate(actions):
            op = str(action.get("op") or "")
            selector = str(action.get("selector") or "")
            by = str(action.get("by") or ("css" if selector else ""))
            name = str(action.get("name") or "")
            role = str(action.get("role") or "")
            exact = bool(action.get("exact", False))
            index = int(action.get("index") or 0)
            try:
                if index < 0:
                    raise ValueError("index must be zero or greater")
                if op == "wait":
                    ms = min(5000, max(0, int(action.get("ms") or 500)))
                    page.wait_for_timeout(ms)
                    completed.append({"step": step, "op": op, "ms": ms})
                    continue
                if by == "css":
                    if not selector:
                        raise ValueError(f"selector required for {op} with by=css")
                    locator = frame.locator(selector)
                    target = {"by": by, "selector": selector}
                elif by == "role":
                    if not role:
                        raise ValueError("role required with by=role")
                    # For click, models sometimes put the target text in
                    # `value`; that field otherwise has no meaning. Accept it.
                    if not name and op == "click":
                        name = str(action.get("value") or "")
                    locator = (
                        frame.get_by_role(role, name=name, exact=exact)
                        if name else frame.get_by_role(role)
                    )
                    target = {"by": by, "role": role, "exact": exact}
                    if name:
                        target["name"] = name
                elif by == "label":
                    if not name and op == "click":
                        name = str(action.get("value") or "")
                    if not name:
                        raise ValueError("name required with by=label")
                    locator = frame.get_by_label(name, exact=exact)
                    target = {"by": by, "name": name, "exact": exact}
                elif by == "placeholder":
                    if not name:
                        raise ValueError("name required with by=placeholder")
                    locator = frame.get_by_placeholder(name, exact=exact)
                    target = {"by": by, "name": name, "exact": exact}
                elif by == "text":
                    if not name and op == "click":
                        name = str(action.get("value") or "")
                    if not name:
                        raise ValueError("name required with by=text")
                    locator = frame.get_by_text(name, exact=exact)
                    target = {"by": by, "name": name, "exact": exact}
                elif by == "testid":
                    if not name:
                        raise ValueError("name required with by=testid")
                    locator = frame.get_by_test_id(name)
                    target = {"by": by, "name": name}
                else:
                    raise ValueError(
                        f"target required for {op}: use by=role/label/placeholder/"
                        "text/testid, or by=css with selector"
                    )
                locator = locator.nth(index)
                if op == "click":
                    locator.click(timeout=8000)
                elif op == "fill":
                    locator.fill(str(action.get("value") or ""), timeout=8000)
                elif op == "select":
                    wanted = str(action.get("value") or "")
                    try:
                        locator.select_option(value=wanted, timeout=8000)
                    except Exception:
                        locator.select_option(label=wanted, timeout=8000)
                elif op == "check":
                    wanted = bool(action.get("checked", True))
                    if wanted:
                        locator.check(timeout=8000)
                    else:
                        locator.uncheck(timeout=8000)
                elif op == "press":
                    key = str(action.get("key") or "")
                    if not key:
                        raise ValueError("key required for press")
                    locator.press(key, timeout=8000)
                else:
                    raise ValueError(f"unknown operation {op!r}")
                try:
                    state = locator.evaluate("""el => ({
                      tag: el.tagName.toLowerCase(),
                      value: 'value' in el ? String(el.value).slice(0, 200) : '',
                      checked: 'checked' in el ? Boolean(el.checked) : undefined,
                      text: (el.innerText || '').trim().slice(0, 200)
                    })""")
                except Exception:
                    state = {"detached_after_action": True}
                completed.append({
                    "step": step, "op": op, **target,
                    "index": index, "state": state,
                })
                page.wait_for_timeout(min(1200, max(0, int(action.get("after_ms") or 250))))
            except Exception as exc:
                self._adopt_new_page(pages_before)
                page = self._page()
                return _json_text({
                    "ok": False,
                    "frame": frame_index,
                    "page_url": page.url,
                    "failed_step": step,
                    "failed_action": action,
                    "completed": completed,
                    "error": f"{type(exc).__name__}: {str(exc)[:800]}",
                })
        self._adopt_new_page(pages_before)
        page = self._page()
        return _json_text({
            "ok": True,
            "frame": frame_index,
            "page_url": page.url,
            "completed": completed,
        })

    @_pinned
    def find(self, query: str, els: list[dict]) -> list[dict]:
        """Filter the full element set by a substring of its visible text.

        Complements the display cap: the listing shows the first 120, but the
        control the task needs may be the 200th on a large page. This is a
        generic affordance -- the browser's own "find control" -- and carries no
        task-specific knowledge; it only matches text the page itself renders.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        handles = getattr(self, "_handles", []) or []
        hits, kept, dropped = [], [], []
        for i, e in enumerate(els):
            if (q in (e.get("label") or "").lower()
                    or q in (e.get("href") or "").lower()
                    or q in (e.get("role") or "").lower()
                    or q in (e.get("type") or "").lower()):
                hits.append(e)
                kept.append(handles[i] if i < len(handles) else None)
            elif i < len(handles):
                dropped.append(handles[i])
        # Filter the HANDLES alongside the descriptors. Actions target
        # self._handles[index], so returning a filtered descriptor list while
        # leaving handles aligned to the unfiltered one makes every click land
        # on the wrong element -- silently, and looking exactly like the model
        # choosing badly. Caught by tests/test_web_find.py, which clicks the
        # found index and checks the RIGHT element fired.
        self._dispose_handles(dropped)
        self._handles = kept
        return hits

    @_pinned
    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        """Scroll the page itself and let the caller re-list in the same turn.

        Doing this over CDP rather than a desktop wheel event means the page
        actually scrolls (not whatever happens to be under the cursor), and it
        pairs with a refreshed element list so one tool call does the work of two.
        """
        page = self._page()
        dy = 400 * max(1, amount) * (-1 if direction == "up" else 1)
        page.mouse.wheel(0, dy)
        page.wait_for_timeout(500)
        return f"scrolled {direction} by {abs(dy)}px"

    @_pinned
    def click(self, index: int, els: list[dict]) -> str:
        e = els[index]
        handle = (self._handles[index]
                  if getattr(self, "_handles", None) and index < len(self._handles) else None)
        page = self._page()
        pages_before = self._pages()
        if handle is not None:
            try:
                handle.scroll_into_view_if_needed(timeout=3000)
                handle.click(timeout=5000)
                page.wait_for_timeout(700)
                self._adopt_new_page(pages_before)
                return f"clicked #{index} ({e['tag']} '{e['label'][:40]}')"
            except Exception as exc:
                # A retained handle failing means the DOM moved or the element
                # detached. Clicking its old coordinates can hit an unrelated
                # control, and iframe coordinates are not page coordinates at
                # all. Surface the stale state so the agent re-lists instead.
                raise RuntimeError(
                    f"element #{index} became stale; call web_elements again: "
                    f"{str(exc)[:180]}"
                ) from exc
        page.mouse.click(e["x"], e["y"])
        page.wait_for_timeout(700)
        self._adopt_new_page(pages_before)
        return f"clicked #{index} by position ({e['tag']} '{e['label'][:40]}')"

    @_pinned
    def type_into(self, index: int, text: str, els: list[dict]) -> str:
        e = els[index]
        handle = (self._handles[index]
                  if getattr(self, "_handles", None) and index < len(self._handles) else None)
        page = self._page()
        if handle is not None:
            try:
                handle.scroll_into_view_if_needed(timeout=3000)
                handle.click(timeout=5000)
                try:
                    handle.fill("", timeout=3000)
                except Exception:
                    # contenteditable/custom role=textbox controls do not
                    # implement fill(), but do accept real keyboard input once
                    # focused.
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                handle.type(text, delay=15)
                page.wait_for_timeout(400)
                return f"typed into #{index} ({e['label'][:40]})"
            except Exception as exc:
                raise RuntimeError(
                    f"element #{index} became stale or cannot accept text; "
                    f"call web_elements again: {str(exc)[:180]}"
                ) from exc
        page.mouse.click(e["x"], e["y"])
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(text, delay=15)
        page.wait_for_timeout(400)
        return f"typed into #{index} by position ({e['label'][:40]})"

    @_pinned
    def navigate(self, url: str) -> str:
        """Go to a URL, tolerating redirects.

        A site that redirects (consent walls, locale routing, http->https) makes
        Playwright raise "interrupted by another navigation". That is normal
        browsing, not a failure, so report where we actually ended up instead of
        surfacing an error the agent will waste turns reacting to.
        """
        page = self._page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            message = str(exc)
            if "interrupted by another navigation" not in message and "ERR_ABORTED" not in message:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    return f"navigation to {url} failed: {message[:120]}"
        page.wait_for_timeout(900)
        try:
            return f"navigated to {page.url}"
        except Exception:
            return f"navigated to {url}"

    @_pinned
    def page_text(self, limit: int = 4000, query: str | None = None) -> str:
        """Page text, with truncation ANNOUNCED and an optional focus term.

        Same defect class as the element cap: cutting at 4000 characters and
        saying nothing means the model reads a partial page and reasonably
        believes it read all of it — then concludes the thing it needs is not
        there. On a long results page the answer is usually past the cut.

        `query` returns the windows around each match instead of the head of the
        document, which is how you actually find a value on a long page. It
        matches only text the page itself renders — no task knowledge.
        """
        text = self._page().evaluate(
            "() => document.body ? document.body.innerText : ''"
        ) or ""
        if query:
            q = query.lower()
            body = text.lower()
            windows, start, found = [], 0, 0
            while found < 10:
                i = body.find(q, start)
                if i < 0:
                    break
                windows.append(text[max(0, i - 300): i + 500])
                start = i + max(1, len(q))
                found += 1
            if not windows:
                return (f"[no occurrence of {query!r} in the page text; "
                        f"the page has {len(text)} characters]")
            return (f"[{found} match(es) for {query!r}, showing the text around each]\n\n"
                    + "\n---\n".join(windows))
        if len(text) <= limit:
            return text
        return (text[:limit]
                + f"\n\n[TRUNCATED: showing {limit} of {len(text)} characters. "
                  f"Call web_read with a query to search the rest — the part you "
                  f"need is often past this point.]")

    @_pinned
    def search(self, queries: list[str], result_limit: int = 5) -> str:
        """Search several public queries through a temporary browser tab.

        Multi-record research should not require one navigate/read/tab-cleanup
        cycle per query. This primitive is intentionally generic: it returns the
        result page's own title, link and nearby text, uses the same Chrome
        context as the episode, and restores the exact original active page.
        """
        cleaned = [str(query).strip() for query in queries if str(query).strip()]
        if not cleaned:
            raise ValueError("at least one non-empty search query is required")
        if len(cleaned) > 8:
            raise ValueError("at most 8 search queries are allowed per call")
        limit = max(1, min(8, int(result_limit)))
        original = self._page()
        context = original.context
        temporary = context.new_page()
        results: list[dict] = []
        try:
            for query in cleaned:
                item: dict = {"query": query, "results": []}
                try:
                    temporary.goto(
                        f"https://www.google.com/search?q={quote_plus(query)}",
                        wait_until="domcontentloaded", timeout=25_000,
                    )
                    temporary.wait_for_timeout(500)
                    item["search_url"] = temporary.url
                    item["results"] = temporary.evaluate(
                        """limit => {
                          const found = [];
                          const seen = new Set();
                          for (const heading of document.querySelectorAll('a h3')) {
                            const anchor = heading.closest('a');
                            const url = anchor?.href || '';
                            const title = (heading.innerText || heading.textContent || '')
                              .replace(/\\s+/g, ' ').trim();
                            if (!title || !/^https?:/i.test(url) || seen.has(url)) continue;
                            seen.add(url);
                            let container = anchor;
                            for (let i = 0; i < 4 && container?.parentElement; i++) {
                              container = container.parentElement;
                              if ((container.innerText || '').length > title.length + 30) break;
                            }
                            const nearby = (container?.innerText || '')
                              .replace(/\\s+/g, ' ').trim().slice(0, 500);
                            found.push({title, url, snippet: nearby});
                            if (found.length >= limit) break;
                          }
                          return found;
                        }""",
                        limit,
                    ) or []
                    if not item["results"]:
                        item["warning"] = (
                            "No ordinary search-result links were visible; the "
                            "engine may have shown a consent or challenge page."
                        )
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                results.append(item)
        finally:
            try:
                temporary.close()
            finally:
                self._active = original
                self._initial_active_url = None
                if not original.is_closed():
                    try:
                        original.bring_to_front()
                    except Exception:
                        pass
        return _json_text({"ok": True, "queries": results})

    @_pinned
    def read_pages(self, urls: list[str], text_limit: int = 2500) -> str:
        """Read several public pages in a temporary tab and restore task state."""
        if not urls:
            raise ValueError("at least one URL is required")
        if len(urls) > 8:
            raise ValueError("at most 8 URLs are allowed per call")
        validated = [_public_http_url(str(url)) for url in urls]
        limit = max(500, min(5000, int(text_limit)))
        original = self._page()
        context = original.context
        temporary = context.new_page()
        pages: list[dict] = []
        try:
            for requested_url in validated:
                item: dict = {"requested_url": requested_url}
                try:
                    temporary.goto(
                        requested_url, wait_until="domcontentloaded", timeout=25_000,
                    )
                    temporary.wait_for_timeout(350)
                    extracted = temporary.evaluate(
                        """limit => {
                          const text = (document.body?.innerText || '')
                            .replace(/\\u0000/g, '').trim();
                          return {
                            title: document.title || '',
                            url: location.href,
                            text: text.slice(0, limit),
                            total_characters: text.length,
                            truncated: text.length > limit,
                          };
                        }""",
                        limit,
                    )
                    item.update(extracted or {})
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                pages.append(item)
        finally:
            try:
                temporary.close()
            finally:
                self._active = original
                self._initial_active_url = None
                if not original.is_closed():
                    try:
                        original.bring_to_front()
                    except Exception:
                        pass
        return _json_text({"ok": True, "pages": pages})

    def close(self) -> None:
        try:
            self._pool.submit(self._close_impl).result(timeout=20)
        except Exception:
            logger.debug("web provider close failed", exc_info=True)
        finally:
            self._pool.shutdown(wait=False)

    def _close_impl(self) -> None:
        try:
            self._dispose_handles()
            self._handles = []
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            logger.debug("web provider close failed", exc_info=True)
        finally:
            self._browser = None
            self._pw = None
