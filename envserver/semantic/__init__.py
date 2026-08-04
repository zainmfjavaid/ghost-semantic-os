"""Task-agnostic semantic computer kernel.

The package deliberately has no dependency on OSWorld tasks, evaluators, or the
HTTP server.  ``envserver.server`` can integrate it later through the adapter
registry and protocol envelopes without making benchmark-specific behavior part
of the kernel.
"""

from .adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    AdapterRegistry,
    SemanticAdapter,
)
from .interpreter import InterpreterLimits, RunResult, SemanticInterpreter
from .protocol import (
    ErrorCode,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    SideEffectState,
    Status,
    error_response,
    ok_response,
)
from .query import QueryEngine, QueryPage
from .state import EpisodeState, EpisodeStore, StateLimits
from .verify import VerificationEngine, VerificationResult

__all__ = [
    "AdapterActionResult",
    "AdapterContext",
    "AdapterObservation",
    "AdapterRegistry",
    "EpisodeState",
    "EpisodeStore",
    "ErrorCode",
    "InterpreterLimits",
    "ProtocolError",
    "QueryEngine",
    "QueryPage",
    "RequestEnvelope",
    "ResponseEnvelope",
    "RunResult",
    "SemanticAdapter",
    "SemanticInterpreter",
    "SideEffectState",
    "StateLimits",
    "Status",
    "VerificationEngine",
    "VerificationResult",
    "error_response",
    "ok_response",
]
