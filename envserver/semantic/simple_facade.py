"""Three-operation, model-facing facade over the semantic kernel.

The policy model sees a text-rendered computer, not adapter/resource protocol.
All identities are episode-scoped capabilities. Surface letters follow a fresh
native identity only through one-to-one semantic continuity; ambiguous matches
fail closed. Qualified element numbers are never guessed across identities.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING
from urllib.parse import urlparse

from .protocol import ErrorCode, ProtocolError
from .state import canonical_fingerprint

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import SemanticRuntime


MAX_OUTPUT_CHARS = 10_000
DEFAULT_LIMIT = 60
MAX_LIMIT = 100
MAX_QUERY_PAGE_SIZE = 60
MAX_QUERY_CONTEXT_GROUP_SIZE = 12
PAGE_TEXT_CHUNK_CHARS = 720
MAX_PAGE_TEXT_CHARS = 500_000
MAX_ACTION_DELTA_CHARS = 640
MAX_ACTION_DELTA_PARTS = 4
LAUNCH_SETTLE_SECONDS = 8.0
LAUNCH_SETTLE_POLL_SECONDS = 0.25
_REFERENCE = re.compile(r"^[A-Z]+(?:[1-9][0-9]*)?$")
_CALC_REFERENCE = re.compile(
    r"^(?:(?P<sheet>[^!]+)!)?(?P<range>[A-Za-z]+[1-9][0-9]*(?::[A-Za-z]+[1-9][0-9]*)?)$"
)
_DING_SURFACE_TITLE = re.compile(r"^@!-?[0-9]+,-?[0-9]+;BDHF$")
_PAGE_TEXT_QUERY = re.compile(
    r"(?:\bpage\s+text\b|"
    r"\b(?:complete|entire|full|whole)\s+(?:web\s+)?page(?:\s+(?:body|content|text))?\b|"
    r"\b(?:complete|entire|full|whole)\s+(?:body|content|text)\s+"
    r"(?:of|from)\s+(?:the\s+)?(?:web\s+)?page\b)"
)
_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "can", "could", "current", "do", "does", "element", "elements",
    "for", "from", "in", "into", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "please",
    "surface", "that", "the", "these", "this", "those", "to", "was", "were",
    "which", "with", "would", "you", "your",
})
_QUERY_FIELD_HINTS = frozenset({"description", "name", "role", "text", "value"})
_SURFACE_ROLES = frozenset({
    "frame", "window", "dialog", "alert", "file chooser", "file chooser dialog",
})
_SURFACE_ROLE_PRIORITY = {
    "frame": 0,
    "window": 1,
    "dialog": 2,
    "alert": 3,
    "file chooser": 4,
    "file chooser dialog": 5,
}
_LOW_SIGNAL_ROLES = frozenset({
    "unknown", "generic", "section", "group", "panel", "filler", "separator",
})
_TEXT_ONLY_ROLES = frozenset({"label", "static text", "statictext"})
_CLICK_ROLES = frozenset({
    "button", "link", "menu item", "menuitem", "tab", "tree item", "treeitem",
    "check box", "checkbox", "radio button", "radio", "switch", "option",
    "combo box", "combobox", "list box", "listbox", "push button",
    "toggle button", "page tab", "hyperlink", "check menu item",
    "radio menu item", "list item", "table cell", "icon",
})
_INVOKE_ACTIONS = (
    "click", "press", "activate", "invoke", "open", "link open", "open link",
    "jump", "do default", "dodefault", "show menu",
)
_BROWSER_APPLICATIONS = frozenset({
    "chrome", "chromium", "chromium-browser", "google chrome", "google-chrome",
})
_LIBREOFFICE_APPLICATIONS = frozenset({
    "libreoffice", "libreoffice calc", "libreoffice impress",
    "libreoffice writer", "soffice",
})
_VLC_APPLICATIONS = frozenset({"vlc", "vlc media player"})
_EXISTING_PATH_CHOOSER_MODES = frozenset({
    "choose", "choose file", "choose folder", "open", "open file", "open folder",
    "select", "select existing", "select file", "select folder",
})
_DEFAULT_DESKTOP_APPLICATIONS = frozenset({
    "chrome", "chromium", "document viewer", "files", "gimp", "google chrome",
    "libreoffice calc", "libreoffice impress", "libreoffice writer", "settings",
    "terminal", "thunderbird", "visual studio code", "vlc media player",
})
_FRIENDLY_APP_NAMES = {
    "code": "Visual Studio Code",
    "evince": "Document Viewer",
    "gnome-control-center": "Settings",
    "gnome-terminal": "Terminal",
    "gnome-terminal-server": "Terminal",
    "google chrome": "Chrome",
    "google-chrome": "Chrome",
    "nautilus": "Files",
    "org.gnome.nautilus": "Files",
    "org.gnome.settings": "Settings",
    "vlc": "VLC",
}


def _letters(index: int) -> str:
    """Return A..Z, AA.. for a zero-based index."""

    if index < 0:
        raise ValueError("surface index must be non-negative")
    output = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        output = string.ascii_uppercase[remainder] + output
    return output


def _letter_rank(label: str) -> int:
    """Return the allocation order of an A..Z, AA.. surface label."""

    rank = 0
    for character in label:
        rank = rank * 26 + (ord(character) - ord("A") + 1)
    return rank


def _bounded_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    suffix = "\n… output truncated; narrow with query or within."
    return text[: MAX_OUTPUT_CHARS - len(suffix)] + suffix


def _clean_text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_content(value: Any, limit: int = 800) -> str:
    """Compact content while retaining line/tab structure as visible markers."""

    if value is None:
        return ""
    raw = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.replace("\t", " ⇥ ").split()) for line in raw.split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and not compact[-1]:
            continue
        compact.append(line)
    text = " ⏎ ".join(compact).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _query_token(value: str) -> str:
    """Normalize one search token without language- or application-specific aliases."""

    token = value.casefold().strip("_'’")
    # A tiny plural normalization covers common model phrasing (paper/papers,
    # service/services) without turning query matching into an opaque stemmer.
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _query_tokens(value: str) -> list[str]:
    # Identifiers and filenames are common semantic labels. Split the same
    # human-visible word boundaries a model naturally omits in prose:
    # snake_case, lowerCamel/PascalCase, acronym-to-word, and alpha<->digit.
    # Punctuation remains handled by the final Unicode word scan.
    segmented = value.replace("_", " ")
    segmented = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", segmented)
    segmented = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", segmented)
    segmented = re.sub(
        r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])",
        " ",
        segmented,
        flags=re.UNICODE,
    )
    return [
        token
        for raw in re.findall(r"\w+", segmented, flags=re.UNICODE)
        if (token := _query_token(raw))
    ]


def _states(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("states", record.get("state", {}))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _actions(record: Mapping[str, Any]) -> tuple[str, ...]:
    raw = record.get("advertised_actions") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(value) for value in raw if str(value))


def _normalized_action(value: Any) -> str:
    """Normalize native action spellings without changing their identity.

    AT-SPI applications use several equivalent separators (for example,
    ``link.open``, ``link-open``, and ``link_open``).  The facade retains the
    exact advertised name for dispatch, but compares a canonical spelling when
    deciding whether an element is honestly clickable.
    """

    return " ".join(re.sub(r"[-_.]+", " ", str(value).casefold()).split())


@dataclass
class _Surface:
    identity: str
    public_id: str
    app: str
    title: str
    role: str
    semantic_signature: tuple[str, str, str]
    ref: str | None
    resource: str | None
    active: bool
    modal: bool
    modified: bool
    busy: bool

    def label(self, *, include_id: bool = True) -> str:
        parts = [self.app, self.title]
        states = []
        if self.modal:
            states.append("modal")
        if self.modified:
            states.append("modified")
        if self.busy:
            states.append("busy")
        if self.active:
            states.append("active")
        cleaned: list[str] = []
        for part in (*parts, *states):
            value = _clean_text(part, 240)
            if value and value.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(value)
        prefix = f"[{self.public_id}] " if include_id else ""
        return prefix + " — ".join(cleaned or ["Untitled surface"])


@dataclass
class _Element:
    identity: str
    public_id: str
    ref: str
    tree_ref: str
    resource: str
    surface_identity: str
    role: str
    name: str
    text: str
    description: str
    value: Any
    states: dict[str, Any]
    actions: tuple[str, ...]
    parent_ref: str | None
    click_action: str | None
    click_arguments: dict[str, Any]
    type_action: str | None
    type_argument: str | None
    type_behavior: str | None
    metadata: dict[str, Any]


class SimpleComputerFacade:
    """Compile adapter state into stable surface/element capabilities."""

    def __init__(self, runtime: "SemanticRuntime") -> None:
        self.runtime = runtime
        self._surface_ids: dict[str, str] = {}
        self._next_surface_index = 0
        self._element_numbers: dict[str, dict[str, int]] = {}
        self._next_element_number: dict[str, int] = {}
        self._current_surfaces: dict[str, _Surface] = {}
        self._current_elements: dict[str, _Element] = {}
        self._surface_by_native_ref: dict[str, str] = {}
        self._cursors: dict[str, dict[str, Any]] = {}
        self._last_model_state: dict[str, Any] | None = None

    def _identity(self, record: Mapping[str, Any], *, include_resource: bool) -> str:
        public_ref = record.get("ref")
        if not isinstance(public_ref, str):
            return canonical_fingerprint(record)
        resolved = self.runtime.state.resolve_ref(public_ref)
        native = resolved.locator.get("native_ref")
        stable_virtual_identity = record.get("_simple_stable_identity")
        if isinstance(stable_virtual_identity, str) and stable_virtual_identity:
            base = f"{resolved.adapter_id}:stable-virtual:{stable_virtual_identity}"
        else:
            base = f"{resolved.adapter_id}:{native or canonical_fingerprint(resolved.locator)}"
        virtual_identity = record.get("_simple_identity")
        if (
            not stable_virtual_identity
            and isinstance(virtual_identity, str)
            and virtual_identity
        ):
            base = f"{base}:virtual:{virtual_identity}"
        return f"{base}:{resolved.resource}" if include_resource else base

    def _observe(
        self, resource: str, *, parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        adapter = self.runtime.registry.resolve(resource)
        records, _revision, _observation = self.runtime._observe(  # noqa: SLF001
            adapter,
            resource,
            {
                "resource": resource,
                "scope": {},
                "where": {},
                "fields": [],
                "order_by": [],
                "parameters": dict(parameters or {}),
                "limit": 100,
                "freshness": "live",
            },
            f"simple-read-{resource}",
        )
        return records

    def _try_observe(
        self, resource: str, *, parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        records, _available = self._try_observe_with_availability(
            resource, parameters=parameters,
        )
        return records

    def _try_observe_with_availability(
        self, resource: str, *, parameters: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return records and whether the resource query itself was supported.

        An empty successful observation is materially different from a missing
        adapter/resource.  Callers which have compatibility fallbacks need the
        distinction; ordinary optional-resource callers can keep using
        ``_try_observe``.
        """

        try:
            return self._observe(resource, parameters=parameters), True
        except ProtocolError as error:
            if error.code in {
                ErrorCode.UNKNOWN_RESOURCE,
                ErrorCode.UNSUPPORTED,
                ErrorCode.NOT_FOUND,
                ErrorCode.ADAPTER_UNAVAILABLE,
                ErrorCode.TIMEOUT,
                ErrorCode.REPRESENTATION_GAP,
            }:
                return [], False
            raise

    def _surface_id(self, identity: str) -> str:
        existing = self._surface_ids.get(identity)
        if existing is not None:
            return existing
        public_id = _letters(self._next_surface_index)
        self._next_surface_index += 1
        self._surface_ids[identity] = public_id
        self._element_numbers.setdefault(identity, {})
        self._next_element_number.setdefault(identity, 1)
        return public_id

    @staticmethod
    def _surface_signature(app: str, title: str, role: str) -> tuple[str, str, str]:
        return (
            _clean_text(app, 500).casefold(),
            _clean_text(title, 500).casefold(),
            _clean_text(role, 500).casefold(),
        )

    @staticmethod
    def _friendly_surface(app: str, title: str, role: str) -> tuple[str, str]:
        """Use stable product names and hide GNOME DING's coordinate title."""

        if (
            app.casefold() == "gjs"
            and role in {"frame", "window"}
            and _DING_SURFACE_TITLE.fullmatch(title)
        ):
            return "Desktop", "Desktop"
        normalized_app = app.casefold()
        normalized_title = title.casefold()
        if normalized_app in {"soffice", "libreoffice"}:
            if "libreoffice calc" in normalized_title:
                return "LibreOffice Calc", title
            if "libreoffice writer" in normalized_title:
                return "LibreOffice Writer", title
            if "libreoffice impress" in normalized_title:
                return "LibreOffice Impress", title
            return "LibreOffice", title
        return _FRIENDLY_APP_NAMES.get(normalized_app, app), title

    @staticmethod
    def _is_browser_surface(surface: _Surface) -> bool:
        return _clean_text(surface.app, 160).casefold() in _BROWSER_APPLICATIONS

    @staticmethod
    def _is_desktop_surface(surface: _Surface) -> bool:
        app = _clean_text(surface.app, 160).casefold()
        return app in {"desktop", "gnome-shell", "org.gnome.shell"}

    @staticmethod
    def _is_libreoffice_surface(surface: _Surface) -> bool:
        return _clean_text(surface.app, 160).casefold() in _LIBREOFFICE_APPLICATIONS

    @staticmethod
    def _is_vlc_surface(surface: _Surface) -> bool:
        return _clean_text(surface.app, 160).casefold() in _VLC_APPLICATIONS

    @staticmethod
    def _chooser_accepts_existing_path(record: Mapping[str, Any]) -> bool:
        mode = (
            _clean_text(record.get("mode"), 80).casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        mode = " ".join(mode.split())
        return mode in _EXISTING_PATH_CHOOSER_MODES

    @staticmethod
    def _requests_complete_page_text(query: str | None) -> bool:
        if not query:
            return False
        return _PAGE_TEXT_QUERY.search(_clean_text(query, 500).casefold()) is not None

    @staticmethod
    def _query_score(
        element: _Element, normalized_query: str,
    ) -> tuple[int, int, int] | None:
        """Rank one row for a compact, descriptive model query.

        A phrase contained in one semantic field is strongest. Otherwise the
        query is treated as a bag of meaningful tokens: low-information prose
        is ignored, partial coverage is allowed, and matches in names/text are
        preferred to incidental descriptions. The matcher is deliberately
        lexical and generic; it has no application, site, task, or synonym
        table.
        """

        fields = {
            "role": element.role.casefold(),
            "name": element.name.casefold(),
            "text": element.text.casefold(),
            "description": element.description.casefold(),
            "value": _clean_text(element.value).casefold(),
        }
        raw_tokens = _query_tokens(normalized_query)
        hints = {token for token in raw_tokens if token in _QUERY_FIELD_HINTS}
        if any(not fields[hint] for hint in hints):
            return None
        terms = list(dict.fromkeys(
            token for token in raw_tokens
            if token not in _QUERY_STOPWORDS and token not in _QUERY_FIELD_HINTS
        ))
        if not terms and not hints:
            return None

        exact_priority = max((
            priority
            for priority, field in (
                (5, fields["name"]), (4, fields["text"]),
                (3, fields["value"]), (2, fields["description"]),
                (1, fields["role"]),
            )
            if normalized_query and normalized_query in field
        ), default=0)
        field_tokens = {
            field: set(_query_tokens(value)) for field, value in fields.items()
        }
        if element.click_action:
            field_tokens["capability_click"] = {"click", "clickable", "activate"}
        if element.type_action:
            field_tokens["capability_type"] = {"type", "typing", "editable", "replace"}
        weights = {
            "name": 6,
            "text": 5,
            "value": 4,
            "role": 3,
            "description": 2,
            "capability_click": 1,
            "capability_type": 1,
        }
        matched = 0
        field_weight = 0
        for term in terms:
            matching_fields = [
                field for field, tokens in field_tokens.items() if term in tokens
            ]
            if matching_fields:
                matched += 1
                field_weight += max(weights[field] for field in matching_fields)
        if terms and matched == 0:
            return None
        # exact_priority is nonzero only for a literal phrase inside one field;
        # matched count then ranks partial descriptive queries deterministically.
        return exact_priority, matched, field_weight

    @staticmethod
    def _expand_query_context(
        elements: list[_Element], direct_matches: list[_Element],
    ) -> list[_Element]:
        """Add one bounded local semantic group around each ranked match.

        Search remains lexical: context never creates another match or a
        synthesized summary.  It only restores existing rows from the same
        semantic tree so a matched field is not detached from its record/card.
        Exact native objects and their already-issued public action IDs are
        retained.  Containers larger than the hard bound are never expanded.
        """

        by_ref = {element.tree_ref: element for element in elements}
        children: dict[str, list[_Element]] = {}
        for element in elements:
            if isinstance(element.parent_ref, str) and element.parent_ref in by_ref:
                children.setdefault(element.parent_ref, []).append(element)

        def bounded_group(root: _Element) -> list[_Element] | None:
            member_refs: set[str] = set()
            pending = [root]
            while pending:
                current = pending.pop()
                if current.tree_ref in member_refs:
                    continue
                member_refs.add(current.tree_ref)
                if len(member_refs) > MAX_QUERY_CONTEXT_GROUP_SIZE:
                    return None
                pending.extend(children.get(current.tree_ref, ()))
            # Adapter records are already in semantic tree order. Filtering
            # that original sequence is safer than inventing a traversal order.
            return [
                element for element in elements if element.tree_ref in member_refs
            ]

        output: list[_Element] = []
        included: set[str] = set()

        def meaningful(element: _Element) -> bool:
            return bool(
                element.name
                or element.text
                or element.description
                or element.value not in (None, "")
                or element.click_action
                or element.type_action
            )

        for match in direct_matches:
            context: list[_Element] | None = None
            # An actionable match can have implementation children (for
            # example a virtual alternate action) which are not its record
            # context.  Begin at its parent.  A structural match may itself be
            # the useful card/row, so let its own bounded subtree qualify.
            current: _Element | None = (
                match
                if not (match.click_action or match.type_action)
                and children.get(match.tree_ref)
                else by_ref.get(match.parent_ref)
                if isinstance(match.parent_ref, str)
                else None
            )
            seen: set[str] = set()
            while current is not None and current.tree_ref not in seen:
                seen.add(current.tree_ref)
                candidate = bounded_group(current)
                # Every ancestor above an oversized container necessarily
                # contains it, so it cannot become a valid small context.
                if candidate is None:
                    break
                # A wrapper containing only the lexical match adds no useful
                # information. Continue until the first small subtree adds at
                # least one real peer (title, description, metadata, or
                # action). Choosing the nearest such ancestor prevents a page
                # or collection root from absorbing unrelated sibling cards.
                if sum(1 for element in candidate if meaningful(element)) >= 2:
                    context = candidate
                    break
                current = (
                    by_ref.get(current.parent_ref)
                    if isinstance(current.parent_ref, str) else None
                )
            for element in context or [match]:
                if element.tree_ref in included:
                    continue
                included.add(element.tree_ref)
                output.append(element)
        return output

    @staticmethod
    def _page_text_chunks(text: Any) -> list[str]:
        remaining = _clean_content(text, MAX_PAGE_TEXT_CHARS)
        chunks: list[str] = []
        while remaining:
            if len(remaining) <= PAGE_TEXT_CHUNK_CHARS:
                chunks.append(remaining)
                break
            split_at = remaining.rfind(" ", 0, PAGE_TEXT_CHUNK_CHARS + 1)
            if split_at < PAGE_TEXT_CHUNK_CHARS // 2:
                split_at = PAGE_TEXT_CHUNK_CHARS
            chunk = remaining[:split_at].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_at:].strip()
        return chunks

    def _rebind_surface_identity(
        self, previous_identity: str, identity: str, public_id: str,
    ) -> None:
        """Move one proven semantic surface capability to its fresh identity."""

        if previous_identity == identity:
            return
        if self._surface_ids.get(previous_identity) != public_id:
            return
        self._surface_ids.pop(previous_identity, None)
        self._surface_ids[identity] = public_id
        numbers = self._element_numbers.pop(previous_identity, {})
        next_number = self._next_element_number.pop(previous_identity, 1)
        self._element_numbers[identity] = numbers
        self._next_element_number[identity] = next_number

    def _stable_surface_id(
        self,
        identity: str,
        signature: tuple[str, str, str],
        *,
        previous_by_signature: Mapping[tuple[str, str, str], list[_Surface]],
        current_signature_counts: Mapping[tuple[str, str, str], int],
        current_identities: set[str],
        claimed_public_ids: set[str],
    ) -> str:
        existing = self._surface_ids.get(identity)
        if existing is not None:
            # Candidate collection normally collapses repeated observations of
            # one native surface before this point.  If persisted identity
            # state is ever inconsistent, preserve both distinct surfaces by
            # assigning the later identity a fresh capability instead of
            # rendering the same public letter twice.
            if existing in claimed_public_ids:
                self._surface_ids.pop(identity, None)
                existing = self._surface_id(identity)
            claimed_public_ids.add(existing)
            return existing

        previous = previous_by_signature.get(signature, [])
        if current_signature_counts.get(signature) == 1 and len(previous) == 1:
            prior = previous[0]
            if (
                prior.identity not in current_identities
                and prior.public_id not in claimed_public_ids
            ):
                self._rebind_surface_identity(
                    prior.identity, identity, prior.public_id,
                )
                claimed_public_ids.add(prior.public_id)
                return prior.public_id

        public_id = self._surface_id(identity)
        claimed_public_ids.add(public_id)
        return public_id

    def _element_id(self, surface_identity: str, identity: str) -> str:
        numbers = self._element_numbers.setdefault(surface_identity, {})
        number = numbers.get(identity)
        if number is None:
            number = self._next_element_number.setdefault(surface_identity, 1)
            numbers[identity] = number
            self._next_element_number[surface_identity] = number + 1
        return f"{self._surface_id(surface_identity)}{number}"

    @staticmethod
    def _element_signature(element: _Element) -> str:
        """Fingerprint one rendered semantic row without transport identity.

        Native AT-SPI object paths can be recreated while the visible control
        remains unchanged.  Excluding refs and ancestry permits continuity in
        that case; including every model-visible field and action/state shape
        makes the fallback deliberately conservative.
        """

        return canonical_fingerprint({
            "resource": element.resource,
            "role": element.role,
            "name": element.name.casefold(),
            "text": element.text.casefold(),
            "description": element.description.casefold(),
            "value": element.value,
            "states": element.states,
            "actions": element.actions,
            "click_action": element.click_action,
            "click_arguments": element.click_arguments,
            "type_action": element.type_action,
            "type_argument": element.type_argument,
            "type_behavior": element.type_behavior,
            "metadata": element.metadata,
        })

    def _stabilize_element_ids(
        self,
        elements: list[_Element],
        *,
        surface_identity: str,
        identities_known_before_read: set[str],
    ) -> None:
        """Rebind a uniquely unchanged row after native identity churn.

        This never chooses among duplicates.  Existing native identities keep
        their prior capabilities, and a former ID is reused only when exactly
        one previous row and one current row share the complete semantic
        signature on the same public surface.
        """

        surface_id = self._surface_id(surface_identity)
        previous_by_signature: dict[str, list[_Element]] = {}
        for previous in self._current_elements.values():
            if re.fullmatch(
                rf"{re.escape(surface_id)}[1-9][0-9]*", previous.public_id,
            ):
                previous_by_signature.setdefault(
                    self._element_signature(previous), [],
                ).append(previous)
        current_by_signature: dict[str, list[_Element]] = {}
        for current in elements:
            current_by_signature.setdefault(
                self._element_signature(current), [],
            ).append(current)

        current_identities = {element.identity for element in elements}
        claimed_ids = {element.public_id for element in elements}
        numbers = self._element_numbers.setdefault(surface_identity, {})
        for signature, current_matches in current_by_signature.items():
            previous_matches = previous_by_signature.get(signature, [])
            if len(current_matches) != 1 or len(previous_matches) != 1:
                continue
            current = current_matches[0]
            previous = previous_matches[0]
            if current.identity == previous.identity:
                continue
            if current.identity in identities_known_before_read:
                continue
            if previous.identity in current_identities:
                continue
            match = re.fullmatch(rf"{re.escape(surface_id)}([1-9][0-9]*)", previous.public_id)
            if match is None:
                continue
            if previous.public_id in claimed_ids - {current.public_id}:
                continue
            previous_number = int(match.group(1))
            if numbers.get(previous.identity) != previous_number:
                continue
            numbers.pop(current.identity, None)
            numbers.pop(previous.identity, None)
            numbers[current.identity] = previous_number
            claimed_ids.discard(current.public_id)
            current.public_id = previous.public_id
            claimed_ids.add(current.public_id)

    @staticmethod
    def _surface_app_and_title(
        record: Mapping[str, Any], by_ref: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, str]:
        title = _clean_text(record.get("name") or record.get("text"), 240)
        app = ""
        parent = record.get("parent_ref")
        seen: set[str] = set()
        while isinstance(parent, str) and parent not in seen:
            seen.add(parent)
            ancestor = by_ref.get(parent)
            if ancestor is None:
                break
            if _clean_text(ancestor.get("role")).casefold() == "application":
                app = _clean_text(ancestor.get("name") or ancestor.get("text"), 160)
                break
            parent = ancestor.get("parent_ref")
        if not app:
            app = title or _clean_text(record.get("role"), 120).title() or "Application"
        return app, title or app

    def _compile_surfaces(
        self,
        surface_records: list[dict[str, Any]],
        browser_tabs: list[dict[str, Any]],
    ) -> tuple[list[_Surface], str | None]:
        by_ref = {
            str(record["ref"]): record
            for record in surface_records
            if isinstance(record.get("ref"), str)
        }

        candidates: list[
            tuple[dict[str, Any], str, dict[str, Any], str, str, str]
        ] = []
        for record in surface_records:
            role = _clean_text(record.get("role"), 80).casefold()
            if role not in _SURFACE_ROLES:
                continue
            state = _states(record)
            if state.get("visible") is False:
                continue
            if state.get("showing") is False and state.get("modal") is not True:
                continue
            try:
                identity = self._identity(record, include_resource=False)
            except ProtocolError:
                continue
            app, title = self._surface_app_and_title(record, by_ref)
            app, title = self._friendly_surface(app, title, role)
            candidates.append((record, role, state, identity, app, title))

        # Some apps expose only an application root. Keep it as a surface when
        # no window descendant was emitted.
        if not candidates:
            for record in surface_records:
                if _clean_text(record.get("role"), 80).casefold() != "application":
                    continue
                state = _states(record)
                if state.get("showing") is False or state.get("visible") is False:
                    continue
                identity = self._identity(record, include_resource=False)
                name = _clean_text(record.get("name") or record.get("text"), 240)
                candidates.append((
                    record,
                    "application",
                    state,
                    identity,
                    name or "Application",
                    name or "Application",
                ))

        # A native surface may be emitted more than once by an accessibility
        # walk or by overlapping legacy sources (for example os.windows and
        # os.dialogs).  Native identity is the only safe deduplication key:
        # titles are not identities, so two genuinely distinct windows with
        # the same title remain separate.  Preserve first-identity order for
        # stable public letters, choose the semantically richest representative
        # deterministically, and combine only boolean surface state belonging
        # to that exact native object.
        deduplicated: dict[
            str, tuple[dict[str, Any], str, dict[str, Any], str, str, str]
        ] = {}

        def candidate_rank(
            candidate: tuple[dict[str, Any], str, dict[str, Any], str, str, str],
        ) -> tuple[int, int, int, int, str, str, str, str]:
            record, role, state, _identity, app, title = candidate
            return (
                int(state.get("modal") is True),
                _SURFACE_ROLE_PRIORITY.get(role, -1),
                int(bool(state.get("active") or state.get("focused"))),
                len(title) + len(app),
                app.casefold(),
                title.casefold(),
                role,
                str(record.get("ref") or ""),
            )

        for candidate in candidates:
            identity = candidate[3]
            previous = deduplicated.get(identity)
            if previous is None:
                deduplicated[identity] = candidate
                continue
            preferred = max((previous, candidate), key=candidate_rank)
            merged_state = dict(preferred[2])
            for state in (previous[2], candidate[2]):
                for key in ("active", "focused", "modal", "modified", "busy"):
                    if state.get(key) is True:
                        merged_state[key] = True
            deduplicated[identity] = (
                preferred[0], preferred[1], merged_state,
                preferred[3], preferred[4], preferred[5],
            )
        candidates = list(deduplicated.values())

        previous_by_signature: dict[tuple[str, str, str], list[_Surface]] = {}
        for previous in self._current_surfaces.values():
            previous_by_signature.setdefault(
                previous.semantic_signature, [],
            ).append(previous)
        current_signature_counts: dict[tuple[str, str, str], int] = {}
        for _record, role, _state, _identity, app, title in candidates:
            signature = self._surface_signature(app, title, role)
            current_signature_counts[signature] = (
                current_signature_counts.get(signature, 0) + 1
            )
        current_identities = {
            identity for _record, _role, _state, identity, _app, _title in candidates
        }

        surfaces: list[_Surface] = []
        claimed_public_ids: set[str] = set()
        self._surface_by_native_ref = {}
        for record, role, state, identity, app, title in candidates:
            signature = self._surface_signature(app, title, role)
            ref = record.get("ref")
            surface = _Surface(
                identity=identity,
                public_id=self._stable_surface_id(
                    identity,
                    signature,
                    previous_by_signature=previous_by_signature,
                    current_signature_counts=current_signature_counts,
                    current_identities=current_identities,
                    claimed_public_ids=claimed_public_ids,
                ),
                app=app,
                title=title,
                role=role,
                semantic_signature=signature,
                ref=str(ref) if isinstance(ref, str) else None,
                resource="ui.surfaces",
                active=bool(state.get("active") or state.get("focused")),
                modal=bool(state.get("modal")),
                modified=bool(state.get("modified")),
                busy=bool(state.get("busy")),
            )
            surfaces.append(surface)
            if isinstance(ref, str):
                native_ref = self.runtime.state.resolve_ref(ref).locator.get("native_ref")
                if isinstance(native_ref, str):
                    self._surface_by_native_ref[native_ref] = identity

        active_tab = next((record for record in browser_tabs if record.get("active") is True), None)
        chrome_surface = next((
            surface for surface in surfaces if self._is_browser_surface(surface)
        ), None)
        if active_tab is not None:
            browser_title = _clean_text(active_tab.get("title"), 240)
            if chrome_surface is None:
                identity = "semantic-browser-window"
                chrome_surface = _Surface(
                    identity=identity,
                    public_id=self._surface_id(identity),
                    app="Chrome",
                    title=browser_title or "Browser",
                    role="window",
                    semantic_signature=self._surface_signature(
                        "Chrome", browser_title or "Browser", "window",
                    ),
                    ref=str(active_tab.get("ref")) if isinstance(active_tab.get("ref"), str) else None,
                    resource="browser.tabs",
                    active=not any(surface.active for surface in surfaces),
                    modal=False,
                    modified=False,
                    busy=False,
                )
                surfaces.append(chrome_surface)
            elif browser_title:
                chrome_surface.app = "Chrome"
                chrome_surface.title = browser_title

        # An explicitly active modal supersedes its owner. A merely showing
        # utility dialog does not hijack focus from the active window.
        active = next((surface for surface in reversed(surfaces) if surface.modal and surface.active), None)
        active = active or next((surface for surface in surfaces if surface.active), None)
        active = active or (surfaces[0] if len(surfaces) == 1 else None)
        # Surface letters remain stable and the complete surface list stays at
        # the top, but the active application's scene gets the scarce default
        # row budget first. Single-app tasks should not lose their form fields
        # merely because Desktop launchers were enumerated earlier.
        render_surfaces = sorted(surfaces, key=lambda surface: (not surface.active))
        for surface in render_surfaces:
            surface.active = surface is active
        surfaces.sort(key=lambda surface: _letter_rank(surface.public_id))
        return surfaces, active.identity if active else None

    def _surface_for_ui_record(
        self,
        record: Mapping[str, Any],
        by_ref: Mapping[str, Mapping[str, Any]],
        surface_identity_by_ref: Mapping[str, str],
    ) -> str | None:
        current: Mapping[str, Any] | None = record
        seen: set[str] = set()
        while current is not None:
            ref = current.get("ref")
            if isinstance(ref, str) and ref in surface_identity_by_ref:
                return surface_identity_by_ref[ref]
            parent = current.get("parent_ref")
            if not isinstance(parent, str) or parent in seen:
                return None
            seen.add(parent)
            current = by_ref.get(parent)
        return None

    @staticmethod
    def _virtual_record(
        record: Mapping[str, Any],
        *,
        role: str,
        name: str = "",
        text: str = "",
        value: Any = None,
        description: str = "",
        states: Mapping[str, Any] | None = None,
        identity: str | None = None,
        stable_identity: str | None = None,
        parent_ref: str | None = None,
        click_action: str | None = None,
        click_arguments: Mapping[str, Any] | None = None,
        type_action: str | None = None,
        type_argument: str = "value",
        type_behavior: str = "replace",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one model-friendly row with an exact private action binding."""

        output = {
            **dict(record),
            "role": role,
            "name": name,
            "text": text,
            "value": value,
            "description": description,
            "states": dict(states or {}),
            # Specialized adapter verbs are never guessed from this list.
            "advertised_actions": [],
            "parent_ref": parent_ref,
        }
        if identity:
            output["_simple_identity"] = identity
            output["_simple_tree_ref"] = f"virtual:{record.get('ref')}:{identity}"
        if stable_identity:
            output["_simple_stable_identity"] = stable_identity
            output["_simple_tree_ref"] = f"stable-virtual:{stable_identity}"
        if click_action:
            output["_simple_click_action"] = click_action
            output["_simple_click_arguments"] = dict(click_arguments or {})
        if type_action:
            output["_simple_type_action"] = type_action
            output["_simple_type_argument"] = type_argument
            output["_simple_type_behavior"] = type_behavior
        if metadata:
            output["_simple_metadata"] = dict(metadata)
        return output

    @staticmethod
    def _document_match(
        sessions: list[dict[str, Any]], active: _Surface,
    ) -> dict[str, Any] | None:
        title = active.title.casefold()
        matches = [
            record for record in sessions
            if _clean_text(record.get("title"), 500).casefold() in title
            or title in _clean_text(record.get("title"), 500).casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        return sessions[-1] if len(sessions) == 1 else None

    @staticmethod
    def _apply_document_identity_to_surface(
        surface: _Surface, document: Mapping[str, Any],
    ) -> None:
        """Give a LibreOffice window the friendly live document identity."""

        kind = _clean_text(document.get("document_type"), 80).casefold()
        title = _clean_text(document.get("title"), 240)
        surface.app = {
            "writer": "LibreOffice Writer",
            "calc": "LibreOffice Calc",
            "impress": "LibreOffice Impress",
        }.get(kind, "LibreOffice")
        if title:
            surface.title = title
        surface.modified = bool(document.get("modified"))

    def _enrich_libreoffice_surfaces(self, surfaces: list[_Surface]) -> None:
        """Fuse UNO document names into incomplete background window rows.

        AT-SPI can briefly expose a background LibreOffice window as only
        ``soffice`` while UNO already knows the open document.  Match explicit
        titles first, then fill a remaining one-to-one window/document pair.
        Ambiguous pairs stay untouched rather than receiving a guessed title.
        """

        office_surfaces = [
            surface for surface in surfaces if self._is_libreoffice_surface(surface)
        ]
        if not office_surfaces:
            return
        sessions = self._try_observe("document.sessions")
        if not sessions:
            return
        matched_sessions: set[int] = set()
        unmatched_surfaces: list[_Surface] = []
        for surface in office_surfaces:
            surface_title = surface.title.casefold()
            matches = [
                (index, document)
                for index, document in enumerate(sessions)
                if (title := _clean_text(document.get("title"), 500).casefold())
                and (title in surface_title or surface_title in title)
            ]
            if len(matches) == 1:
                index, document = matches[0]
                self._apply_document_identity_to_surface(surface, document)
                matched_sessions.add(index)
            else:
                unmatched_surfaces.append(surface)
        remaining_sessions = [
            document for index, document in enumerate(sessions)
            if index not in matched_sessions
        ]
        if len(unmatched_surfaces) == 1 and len(remaining_sessions) == 1:
            self._apply_document_identity_to_surface(
                unmatched_surfaces[0], remaining_sessions[0],
            )

    def _libreoffice_records(
        self, active: _Surface, query: str | None,
    ) -> list[tuple[dict[str, Any], str]]:
        sessions = self._try_observe("document.sessions")
        document = self._document_match(sessions, active)
        if document is None:
            return []
        kind = str(document.get("document_type") or "").casefold()
        title = _clean_text(document.get("title"), 240)
        if title:
            active.title = title
        active.app = {
            "writer": "LibreOffice Writer",
            "calc": "LibreOffice Calc",
            "impress": "LibreOffice Impress",
        }.get(kind, "LibreOffice")
        active.modified = bool(document.get("modified"))
        doc_description = " — ".join(value for value in (
            kind.title(), _clean_text(document.get("url"), 500),
        ) if value)
        records: list[tuple[dict[str, Any], str]] = [(
            self._virtual_record(
                document,
                role="document",
                name=title or "Untitled document",
                description=doc_description,
                states={"modified": active.modified},
            ),
            "document.sessions",
        )]

        # Saving an already located live document is a universal LibreOffice
        # completion action, not an app/task recipe. Use document.state as the
        # authority so stale session metadata cannot hide or retain the button.
        live_state = self._document_match(
            self._try_observe("document.state"), active,
        )
        if live_state is not None:
            active.modified = bool(live_state.get("modified"))
            has_location = bool(
                live_state.get("has_location")
                if "has_location" in live_state
                else live_state.get("url")
            )
            if (
                active.modified
                and has_location
                and live_state.get("read_only") is not True
                and "save" in {
                    _normalized_action(action) for action in _actions(live_state)
                }
            ):
                records.append((self._virtual_record(
                    live_state,
                    role="button",
                    name="Save document",
                    description="Save current changes to the existing file",
                    stable_identity=(
                        "document-save:"
                        + kind + ":" + str(live_state.get("url") or title)
                    ),
                    click_action="save",
                ), "document.state"))

        if kind == "writer":
            paragraphs = self._try_observe("writer.paragraphs")
            # Writer's native UNO bridge can insert exact paragraph objects at
            # document end relative to any current paragraph.  Present that as
            # one stable, low-context insertion point rather than asking the
            # model to infer a specialized adapter verb from every paragraph.
            # The private target follows the live last paragraph after each
            # read; the public identity deliberately remains stable.
            if paragraphs:
                records.append((self._virtual_record(
                    paragraphs[-1],
                    role="input",
                    name="Document end",
                    description="Append paragraphs without replacing existing text",
                    stable_identity="writer:document-end",
                    type_action="simple_writer_insert_end",
                    type_behavior="insert",
                    metadata={"kind": "writer_document_end"},
                ), "writer.paragraphs"))
            for paragraph in paragraphs:
                index = int(paragraph.get("index") or 0)
                style = _clean_text(paragraph.get("style"), 160)
                role = "heading" if style.casefold().startswith("heading") else "paragraph"
                records.append((self._virtual_record(
                    paragraph,
                    role=role,
                    name=style or f"Paragraph {index + 1}",
                    text=str(paragraph.get("text") or ""),
                    description=(
                        f"paragraph {index + 1}"
                        + (f" — alignment {paragraph.get('alignment')}" if paragraph.get("alignment") is not None else "")
                    ),
                    type_action="replace_text",
                    type_argument="text",
                    metadata={"kind": "writer_paragraph", "index": index},
                ), "writer.paragraphs"))
            for table in self._try_observe("writer.tables"):
                table_ref = str(table.get("ref") or "")
                records.append((self._virtual_record(
                    table,
                    role="table",
                    name=_clean_text(table.get("name"), 240) or "Table",
                    value=f"{int(table.get('cell_count') or 0)} cells",
                    identity="table",
                ), "writer.tables"))
                for cell in table.get("cells") or []:
                    if not isinstance(cell, Mapping):
                        continue
                    cell_name = _clean_text(cell.get("name"), 80)
                    records.append((self._virtual_record(
                        table,
                        role="cell",
                        name=cell_name,
                        text=str(cell.get("text") or ""),
                        identity=f"cell:{cell_name}",
                        parent_ref=f"virtual:{table_ref}:table" if table_ref else None,
                        type_action="set_table_cell",
                        type_argument="text",
                        metadata={"static_arguments": {"cell": cell_name}},
                    ), "writer.tables"))
        elif kind == "calc":
            for sheet in self._try_observe("spreadsheet.sheets"):
                sheet_name = _clean_text(sheet.get("name"), 240)
                sheet_index = int(sheet.get("index") or 0)
                records.append((self._virtual_record(
                    sheet,
                    role="sheet",
                    name=sheet_name,
                    description=f"sheet {sheet_index + 1}",
                    states={"selected": sheet.get("active") is True},
                    stable_identity=f"sheet:{sheet_index}:{sheet_name.casefold()}",
                ), "spreadsheet.sheets"))
            parameters: dict[str, Any] = {}
            resource = "spreadsheet.selection"
            if query:
                match = _CALC_REFERENCE.fullmatch(query.strip())
                if match:
                    resource = "spreadsheet.cells"
                    parameters["range"] = match.group("range").upper()
                    if match.group("sheet"):
                        parameters["sheet"] = match.group("sheet")
            for cell in self._try_observe(resource, parameters=parameters):
                address = _clean_text(cell.get("address"), 80)
                sheet_name = _clean_text(cell.get("sheet"), 160)
                formula = str(cell.get("formula") or "")
                display = str(cell.get("display") or "")
                value = formula if formula.startswith("=") else display
                if not value and cell.get("value") not in (None, 0, 0.0, ""):
                    value = cell.get("value")
                records.append((self._virtual_record(
                    cell,
                    role="cell",
                    name=f"{sheet_name} {address}".strip(),
                    value=value,
                    description=" — ".join(value for value in (
                        _clean_text(cell.get("style"), 120),
                        f"number format {cell.get('number_format')}" if cell.get("number_format") is not None else "",
                    ) if value),
                    type_action="simple_calc_type",
                    type_argument="value",
                    type_behavior=(
                        "replace-or-grid(tabs=columns,newlines=rows,start=here)"
                    ),
                    stable_identity=f"cell:{sheet_name.casefold()}:{address.upper()}",
                    metadata={"kind": "calc_cell", "sheet": sheet_name, "address": address},
                ), resource))
        elif kind == "impress":
            for slide in self._try_observe("presentation.slides"):
                index = int(slide.get("index") or 0)
                records.append((self._virtual_record(
                    slide,
                    role="slide",
                    name=_clean_text(slide.get("name"), 240) or f"Slide {index + 1}",
                    value=f"{int(slide.get('shape_count') or 0)} objects",
                    description=f"slide {index + 1} — layout {slide.get('layout')}",
                ), "presentation.slides"))
            for shape in self._try_observe("presentation.shapes"):
                text = str(shape.get("text") or "")
                if not text and query:
                    continue
                slide_index = int(shape.get("slide_index") or 0)
                shape_index = int(shape.get("shape_index") or 0)
                records.append((self._virtual_record(
                    shape,
                    role="text" if text else "object",
                    name=_clean_text(shape.get("name"), 160) or f"Slide {slide_index + 1} object {shape_index + 1}",
                    text=text,
                    description=_clean_text(shape.get("shape_type"), 240),
                    type_action="replace_text" if text else None,
                    type_argument="text",
                    metadata={"kind": "impress_shape", "slide": slide_index, "shape": shape_index},
                ), "presentation.shapes"))
        return records

    def _vlc_records(self) -> list[tuple[dict[str, Any], str]]:
        output: list[tuple[dict[str, Any], str]] = []
        for player in self._try_observe("vlc.playback"):
            ref = str(player.get("ref") or "")
            identity = _clean_text(player.get("identity"), 160) or "VLC"
            title = _clean_text(player.get("title"), 300)
            artists = player.get("artists") or []
            artist_text = ", ".join(map(str, artists)) if isinstance(artists, (list, tuple)) else str(artists)
            value = (
                f"{player.get('playback_status') or 'unknown'}; "
                f"{float(player.get('position_seconds') or 0):.1f}/{float(player.get('duration_seconds') or 0):.1f}s; "
                f"volume {float(player.get('volume') or 0):.2f}; "
                f"loop {player.get('loop') or 'none'}; shuffle {bool(player.get('shuffle'))}"
            )
            output.append((self._virtual_record(
                player, role="media", name=title or identity,
                text=artist_text, value=value, identity="player",
            ), "vlc.playback"))
            if player.get("can_control") is not False:
                for action in ("play", "pause", "stop"):
                    output.append((self._virtual_record(
                        player, role="button", name=action.title(),
                        identity=f"action:{action}", parent_ref=f"virtual:{ref}:player" if ref else None,
                        click_action=action,
                    ), "vlc.playback"))
                output.append((self._virtual_record(
                    player, role="switch", name="Shuffle",
                    value=bool(player.get("shuffle")), identity="field:shuffle",
                    parent_ref=f"virtual:{ref}:player" if ref else None, click_action="set_shuffle",
                    click_arguments={"enabled": not bool(player.get("shuffle"))},
                ), "vlc.playback"))
            output.append((self._virtual_record(
                player, role="input", name="Volume (0–2)",
                value=player.get("volume"), identity="field:volume", parent_ref=f"virtual:{ref}:player" if ref else None,
                type_action="set_volume", type_argument="volume",
                metadata={"parse": "number"},
            ), "vlc.playback"))
            if player.get("can_seek") is True:
                output.append((self._virtual_record(
                    player, role="input", name="Position (seconds)",
                    value=player.get("position_seconds"), identity="field:position",
                    parent_ref=f"virtual:{ref}:player" if ref else None, type_action="seek",
                    type_argument="position_seconds", metadata={"parse": "number"},
                ), "vlc.playback"))
        for entry in self._try_observe("vlc.playlist"):
            output.append((self._virtual_record(
                entry, role="list item",
                name=_clean_text(entry.get("title"), 300) or f"Track {int(entry.get('position') or 0) + 1}",
                text=", ".join(map(str, entry.get("artists") or [])),
                description=_clean_text(entry.get("url"), 500),
            ), "vlc.playlist"))
        return output

    def _chrome_native_records(
        self, active_tab: Mapping[str, Any] | None,
    ) -> list[tuple[dict[str, Any], str]]:
        if active_tab is None:
            return []
        url = str(active_tab.get("url") or "").casefold()
        resource = next((value for marker, value in (
            ("chrome://bookmarks", "chrome.bookmarks"),
            ("chrome://settings", "chrome.settings"),
            ("chrome://history", "chrome.history"),
            ("chrome://extensions", "chrome.extensions"),
            ("chrome://downloads", "chrome.downloads"),
        ) if marker in url), None)
        if resource is None:
            return []
        output: list[tuple[dict[str, Any], str]] = []
        for record in self._try_observe(resource):
            if resource == "chrome.bookmarks":
                output.append((self._virtual_record(
                    record,
                    role="folder" if record.get("folder") else "bookmark",
                    name=_clean_text(record.get("title"), 300) or "Untitled bookmark",
                    description=_clean_text(record.get("url"), 500),
                ), resource))
            elif resource == "chrome.settings":
                output.append((self._virtual_record(
                    record, role="setting", name=_clean_text(record.get("key"), 300),
                    value=record.get("value"),
                    description=_clean_text(record.get("controlled_by") or record.get("enforcement"), 240),
                    type_action="set_pref", type_argument="value",
                    metadata={"parse": str(record.get("type") or "auto")},
                ), resource))
            elif resource == "chrome.extensions":
                enabled = bool(record.get("enabled"))
                output.append((self._virtual_record(
                    record, role="extension", name=_clean_text(record.get("name"), 300),
                    value=f"version {record.get('version')}; enabled {enabled}",
                    description=_clean_text(record.get("path") or record.get("install_type"), 500),
                    click_action="disable_extension" if enabled else "enable_extension",
                ), resource))
            elif resource == "chrome.history":
                output.append((self._virtual_record(
                    record, role="history item", name=_clean_text(record.get("title"), 300),
                    description=_clean_text(record.get("url"), 500),
                    value=record.get("last_visit_time"),
                ), resource))
            else:
                output.append((self._virtual_record(
                    record, role="download", name=_clean_text(record.get("filename"), 500),
                    description=_clean_text(record.get("url"), 500),
                    value=record.get("state"),
                ), resource))
        return output

    def _browser_page_text_records(
        self, active: _Surface, query: str | None,
    ) -> list[tuple[dict[str, Any], str]]:
        if (
            not self._is_browser_surface(active)
            or not self._requests_complete_page_text(query)
        ):
            return []
        output: list[tuple[dict[str, Any], str]] = []
        for record in self._try_observe("browser.text"):
            chunks = self._page_text_chunks(record.get("text"))
            url = _clean_text(record.get("url"), 500)
            total = len(chunks)
            for index, chunk in enumerate(chunks):
                output.append((self._virtual_record(
                    record,
                    role="page text",
                    name=f"Page text {index + 1}/{total}",
                    text=chunk,
                    description=url,
                    identity=f"page-text:{index}",
                ), "browser.text"))
        return output

    def _browser_navigation_records(
        self, active_tab: Mapping[str, Any] | None,
    ) -> list[tuple[dict[str, Any], str]]:
        """Expose browser chrome through the same click/type vocabulary."""

        if active_tab is None or not isinstance(active_tab.get("ref"), str):
            return []
        ref = str(active_tab["ref"])
        output: list[tuple[dict[str, Any], str]] = [(
            self._virtual_record(
                active_tab,
                role="input",
                name="Address",
                value=_clean_text(active_tab.get("url"), 500),
                identity="browser-address",
                type_action="navigate",
                type_argument="url",
            ),
            "browser.tabs",
        )]
        for action, name in (
            ("back", "Back"),
            ("forward", "Forward"),
            ("reload", "Reload"),
            ("open_tab", "New tab"),
        ):
            output.append((self._virtual_record(
                active_tab,
                role="button",
                name=name,
                identity=f"browser-action:{action}",
                click_action=action,
                parent_ref=f"virtual:{ref}:browser-address",
            ), "browser.tabs"))
        return output

    @staticmethod
    def _external_web_link(record: Mapping[str, Any]) -> tuple[str, str] | None:
        """Return the exact label/URI for one proved native-app web link."""

        role = _clean_text(record.get("role"), 80).casefold()
        if role not in {"link", "hyperlink"}:
            return None
        uri = _clean_text(record.get("url"), 4_096)
        parsed = urlparse(uri)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not isinstance(record.get("ref"), str)
        ):
            return None
        label = _clean_text(
            record.get("name") or record.get("text") or parsed.hostname,
            300,
        )
        return label, uri

    def _external_browser_link_records(
        self,
        active: _Surface,
        ui_records: list[dict[str, Any]],
        browser_tabs: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str]]:
        """Expose explicit new-tab actions for native-app HTTP(S) links.

        The ordinary AT-SPI link remains available and retains its native
        default behavior.  This second row exists because "open" and "open in
        a new browser tab while preserving existing tabs" are different user
        intents.  It is emitted only when both the source URI and a live Chrome
        tab target are proved; otherwise the facade does not pretend the
        stronger operation is available.
        """

        if self._is_browser_surface(active) or not browser_tabs:
            return []
        target_tab = next(
            (record for record in browser_tabs if record.get("active") is True),
            browser_tabs[0],
        )
        if not isinstance(target_tab.get("ref"), str):
            return []
        output: list[tuple[dict[str, Any], str]] = []
        for record in ui_records:
            link = self._external_web_link(record)
            if link is None:
                continue
            label, uri = link
            source_ref = str(record["ref"])
            output.append((self._virtual_record(
                target_tab,
                role="button",
                name=f"Open in new Chrome tab: {label}",
                description=uri,
                identity="external-web-link-new-tab",
                stable_identity=(
                    "external-web-link-new-tab:"
                    + canonical_fingerprint({
                        "source_ref": source_ref,
                        "uri": uri,
                    })[:24]
                ),
                parent_ref=source_ref,
                click_action="open_tab",
                click_arguments={"url": uri},
            ), "browser.tabs"))
        return output

    def _desktop_application_records(
        self, query: str | None,
    ) -> list[tuple[dict[str, Any], str]]:
        """Expose installed launchers as ordinary Desktop click capabilities.

        With a three-tool public surface there is intentionally no separate
        launch primitive.  A Desktop read must therefore expose the same
        semantic affordance a human gets from the application grid.  The
        unfiltered view stays small; an explicit query searches every
        installed, non-hidden desktop entry.
        """

        entries = [
            record for record in self._try_observe("os.desktop_entries")
            if record.get("hidden") is not True
            and isinstance(record.get("desktop_id"), str)
            and _clean_text(record.get("name"), 240)
        ]
        by_name: dict[str, dict[str, Any]] = {}
        for record in entries:
            name = _clean_text(record.get("name"), 240)
            by_name.setdefault(name.casefold(), record)
        if query:
            selected = list(by_name.values())
        else:
            selected = [
                record for name, record in by_name.items()
                if name in _DEFAULT_DESKTOP_APPLICATIONS
            ]
            # A nonstandard image should still expose a bounded application
            # grid rather than a mysteriously empty Desktop.
            if not selected:
                selected = list(by_name.values())[:20]
        output: list[tuple[dict[str, Any], str]] = []
        for record in sorted(
            selected, key=lambda value: _clean_text(value.get("name"), 240).casefold(),
        ):
            desktop_id = str(record["desktop_id"])
            output.append((self._virtual_record(
                record,
                role="application",
                name=_clean_text(record.get("name"), 240),
                identity=f"desktop-entry:{desktop_id}",
                click_action="launch",
                click_arguments={"desktop_id": desktop_id},
            ), "os.desktop_entries"))
        return output

    def _specialized_records(
        self,
        active: _Surface | None,
        *,
        query: str | None,
        browser_tabs: list[dict[str, Any]],
        ui_records: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str]]:
        if active is None:
            return []
        output: list[tuple[dict[str, Any], str]] = []
        if self._is_desktop_surface(active):
            output.extend(self._desktop_application_records(query))
        if self._is_libreoffice_surface(active):
            output.extend(self._libreoffice_records(active, query))
        if self._is_vlc_surface(active):
            output.extend(self._vlc_records())
        if active.modal or active.role in {"file chooser", "file chooser dialog"}:
            choosers = self._try_observe("os.file_choosers")
            if len(choosers) == 1:
                chooser = choosers[0]
                if self._chooser_accepts_existing_path(chooser):
                    output.append((self._virtual_record(
                        chooser, role="input", name="Choose exact guest path",
                        type_action="choose_path", type_argument="path",
                    ), "os.file_choosers"))
                else:
                    output.append((self._virtual_record(
                        chooser,
                        role="note",
                        name="Exact path shortcut unavailable",
                        description=(
                            "This chooser does not prove an existing-path selection mode; "
                            "use its visible fields and buttons."
                        ),
                    ), "os.file_choosers"))
        active_tab = next((record for record in browser_tabs if record.get("active") is True), None)
        if self._is_browser_surface(active):
            output.extend(self._browser_navigation_records(active_tab))
            output.extend(self._chrome_native_records(active_tab))
            output.extend(self._browser_page_text_records(active, query))
        else:
            output.extend(self._external_browser_link_records(
                active, ui_records, browser_tabs,
            ))
        return output

    @staticmethod
    def _action_shape(
        record: Mapping[str, Any], resource: str,
    ) -> tuple[str | None, dict[str, Any], str | None, str | None]:
        role = _clean_text(record.get("role"), 80).casefold()
        advertised = _actions(record)
        normalized_actions = {_normalized_action(value): value for value in advertised}
        state = _states(record)
        disabled = state.get("enabled") is False or state.get("disabled") is True
        read_only = state.get("read_only") is True or state.get("readonly") is True
        click_action: str | None = None
        click_arguments: dict[str, Any] = {}
        if disabled:
            pass
        elif resource == "browser.tabs" and not record.get("_simple_identity"):
            click_action = "switch_tab"
        elif resource == "browser.elements":
            if "invoke" in normalized_actions:
                click_action = "invoke"
            elif any(value in normalized_actions for value in ("toggle", "check", "uncheck")):
                click_action = "toggle"
            elif state.get("expanded") is True and "collapse" in normalized_actions:
                click_action = "collapse"
            elif state.get("expanded") is False and "expand" in normalized_actions:
                click_action = "expand"
            elif (
                role not in _TEXT_ONLY_ROLES
                and role not in {"root web area", "web area"}
                and any(
                    _clean_text(record.get(field), 500)
                    for field in ("name", "text", "value")
                )
            ):
                # A webpage is still directly clickable even when its AX tree
                # omits an Invoke action. Modern component frameworks commonly
                # attach pointer handlers to otherwise-generic DOM nodes (for
                # example calendar days rendered as ``generic \"10\"``). The
                # browser adapter resolves the exact backend node and invokes
                # it; no selector or coordinate crosses the public interface.
                click_action = "invoke"
        else:
            invoke_name = next(
                (normalized_actions[value] for value in _INVOKE_ACTIONS if value in normalized_actions),
                None,
            )
            if invoke_name is not None:
                click_action = "invoke"
                click_arguments = {"advertised_action": invoke_name}
            elif "toggle" in normalized_actions:
                click_action = "toggle"
                click_arguments = {"advertised_action": normalized_actions["toggle"]}
            elif state.get("checked") is True and "uncheck" in normalized_actions:
                click_action = "uncheck"
                click_arguments = {"advertised_action": normalized_actions["uncheck"]}
            elif state.get("checked") is False and "check" in normalized_actions:
                click_action = "check"
                click_arguments = {"advertised_action": normalized_actions["check"]}
            elif state.get("expanded") is True and "collapse" in normalized_actions:
                click_action = "collapse"
                click_arguments = {"advertised_action": normalized_actions["collapse"]}
            elif state.get("expanded") is False and "expand" in normalized_actions:
                click_action = "expand"
                click_arguments = {"advertised_action": normalized_actions["expand"]}
            elif "dismiss" in normalized_actions:
                click_action = "dismiss"
                click_arguments = {"advertised_action": normalized_actions["dismiss"]}
            elif not advertised and role in _CLICK_ROLES:
                # Direct AT-SPI Action is absent. The guest may still prove a
                # private semantic hit-test for an intrinsically interactive
                # role, but it will fail closed rather than execute another
                # unrelated advertised action.
                click_action = "invoke"

        type_action: str | None = None
        if not disabled and not read_only:
            if (
                "select option" in normalized_actions
                and role in {"combo box", "combobox", "list box", "listbox"}
            ):
                type_action = "select_option"
            elif "replace text" in normalized_actions:
                type_action = "replace_text"
            elif "set text" in normalized_actions:
                type_action = "set_text"
            elif "insert text" in normalized_actions:
                type_action = "insert_text"
            elif "set value" in normalized_actions:
                type_action = "set_value"
            elif role in {"slider", "spin button", "spinbutton"} and isinstance(
                record.get("value"), Mapping,
            ):
                # `_value` emits this structure only after AT-SPI Value was
                # successfully queried, so set_value is a proved interface.
                type_action = "set_value"
        behavior = None
        if type_action:
            behavior = "insert" if type_action == "insert_text" else "replace"
        return click_action, click_arguments, type_action, behavior

    @staticmethod
    def _enforce_action_state(
        record: Mapping[str, Any],
        click_action: str | None,
        type_action: str | None,
    ) -> tuple[str | None, str | None]:
        """Apply current state after native and virtual action overrides."""

        state = _states(record)
        disabled = state.get("enabled") is False or state.get("disabled") is True
        read_only = state.get("read_only") is True or state.get("readonly") is True
        if disabled:
            return None, None
        if read_only:
            return click_action, None
        return click_action, type_action

    def _compile_elements(
        self,
        *,
        ui_records: list[dict[str, Any]],
        browser_tabs: list[dict[str, Any]],
        browser_elements: list[dict[str, Any]],
        virtual_records_by_surface: Mapping[
            str, list[tuple[dict[str, Any], str]]
        ],
        surfaces: list[_Surface],
        preserve_context_containers: bool = False,
        include_not_showing: bool = False,
    ) -> list[_Element]:
        if not surfaces:
            return []
        surface_identities = {surface.identity for surface in surfaces}
        identities_known_before_read = {
            identity: set(self._element_numbers.get(identity, {}))
            for identity in surface_identities
        }
        by_ref = {
            str(record["ref"]): record
            for record in ui_records
            if isinstance(record.get("ref"), str)
        }
        surface_identity_by_ref: dict[str, str] = {}
        for record in ui_records:
            role = _clean_text(record.get("role"), 80).casefold()
            if role not in _SURFACE_ROLES or not isinstance(record.get("ref"), str):
                continue
            try:
                surface_identity_by_ref[str(record["ref"])] = self._identity(
                    record, include_resource=False,
                )
            except ProtocolError:
                continue
        chrome_surface = next((
            surface for surface in surfaces if self._is_browser_surface(surface)
        ), None)

        candidates: list[tuple[dict[str, Any], str, str]] = []
        # Browser AX has cleaner page semantics than the Chrome accessibility
        # mirror, so it wins only for true cross-source duplicates. AT-SPI is
        # still retained for toolbar, menus, internal pages, and native dialogs.
        if chrome_surface is not None:
            candidates.extend(
                (record, "browser.tabs", chrome_surface.identity)
                for record in browser_tabs
            )
            candidates.extend(
                (record, "browser.elements", chrome_surface.identity)
                for record in browser_elements
            )
        for surface_identity, virtual_records in virtual_records_by_surface.items():
            if surface_identity not in surface_identities:
                continue
            candidates.extend(
                (record, resource, surface_identity)
                for record, resource in virtual_records
            )
        for record in ui_records:
            role = _clean_text(record.get("role"), 80).casefold()
            if role in _SURFACE_ROLES or role == "application":
                continue
            surface_identity = self._surface_for_ui_record(
                record, by_ref, surface_identity_by_ref,
            )
            if surface_identity in surface_identities:
                candidates.append((record, "ui.elements", surface_identity))

        output: list[_Element] = []
        raw_by_ref = {
            str(record.get("_simple_tree_ref") or record["ref"]): record
            for record, _resource, _surface_identity in candidates
            if isinstance(record.get("ref"), str)
        }
        structural_parent_refs = {
            str(record["parent_ref"])
            for record, _resource, _surface_identity in candidates
            if isinstance(record.get("parent_ref"), str)
            and str(record["parent_ref"]) in raw_by_ref
        }
        actionable_texts: dict[str, set[str]] = {}
        for record, resource, surface_identity in candidates:
            role = _clean_text(record.get("role") or record.get("kind"), 80).casefold()
            name = _clean_text(
                record.get("title")
                if resource == "browser.tabs" and not record.get("_simple_identity")
                else record.get("name"),
                500,
            )
            text = _clean_content(record.get("text"), 800)
            click_action, _click_arguments, type_action, _behavior = self._action_shape(
                record, resource,
            )
            click_action = record.get("_simple_click_action") or click_action
            type_action = record.get("_simple_type_action") or type_action
            click_action, type_action = self._enforce_action_state(
                record, click_action, type_action,
            )
            if click_action or type_action:
                actionable_texts.setdefault(surface_identity, set()).update(
                    value.casefold() for value in (name, text) if value
                )
        seen_semantics: dict[str, str] = {}
        for record, resource, surface_identity in candidates:
            state = _states(record)
            if record.get("ignored") is True:
                continue
            if state.get("visible") is False:
                continue
            # AT-SPI exposes the entire dormant widget tree, including every
            # closed menu, hidden popover, toolbar implementation detail, and
            # inactive search panel. Dumping those nodes made ordinary Writer
            # reads 46K characters and multi-app reads hit the 80K transport
            # ceiling. They remain discoverable through an explicit query or
            # within expansion, but the default all-surface scene contains
            # only controls the application itself reports as showing.
            if (
                resource == "ui.elements"
                and state.get("showing") is False
                and not include_not_showing
            ):
                continue
            role = _clean_text(record.get("role") or record.get("kind"), 80).casefold()
            name = _clean_text(record.get("name"), 500)
            text = _clean_content(record.get("text"), 800)
            description = _clean_text(record.get("description"), 500)
            value = record.get("value")
            if resource == "browser.tabs" and not record.get("_simple_identity"):
                role = "tab"
                name = _clean_text(record.get("title"), 500) or "Untitled tab"
                description = _clean_text(record.get("url"), 500)
                state["selected"] = record.get("active") is True
            actions = _actions(record)
            click_action, click_arguments, type_action, type_behavior = self._action_shape(
                record, resource,
            )
            if isinstance(record.get("_simple_click_action"), str):
                click_action = str(record["_simple_click_action"])
                click_arguments = dict(record.get("_simple_click_arguments") or {})
            if isinstance(record.get("_simple_type_action"), str):
                type_action = str(record["_simple_type_action"])
            if isinstance(record.get("_simple_type_behavior"), str):
                type_behavior = str(record["_simple_type_behavior"])
            click_action, type_action = self._enforce_action_state(
                record, click_action, type_action,
            )
            if type_action is None:
                type_behavior = None
            if role in _TEXT_ONLY_ROLES and not (click_action or type_action):
                visible_values = [value for value in (name, text) if value]
                if visible_values and all(
                    re.fullmatch(r"[\W_]+", value, flags=re.UNICODE)
                    for value in visible_values
                ):
                    continue
                if any(
                    value.casefold() in actionable_texts.get(surface_identity, set())
                    for value in visible_values
                ):
                    continue
            tree_ref = str(record.get("_simple_tree_ref") or record["ref"])
            # During a lexical query, retain every real in-tree parent long
            # enough for bounded context expansion. This is deliberately
            # based on tree shape rather than a preferred vocabulary of AX
            # roles: real web cards are often exposed merely as generic,
            # section, panel, or container nodes. Oversized ancestry is still
            # rejected later by _expand_query_context.
            context_container = (
                preserve_context_containers
                and tree_ref in structural_parent_refs
            )
            meaningful = bool(
                name or text or description or value not in (None, "")
                or click_action or type_action
                or context_container
            )
            if not meaningful:
                continue
            if (
                role in _LOW_SIGNAL_ROLES
                and not context_container
                and not (name or text or value not in (None, ""))
            ):
                continue
            semantic_key = canonical_fingerprint({
                "surface": surface_identity,
                "role": role,
                "name": name.casefold(),
                "text": text.casefold(),
                "value": value,
                "click": bool(click_action),
                "type": bool(type_action),
            })
            # Prefer browser semantics for duplicated page controls; keep
            # native-only Chrome toolbar/dialog nodes from AT-SPI.
            previous_resource = seen_semantics.get(semantic_key)
            if previous_resource is not None and previous_resource != resource:
                continue
            seen_semantics.setdefault(semantic_key, resource)
            identity = self._identity(record, include_resource=True)
            public_id = self._element_id(surface_identity, identity)
            output.append(_Element(
                identity=identity,
                public_id=public_id,
                ref=str(record["ref"]),
                tree_ref=str(record.get("_simple_tree_ref") or record["ref"]),
                resource=resource,
                surface_identity=surface_identity,
                role=role or "element",
                name=name,
                text=text,
                description=description,
                value=value,
                states=state,
                actions=actions,
                parent_ref=(str(record["parent_ref"]) if isinstance(record.get("parent_ref"), str) else None),
                click_action=click_action,
                click_arguments=click_arguments,
                type_action=type_action,
                type_argument=(
                    str(record.get("_simple_type_argument") or "value")
                    if type_action else None
                ),
                type_behavior=type_behavior,
                metadata={
                    **({"parse": "number"} if type_action == "set_value" else {}),
                    **dict(record.get("_simple_metadata") or {}),
                },
            ))
        included = {element.tree_ref for element in output}
        for element in output:
            parent = element.parent_ref
            seen: set[str] = set()
            while isinstance(parent, str) and parent not in seen and parent not in included:
                seen.add(parent)
                raw_parent = raw_by_ref.get(parent)
                parent = (
                    str(raw_parent["parent_ref"])
                    if raw_parent is not None and isinstance(raw_parent.get("parent_ref"), str)
                    else None
                )
            element.parent_ref = parent if isinstance(parent, str) and parent in included else None
        for surface_identity in surface_identities:
            surface_elements = [
                element for element in output
                if element.surface_identity == surface_identity
            ]
            self._stabilize_element_ids(
                surface_elements,
                surface_identity=surface_identity,
                identities_known_before_read=identities_known_before_read[surface_identity],
            )
        return output

    @staticmethod
    def _render_element(element: _Element, depth: int) -> str:
        fields = [element.role]
        if element.name:
            fields.append(f'"{element.name}"')
        if element.text and element.text != element.name:
            fields.append(f'text="{element.text}"')
        if element.value not in (None, ""):
            value = _clean_content(element.value, 500)
            if value:
                fields.append(f'value="{value}"')
        if element.description and element.description not in {element.name, element.text}:
            fields.append(f'description="{element.description}"')
        for state in ("selected", "checked", "expanded", "focused", "required", "invalid", "busy", "read_only"):
            if element.states.get(state) is True:
                fields.append(state.replace("_", "-"))
        if element.states.get("enabled") is False:
            fields.append("disabled")
        normalized_actions = {_normalized_action(value) for value in element.actions}
        if (
            element.states.get("checked") is False
            and (
                element.role in {"check box", "checkbox", "radio button", "radio", "switch"}
                or normalized_actions.intersection({"check", "uncheck", "toggle"})
            )
        ):
            fields.append("checked=false")
        if (
            element.states.get("expanded") is False
            and normalized_actions.intersection({"expand", "collapse"})
        ):
            fields.append("expanded=false")
        if element.click_action:
            fields.append("click")
        if element.type_behavior:
            fields.append(f"type={element.type_behavior}")
        return f"{'  ' * min(depth, 5)}[{element.public_id}] " + " ".join(fields)

    @staticmethod
    def _model_state_snapshot(
        surfaces: list[_Surface],
        active: _Surface | None,
        rendered_elements: list[_Element],
        *,
        elements_complete: bool,
    ) -> dict[str, Any]:
        """Capture only semantic state actually rendered to the policy model.

        The snapshot deliberately excludes hidden adapter identity and rows on
        later pages.  This makes a later delta a comparison of two model-visible
        views, rather than an accidental leak or a diff against unseen state.
        """

        return {
            "active_surface": active.public_id if active else None,
            "elements_complete": elements_complete,
            "surfaces": {
                surface.public_id: {
                    "label": surface.label(include_id=False),
                    "app": _clean_text(surface.app, 160),
                    "title": _clean_text(surface.title, 240),
                    "active": surface.active,
                    "modified": surface.modified,
                    "modal": surface.modal,
                    "busy": surface.busy,
                }
                for surface in surfaces
            },
            "elements": {
                element.public_id: {
                    "surface": next((
                        surface.public_id for surface in surfaces
                        if surface.identity == element.surface_identity
                    ), None),
                    "role": _clean_text(element.role, 80) or "element",
                    "name": _clean_text(element.name or element.text, 180),
                    "value": _clean_content(element.value, 220),
                    "states": {
                        state: value
                        for state in (
                            "checked", "selected", "expanded", "focused",
                            "enabled", "busy", "invalid", "read_only",
                        )
                        if (value := element.states.get(state)) is not None
                    },
                    "actionable": bool(element.click_action or element.type_action),
                }
                for element in rendered_elements
            },
        }

    @staticmethod
    def _action_delta(
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
        action_result: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Render one bounded, conservative line describing settled state change.

        Surface inventory and stable rows can be compared safely.  Element
        additions/removals are reported only when both reads were complete and
        remained on the same active surface; otherwise filtering, pagination,
        or a surface switch could manufacture noisy false changes.
        """

        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return None
        before_surfaces = before.get("surfaces")
        after_surfaces = after.get("surfaces")
        before_elements = before.get("elements")
        after_elements = after.get("elements")
        if not all(isinstance(value, Mapping) for value in (
            before_surfaces, after_surfaces, before_elements, after_elements,
        )):
            return None

        candidates: list[tuple[int, str]] = []

        def add(priority: int, text: str) -> None:
            cleaned = _clean_text(text, MAX_ACTION_DELTA_CHARS)
            if cleaned and all(existing != cleaned for _rank, existing in candidates):
                candidates.append((priority, cleaned))

        # Some protocol actions prove a semantic change even when the window
        # manager intentionally leaves the originating application active. A
        # new background browser tab is the important example: the CDP adapter
        # verifies the tab-set inclusion and exact count transition, while the
        # fresh active-surface tree may truthfully remain unchanged. Consume
        # only that narrow, self-proving receipt shape; never dump arbitrary
        # adapter delta fields into model context.
        receipt_delta = (
            action_result.get("delta")
            if isinstance(action_result, Mapping)
            and action_result.get("status") == "applied"
            and isinstance(action_result.get("delta"), Mapping)
            else None
        )
        if isinstance(receipt_delta, Mapping):
            tabs_before = receipt_delta.get("tab_count_before")
            tabs_after = receipt_delta.get("tab_count_after")
            if (
                receipt_delta.get("existing_tabs_preserved") is True
                and isinstance(tabs_before, int)
                and not isinstance(tabs_before, bool)
                and isinstance(tabs_after, int)
                and not isinstance(tabs_after, bool)
                and tabs_after == tabs_before + 1
            ):
                opened_url = _clean_text(receipt_delta.get("opened_url"), 360)
                add(
                    0,
                    "added tab"
                    + (f" {opened_url}" if opened_url else "")
                    + f" ({tabs_before}→{tabs_after}; existing tabs preserved)",
                )

        before_surface_ids = set(before_surfaces)
        after_surface_ids = set(after_surfaces)
        added_surface_ids = sorted(
            after_surface_ids - before_surface_ids,
            key=lambda public_id: (
                after_surfaces[public_id].get("active") is not True,
                _letter_rank(str(public_id)),
            ),
        )
        for index, public_id in enumerate(added_surface_ids[:2]):
            surface = after_surfaces[public_id]
            add(
                0 if surface.get("active") is True else 3 + index,
                f"now visible [{public_id}] {surface.get('label') or 'Untitled surface'}",
            )
        if len(added_surface_ids) > 2:
            add(8, f"{len(added_surface_ids) - 2} other surfaces now visible")

        removed_surface_ids = sorted(
            before_surface_ids - after_surface_ids,
            key=lambda public_id: _letter_rank(str(public_id)),
        )
        for public_id in removed_surface_ids[:1]:
            surface = before_surfaces[public_id]
            # Removed capabilities are stale, so intentionally omit their IDs.
            add(5, f"no longer visible: {surface.get('label') or 'Untitled surface'}")

        surface_state_priority = {
            "modified": 1,
            "active": 2,
            "modal": 4,
            "busy": 5,
        }
        for public_id in sorted(
            before_surface_ids & after_surface_ids,
            key=lambda value: _letter_rank(str(value)),
        ):
            old = before_surfaces[public_id]
            new = after_surfaces[public_id]
            if (old.get("app"), old.get("title")) != (new.get("app"), new.get("title")):
                add(3, f"surface [{public_id}] now {new.get('label') or 'Untitled surface'}")
            for state, priority in surface_state_priority.items():
                if old.get(state) != new.get(state):
                    add(
                        priority,
                        f"surface [{public_id}] {state}="
                        f"{str(old.get(state)).lower()}→{str(new.get(state)).lower()}",
                    )

        for public_id in sorted(set(before_elements) & set(after_elements)):
            old = before_elements[public_id]
            new = after_elements[public_id]
            if old.get("value") != new.get("value"):
                add(
                    6,
                    f"[{public_id}] value "
                    f"{json.dumps(old.get('value') or '', ensure_ascii=False)}→"
                    f"{json.dumps(new.get('value') or '', ensure_ascii=False)}",
                )
            old_states = old.get("states") if isinstance(old.get("states"), Mapping) else {}
            new_states = new.get("states") if isinstance(new.get("states"), Mapping) else {}
            for state in (
                "checked", "selected", "expanded", "focused", "enabled",
                "busy", "invalid", "read_only",
            ):
                if old_states.get(state) != new_states.get(state):
                    add(
                        7,
                        f"[{public_id}] {state}="
                        f"{str(old_states.get(state)).lower()}→"
                        f"{str(new_states.get(state)).lower()}",
                    )

        comparable_elements = (
            before.get("elements_complete") is True
            and after.get("elements_complete") is True
            and before.get("active_surface") == after.get("active_surface")
        )
        if comparable_elements:
            added_element_ids = sorted(set(after_elements) - set(before_elements))
            for public_id in added_element_ids:
                element = after_elements[public_id]
                if element.get("actionable") is not True:
                    continue
                label = " ".join(filter(None, (
                    str(element.get("role") or "element"),
                    f'"{element.get("name")}"' if element.get("name") else "",
                )))
                add(8, f"added [{public_id}] {label}")
                break
            removed_element_ids = sorted(set(before_elements) - set(after_elements))
            for public_id in removed_element_ids:
                element = before_elements[public_id]
                if element.get("actionable") is not True:
                    continue
                label = " ".join(filter(None, (
                    str(element.get("role") or "element"),
                    f'"{element.get("name")}"' if element.get("name") else "",
                )))
                # The old element ID is stale and must never be suggested.
                add(9, f"removed {label}")
                break

        if not candidates:
            return None
        parts: list[str] = []
        for _priority, part in sorted(candidates, key=lambda item: item[0]):
            candidate = "; ".join([*parts, part])
            if len(candidate) > MAX_ACTION_DELTA_CHARS:
                break
            parts.append(part)
            if len(parts) >= MAX_ACTION_DELTA_PARTS:
                break
        if not parts:
            return None
        return "After action — " + "; ".join(parts) + "."

    def _prepend_action_delta(
        self,
        view: dict[str, Any],
        before: Mapping[str, Any] | None,
        action_result: Mapping[str, Any] | None = None,
    ) -> bool:
        delta = self._action_delta(
            before,
            self._last_model_state,
            action_result,
        )
        if delta:
            view["text"] = _bounded_output(f"{delta}\n\n{view['text']}")
            return True
        return False

    def read(
        self,
        *,
        query: str | None = None,
        within: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        self.runtime.state.consume_operation()
        saved_cursor: dict[str, Any] | None = None
        if cursor:
            # A cursor is a revision-bound view capability, not a one-use token.
            # Reusing it against the same exact state is safe and lets a model
            # recover from an interrupted turn without losing the collection.
            saved_cursor = self._cursors.get(cursor)
            if saved_cursor is None:
                raise ProtocolError(
                    ErrorCode.STALE_REF, "read cursor is stale; start a fresh read"
                )
            saved_query = saved_cursor.get("raw_query")
            saved_within = saved_cursor.get("within")
            if query is None:
                query = str(saved_query) if saved_query is not None else None
            elif _clean_text(query, 500).casefold() != saved_cursor.get("query"):
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "cursor query conflicts with this read; start a fresh read",
                )
            if within is None:
                within = str(saved_within) if saved_within is not None else None
            elif within.strip().upper() != saved_within:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "cursor scope conflicts with this read; start a fresh read",
                )
        if within is not None:
            within = within.strip().upper()
            if not _REFERENCE.fullmatch(within) or within.isalpha():
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "within must be a current element ID on any listed surface",
                )
        limit = min(MAX_LIMIT, max(1, int(limit)))
        surface_records, surfaces_available = self._try_observe_with_availability(
            "ui.surfaces"
        )
        if not surfaces_available:
            # Compatibility with an older guest bundle during an in-place
            # rollout. The released simple runtime advertises ui.surfaces and
            # therefore takes the single-walk path above.
            surface_records = [
                *self._try_observe("os.applications"),
                *self._try_observe("os.windows"),
                *self._try_observe("os.dialogs"),
            ]
        # Surface discovery is global and shallow; element discovery is scoped
        # to the active single-app surface below. Walking the entire desktop's
        # deep AT-SPI tree made Calc exceed the guest timeout before it could
        # return one useful row.
        browser_tabs = self._try_observe("browser.tabs")
        surfaces, active_identity = self._compile_surfaces(
            surface_records, browser_tabs,
        )
        self._enrich_libreoffice_surfaces(surfaces)
        current_surface_by_identity = {surface.identity: surface for surface in surfaces}
        active = current_surface_by_identity.get(active_identity or "")
        active_browser = active is not None and self._is_browser_surface(active)
        scene_browser_tabs = browser_tabs if active_browser else []
        browser_elements = (
            self._try_observe("browser.elements")
            if active_browser
            else []
        )
        ui_records = self._try_observe(
            "ui.elements",
            parameters={"active_surface_only": True, "max_records": 1500},
        )

        ui_by_ref = {
            str(record["ref"]): record
            for record in ui_records if isinstance(record.get("ref"), str)
        }
        surface_identity_by_ref: dict[str, str] = {}
        for record in ui_records:
            if (
                _clean_text(record.get("role"), 80).casefold() not in _SURFACE_ROLES
                or not isinstance(record.get("ref"), str)
            ):
                continue
            try:
                surface_identity_by_ref[str(record["ref"])] = self._identity(
                    record, include_resource=False,
                )
            except ProtocolError:
                continue
        ui_records_by_surface: dict[str, list[dict[str, Any]]] = {
            surface.identity: [] for surface in surfaces
        }
        for record in ui_records:
            surface_identity = self._surface_for_ui_record(
                record, ui_by_ref, surface_identity_by_ref,
            )
            if surface_identity in ui_records_by_surface:
                ui_records_by_surface[surface_identity].append(record)

        virtual_records_by_surface = {
            surface.identity: (
                self._specialized_records(
                    surface,
                    query=query,
                    browser_tabs=scene_browser_tabs,
                    ui_records=ui_records_by_surface.get(surface.identity, []),
                )
                if surface.active else []
            )
            for surface in surfaces
        }
        all_elements = self._compile_elements(
            ui_records=ui_records,
            browser_tabs=scene_browser_tabs,
            browser_elements=browser_elements,
            virtual_records_by_surface=virtual_records_by_surface,
            surfaces=surfaces,
            preserve_context_containers=bool(query),
            include_not_showing=bool(query or within),
        )
        elements = list(all_elements)

        by_ref = {element.tree_ref: element for element in elements}
        if within:
            root = next((element for element in elements if element.public_id == within), None)
            if root is None:
                raise ProtocolError(ErrorCode.STALE_REF, f"{within} is not current; read again")
            descendants: list[_Element] = []
            for element in elements:
                parent = element.parent_ref
                seen: set[str] = set()
                while isinstance(parent, str) and parent not in seen:
                    if parent == root.tree_ref:
                        descendants.append(element)
                        break
                    seen.add(parent)
                    parent_element = by_ref.get(parent)
                    parent = parent_element.parent_ref if parent_element else None
            elements = [root, *descendants]

        page_text_mode = bool(
            any(self._is_browser_surface(surface) for surface in surfaces)
            and self._requests_complete_page_text(query)
        )
        if page_text_mode:
            elements = [
                element for element in elements
                if element.resource == "browser.text"
            ]

        normalized_query = _clean_text(query, 500).casefold() if query else ""
        filter_query = "" if page_text_mode else normalized_query
        if (
            query
            and any(
                "libreoffice calc" in f"{surface.app} {surface.title}".casefold()
                for surface in surfaces
            )
            and _CALC_REFERENCE.fullmatch(query.strip())
        ):
            filter_query = ""
        if filter_query:
            ranked: list[tuple[tuple[int, int, int], int, _Element]] = []
            for original_index, element in enumerate(elements):
                score = self._query_score(element, filter_query)
                if score is not None:
                    ranked.append((score, original_index, element))
            exact = [item for item in ranked if item[0][0] > 0]
            if exact:
                ranked = exact
            elif ranked:
                # Keep weaker partials only when they retain at least half as
                # many meaningful terms as the best current result. If the
                # active surface contains only one useful lead (for example a
                # folder named in a long request), that lead still survives.
                best_matched = max(item[0][1] for item in ranked)
                minimum_matched = max(1, (best_matched + 1) // 2)
                ranked = [item for item in ranked if item[0][1] >= minimum_matched]
            ranked.sort(key=lambda item: (
                -item[0][0], -item[0][1], -item[0][2], item[1],
            ))
            direct_matches = [item[2] for item in ranked]
            elements = self._expand_query_context(elements, direct_matches)
            # The full-state experiment still bounds pathological lexical
            # matches, but keeps an order of magnitude more evidence than the
            # previous 20-row active-surface arm.
            limit = min(limit, MAX_QUERY_PAGE_SIZE)

        state_fingerprint = canonical_fingerprint({
            "surfaces": [
                (
                    surface.identity, surface.app, surface.title, surface.role,
                    surface.active, surface.modal, surface.modified, surface.busy,
                )
                for surface in surfaces
            ],
            "active_identity": active_identity,
            "elements": [
                (
                    element.identity, element.role, element.name, element.text,
                    element.value, element.states,
                )
                for element in elements
            ],
        })
        offset = 0
        if saved_cursor is not None:
            if (
                saved_cursor.get("fingerprint") != state_fingerprint
                or saved_cursor.get("query") != normalized_query
                or saved_cursor.get("within") != within
            ):
                raise ProtocolError(ErrorCode.REVISION_CONFLICT, "computer changed; start a fresh read")
            offset = int(saved_cursor.get("offset") or 0)
        candidate_page = elements[offset : offset + limit]

        self._current_surfaces = {
            surface.public_id: surface for surface in surfaces
        }
        # Filtering changes only what this read displays. It must not revoke a
        # still-current capability that the model learned from an earlier
        # retained read of another surface.
        self._current_elements = {
            element.public_id: element for element in all_elements
        }
        lines = ["COMPUTER", "", "Surfaces"]
        lines.extend(surface.label() for surface in surfaces)
        rendered_page: list[_Element] = []
        if active is None:
            lines.extend(("", "Active Surface — none", "No active semantic surface is available."))
        all_by_ref = {element.tree_ref: element for element in all_elements}
        candidate_by_surface: dict[str, list[_Element]] = {}
        for element in candidate_page:
            candidate_by_surface.setdefault(element.surface_identity, []).append(element)
        full_inventory = query is None and within is None and cursor is None
        output_full = False
        for surface in surfaces:
            surface_elements = candidate_by_surface.get(surface.identity, [])
            if not surface_elements and not full_inventory:
                continue
            heading = (
                f"Active Surface {surface.label()}"
                if surface.active else f"Surface {surface.label()}"
            )
            if len("\n".join([*lines, "", heading])) > MAX_OUTPUT_CHARS - 180:
                output_full = True
                break
            lines.extend(("", heading))
            if not surface_elements:
                lines.append("No meaningful semantic elements on this surface.")
                continue
            for element in surface_elements:
                depth = 0
                parent = element.parent_ref
                seen: set[str] = set()
                while isinstance(parent, str) and parent not in seen and depth < 5:
                    seen.add(parent)
                    parent_element = all_by_ref.get(parent)
                    if parent_element is None:
                        break
                    depth += 1
                    parent = parent_element.parent_ref
                line = self._render_element(element, depth)
                # Keep every rendered row intact and reserve room for the
                # continuation instruction. Character bounds therefore never
                # silently discard rows after element-count pagination.
                if len("\n".join([*lines, line])) > MAX_OUTPUT_CHARS - 180:
                    output_full = True
                    break
                lines.append(line)
                rendered_page.append(element)
            if output_full:
                break
        if not rendered_page and candidate_page:
            available = MAX_OUTPUT_CHARS - len("\n".join(lines)) - 180
            if available > 40:
                lines.append(self._render_element(candidate_page[0], 0)[:available] + "…")
            rendered_page.append(candidate_page[0])
        if not rendered_page and not candidate_page and surfaces:
            lines.append("No matching semantic elements on any current surface.")
        next_cursor = None
        if offset + len(rendered_page) < len(elements):
            next_cursor = secrets.token_urlsafe(12)
            self._cursors[next_cursor] = {
                "fingerprint": state_fingerprint,
                "query": normalized_query,
                "raw_query": query,
                "within": within,
                "offset": offset + len(rendered_page),
            }
        if next_cursor:
            lines.append(
                f"… {len(elements) - offset - len(rendered_page)} more elements. "
                f'read_computer(cursor="{next_cursor}")'
            )
        text = _bounded_output("\n".join(lines))
        self._last_model_state = self._model_state_snapshot(
            surfaces,
            active,
            rendered_page,
            elements_complete=(
                query is None
                and within is None
                and cursor is None
                and next_cursor is None
            ),
        )
        return {
            "ok": True,
            "text": text,
            "active_surface": active.public_id if active else None,
            "surface_count": len(surfaces),
            "element_count": len(elements),
            "returned_elements": len(rendered_page),
            "next_cursor": next_cursor,
        }

    def _resolve_current(self, public_id: str) -> tuple[_Surface | None, _Element | None]:
        normalized = public_id.strip().upper()
        if not _REFERENCE.fullmatch(normalized):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "element must be a surface letter like B or element ID like B10",
            )
        if normalized.isalpha():
            surface = self._current_surfaces.get(normalized)
            if surface is None:
                raise ProtocolError(ErrorCode.STALE_REF, f"surface {normalized} is stale; read again")
            return surface, None
        element = self._current_elements.get(normalized)
        if element is None:
            raise ProtocolError(ErrorCode.STALE_REF, f"element {normalized} is stale; read again")
        return None, element

    def _activate_element_surface(self, element: _Element) -> _Element:
        """Bring a background element's exact surface forward, then re-resolve it.

        The model addresses a surface-qualified capability directly (for example
        ``B10``). Requiring a separate ``computer_click(B)`` first adds no user
        intent and caused multi-app thrashing. Activation stays private, and the
        element is re-read before mutation so this never acts through a stale
        accessibility object.
        """

        surface = next((
            candidate for candidate in self._current_surfaces.values()
            if candidate.identity == element.surface_identity
        ), None)
        if surface is None:
            raise ProtocolError(
                ErrorCode.STALE_REF,
                f"surface for {element.public_id} is stale; read again",
            )
        if surface.active:
            return element
        if not surface.ref or not surface.resource:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"surface {surface.public_id} cannot be activated",
            )
        self._act(
            public_id=surface.public_id,
            action=("switch_tab" if surface.resource == "browser.tabs" else "activate_window"),
            ref=surface.ref,
            arguments={},
        )
        self.read()
        _surface, fresh = self._resolve_current(element.public_id)
        if fresh is None or fresh.surface_identity != element.surface_identity:
            raise ProtocolError(
                ErrorCode.STALE_REF,
                f"{element.public_id} changed while its surface was activated; read again",
            )
        return fresh

    def _act(self, *, public_id: str, action: str, ref: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result, _adapter, _provenance = self.runtime._act(  # noqa: SLF001
            {
                "target": {"ref": ref},
                "action": action,
                "arguments": dict(arguments),
                "preconditions": [],
                "postconditions": [],
                "confirm": True,
                "timeout_ms": 10_000,
            },
            consume_budget=True,
            request_id=f"simple-act-{public_id}",
        )
        return result

    def _wait_for_launched_surface(self, previous_ids: set[str]) -> None:
        """Wait for a launcher-dispatched window without capturing pixels.

        gtk-launch returns before the application registers its AT-SPI window.
        Polling only the shallow surface inventory prevents the mandatory fresh
        click result from racing back as an unchanged Desktop.  This is a
        bounded semantic wait, not a model-visible retry loop.
        """

        deadline = time.monotonic() + LAUNCH_SETTLE_SECONDS
        activation_attempted: set[str] = set()
        while time.monotonic() < deadline:
            self.runtime.state.consume_operation()
            records = self._try_observe("ui.surfaces")
            surfaces, active_identity = self._compile_surfaces(records, [])
            active = next((
                surface for surface in surfaces
                if surface.identity == active_identity
            ), None)
            if (
                active is not None
                and not self._is_desktop_surface(active)
                and (
                    active.identity not in previous_ids
                    or not any(
                        previous.identity == active.identity and previous.active
                        for previous in self._current_surfaces.values()
                    )
                )
            ):
                return
            new_surfaces = [
                surface for surface in surfaces
                if surface.identity not in previous_ids
                and not self._is_desktop_surface(surface)
            ]
            if (
                active is None
                and len(new_surfaces) == 1
                and new_surfaces[0].identity not in activation_attempted
                and new_surfaces[0].ref
            ):
                launched = new_surfaces[0]
                activation_attempted.add(launched.identity)
                self._act(
                    public_id=launched.public_id,
                    action="activate_window",
                    ref=str(launched.ref),
                    arguments={},
                )
            time.sleep(LAUNCH_SETTLE_POLL_SECONDS)

    @staticmethod
    def _typed_scalar(text: str, parse: str, current: Any = None) -> Any:
        normalized = text.strip()
        kind = parse.casefold()
        if kind in {"number", "integer", "double"} or isinstance(current, (int, float)) and not isinstance(current, bool):
            try:
                number = float(normalized)
            except ValueError as error:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "typed value must be a number") from error
            return int(number) if number.is_integer() else number
        if kind == "number-or-text":
            try:
                number = float(normalized)
            except ValueError:
                return text
            return int(number) if number.is_integer() else number
        if kind in {"boolean", "bool"} or isinstance(current, bool):
            if normalized.casefold() in {"true", "on", "yes", "1"}:
                return True
            if normalized.casefold() in {"false", "off", "no", "0"}:
                return False
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "typed value must be true or false")
        if kind in {"list", "dictionary", "json"}:
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "typed value must be valid JSON") from error
        return text

    @staticmethod
    def _calc_end_address(start: str, rows: int, columns: int) -> str:
        match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", start)
        if match is None:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "spreadsheet cell address is invalid")
        column = 0
        for character in match.group(1).upper():
            column = column * 26 + ord(character) - ord("A") + 1
        column += columns - 1
        label = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            label = chr(ord("A") + remainder) + label
        return f"{label}{int(match.group(2)) + rows - 1}"

    def _type_calc(self, element: _Element, text: str) -> dict[str, Any]:
        sheet = str(element.metadata.get("sheet") or "")
        address = str(element.metadata.get("address") or "")
        matrix = [line.split("\t") for line in text.splitlines()]
        if not matrix:
            matrix = [[""]]
        width = len(matrix[0])
        if width == 0 or any(len(row) != width for row in matrix):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "spreadsheet input must be a rectangular tab/newline grid",
            )
        if len(matrix) == 1 and width == 1:
            scalar = matrix[0][0]
            if scalar.startswith("="):
                action = "set_formula"
                arguments: dict[str, Any] = {"formula": scalar}
            else:
                try:
                    number = float(scalar.strip())
                except ValueError:
                    action = "set_text"
                    arguments = {"text": scalar}
                else:
                    action = "set_value"
                    arguments = {"value": int(number) if number.is_integer() else number}
            return self._act(
                public_id=element.public_id,
                action=action,
                ref=element.ref,
                arguments=arguments,
            )
        end = self._calc_end_address(address, len(matrix), width)
        range_name = f"{address}:{end}"
        ranges = self._observe(
            "spreadsheet.ranges",
            parameters={"sheet": sheet, "range": range_name},
        )
        if len(ranges) != 1 or not isinstance(ranges[0].get("ref"), str):
            raise ProtocolError(ErrorCode.AMBIGUOUS, "spreadsheet target range is not unique")
        has_formula = any(value.startswith("=") for row in matrix for value in row)
        if has_formula:
            action = "set_range_formulas"
            arguments = {"formulas": matrix}
        else:
            values = [
                [self._typed_scalar(value, "number-or-text") for value in row]
                for row in matrix
            ]
            action = "set_range_values"
            arguments = {"values": values}
        return self._act(
            public_id=element.public_id,
            action=action,
            ref=str(ranges[0]["ref"]),
            arguments=arguments,
        )

    def click(self, public_id: str) -> dict[str, Any]:
        before = self._last_model_state
        previous_surface_ids = {
            surface.identity for surface in self._current_surfaces.values()
        }
        surface, element = self._resolve_current(public_id)
        if surface is not None:
            if not surface.ref or not surface.resource:
                raise ProtocolError(ErrorCode.UNSUPPORTED, f"surface {surface.public_id} cannot be activated")
            action = "switch_tab" if surface.resource == "browser.tabs" else "activate_window"
            result = self._act(
                public_id=surface.public_id,
                action=action,
                ref=surface.ref,
                arguments={},
            )
        else:
            assert element is not None
            if element.click_action is None:
                # Browser AX trees often put the visible label in a static-text
                # child while the owning generic/button node receives pointer
                # events. Treat clicking that label like an ordinary human
                # click only when one nearest in-surface ancestor is currently
                # and honestly invokable. Native-app labels remain read-only.
                by_tree_ref = {
                    candidate.tree_ref: candidate
                    for candidate in self._current_elements.values()
                    if candidate.surface_identity == element.surface_identity
                }
                parent_ref = element.parent_ref
                seen: set[str] = set()
                owning_click_target: _Element | None = None
                while isinstance(parent_ref, str) and parent_ref not in seen:
                    seen.add(parent_ref)
                    candidate = by_tree_ref.get(parent_ref)
                    if candidate is None:
                        break
                    if candidate.click_action is not None:
                        owning_click_target = candidate
                        break
                    parent_ref = candidate.parent_ref
                if element.resource != "browser.elements" or owning_click_target is None:
                    raise ProtocolError(
                        ErrorCode.UNSUPPORTED,
                        f"{element.public_id} is readable but not clickable",
                    )
                element = owning_click_target
            element = self._activate_element_surface(element)
            if element.click_action is None:
                raise ProtocolError(
                    ErrorCode.STALE_REF,
                    f"{element.public_id} changed while its surface was activated; read again",
                )
            result = self._act(
                public_id=element.public_id,
                action=element.click_action,
                ref=element.ref,
                arguments=element.click_arguments,
            )
            if element.click_action == "launch":
                self._wait_for_launched_surface(previous_surface_ids)
        view = self.read()
        self._prepend_action_delta(view, before, result)
        return {**view, "action": result}

    def type_text(self, public_id: str, text: str) -> dict[str, Any]:
        before = self._last_model_state
        surface, element = self._resolve_current(public_id)
        if surface is not None or element is None:
            raise ProtocolError(ErrorCode.UNSUPPORTED, "type requires an editable element ID")
        if element.type_action is None:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"{element.public_id} is not editable",
            )
        element = self._activate_element_surface(element)
        if element.type_action is None:
            raise ProtocolError(
                ErrorCode.STALE_REF,
                f"{element.public_id} changed while its surface was activated; read again",
            )
        if element.type_action == "simple_calc_type":
            result = self._type_calc(element, text)
        elif element.type_action == "simple_writer_insert_end":
            # Preserve intentional blank paragraphs and trailing paragraph
            # breaks while making CRLF/CR input deterministic for UNO.
            paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            result = self._act(
                public_id=element.public_id,
                action="insert_paragraphs",
                ref=element.ref,
                arguments={"paragraphs": paragraphs, "position": "end"},
            )
        else:
            arguments = dict(element.metadata.get("static_arguments") or {})
            arguments[str(element.type_argument or "value")] = self._typed_scalar(
                text,
                str(element.metadata.get("parse") or "auto"),
                element.value,
            )
            result = self._act(
                public_id=element.public_id,
                action=element.type_action,
                ref=element.ref,
                arguments=arguments,
            )
        view = self.read()
        self._prepend_action_delta(view, before, result)
        return {**view, "action": result}
