"""Factory and inventory metadata for non-core Linux OSWorld applications."""

from __future__ import annotations

from typing import Sequence

from .application_adapter import RemoteApplicationAdapter, Transport
from .gimp_adapter import GimpSemanticAdapter
from .media_adapter import MediaMetadataAdapter, PicardMediaAdapter
from .pdf_adapter import PDFEvinceSemanticAdapter
from .terminal_adapter import SandboxedTerminalAdapter
from .thunderbird_adapter import ThunderbirdSemanticAdapter
from .vlc_adapter import MPRISMediaAdapter, VLCSemanticAdapter
from .vscode_adapter import VSCodeSemanticAdapter


INVENTORY_ADAPTER_IDS = frozenset({
    "thunderbird-extension@1",
    "vscode-ghost-extension@1",
    "vlc-mpris-http@1",
    "mpris-media@1",
    "pdf-evince@1",
    "sandboxed-process@1",
    "gimp-pdb@1",
    "picard-media@1",
    "media-metadata@1",
})


def create_remaining_application_adapters(
    transport: Transport | None,
) -> Sequence[RemoteApplicationAdapter]:
    """Create one task-agnostic adapter for every remaining inventory family."""

    return (
        ThunderbirdSemanticAdapter(transport),
        VSCodeSemanticAdapter(transport),
        VLCSemanticAdapter(transport),
        MPRISMediaAdapter(transport),
        PDFEvinceSemanticAdapter(transport),
        SandboxedTerminalAdapter(transport),
        GimpSemanticAdapter(transport),
        PicardMediaAdapter(transport),
        MediaMetadataAdapter(transport),
    )
