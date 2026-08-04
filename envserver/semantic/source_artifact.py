"""Private public-source to guest-artifact composition.

This module deliberately exposes no model tool. It composes the existing
SSRF-safe public HTTP transport with the guest daemon's private atomic blob
transport, producing a bounded guest artifact plus truthful source provenance.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .protocol import ErrorCode, ProtocolError, SideEffectState
from .research_adapter import PublicHTTPTransport


GuestRequest = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]

MAX_LIBREOFFICE_EXTENSION_BYTES = 300 * 1024 * 1024
_STAGING_ROOT = "/home/user/Downloads/.ghost-semantic"


def _guest_error(response: Mapping[str, Any], message: str) -> ProtocolError:
    raw = response.get("error")
    error = raw if isinstance(raw, Mapping) else {}
    try:
        code = ErrorCode(str(error.get("code") or "internal_error"))
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    try:
        side_effect_state = SideEffectState(
            str(error.get("side_effect_state") or "none")
        )
    except ValueError:
        side_effect_state = SideEffectState.UNKNOWN
    return ProtocolError(
        code,
        str(error.get("message") or message)[:2_000],
        retryable=bool(error.get("retryable", False)),
        side_effect_state=side_effect_state,
    )


@dataclass(frozen=True)
class StagedSourceArtifact:
    path: str
    sha256: str
    size: int
    requested_url: str
    final_url: str
    http_status: int
    fetched_at: str
    redirect_chain: tuple[str, ...]
    content_type: str | None

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.final_url,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "fetched_at": self.fetched_at,
            "redirect_chain": list(self.redirect_chain),
            "content_hash": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
            "freshness": "live",
        }


class PublicSourceArtifactStager:
    """Stream one public artifact into a private guest staging root."""

    def __init__(
        self,
        guest_request: GuestRequest,
        *,
        http_transport: PublicHTTPTransport | None = None,
    ) -> None:
        self.guest_request = guest_request
        self.http_transport = http_transport or PublicHTTPTransport()

    def stage_libreoffice_extension(self, source_url: str) -> StagedSourceArtifact:
        transfer_id = secrets.token_urlsafe(24)
        path = f"{_STAGING_ROOT}/{transfer_id}.oxt"
        offset = 0
        finalized = False

        def stage(chunk: bytes) -> None:
            nonlocal offset
            response = self.guest_request("POST", "/v1/act", {
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer_id,
                    "offset": offset,
                    "path": path if offset == 0 else None,
                    "artifact_kind": (
                        "libreoffice_extension" if offset == 0 else None
                    ),
                    "base64": base64.b64encode(chunk).decode("ascii"),
                    "final": False,
                },
            })
            if not response.get("ok"):
                raise _guest_error(response, "guest rejected a source artifact chunk")
            offset += len(chunk)

        try:
            fetched = self.http_transport.stream(
                source_url,
                stage,
                max_bytes=MAX_LIBREOFFICE_EXTENSION_BYTES,
            )
            final = self.guest_request("POST", "/v1/act", {
                "action": "stage_base64_chunk",
                "arguments": {
                    "transfer_id": transfer_id,
                    "offset": offset,
                    "path": path if offset == 0 else None,
                    "artifact_kind": (
                        "libreoffice_extension" if offset == 0 else None
                    ),
                    "base64": "",
                    "final": True,
                },
            })
            if not final.get("ok"):
                raise _guest_error(final, "guest rejected the completed source artifact")
            finalized = True
            result = final.get("result")
            if not isinstance(result, Mapping):
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    "guest committed a source artifact without a receipt",
                    side_effect_state=SideEffectState.UNKNOWN,
                )
            guest_hash = result.get("sha256")
            guest_size = result.get("size")
            if guest_hash != fetched.content_hash or guest_size != fetched.size:
                raise ProtocolError(
                    ErrorCode.ARTIFACT_CONFLICT,
                    "guest source artifact differs from the fetched response",
                    side_effect_state=SideEffectState.APPLIED,
                )
            return StagedSourceArtifact(
                path=str(result.get("path") or path),
                sha256=fetched.content_hash,
                size=fetched.size,
                requested_url=fetched.requested_url,
                final_url=fetched.final_url,
                http_status=fetched.status,
                fetched_at=fetched.fetched_at,
                redirect_chain=fetched.redirect_chain,
                content_type=fetched.headers.get("content-type"),
            )
        except Exception:
            if not finalized:
                try:
                    self.guest_request("POST", "/v1/act", {
                        "action": "abort_blob_transfer",
                        "arguments": {"transfer_id": transfer_id},
                    })
                except Exception:
                    pass
            raise

    def remove(self, staged: StagedSourceArtifact) -> bool:
        response = self.guest_request("POST", "/v1/act", {
            "action": "remove_staged_artifact",
            "arguments": {
                "path": staged.path,
                "expected_hash": staged.sha256,
            },
        })
        if not response.get("ok"):
            raise _guest_error(response, "guest rejected source artifact cleanup")
        result = response.get("result")
        return bool(isinstance(result, Mapping) and result.get("removed") is True)


def source_provenance(
    staged: StagedSourceArtifact, installed: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    """Bind network bytes to the registry identity established by unopkg."""

    return ({
        **staged.provenance(),
        "artifact_path": staged.path,
        "artifact_type": "libreoffice_extension",
        "extension_identifier": installed.get("identifier"),
        "extension_version": installed.get("version"),
        "registry": "unopkg",
    },)
