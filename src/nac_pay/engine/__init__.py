"""Pure, headless pay engine — Section 3.

No I/O, no UI, no calendar logic. Operates on Chunk + FloorEvent only.
"""

from .compute import compute_pay
from .models import (
    Chunk,
    ChunkKind,
    ChunkResult,
    EngineInput,
    EngineResult,
    FloorEvent,
    FloorEventKind,
    WinningOption,
)
from .trip_pch import (
    TripPchComponents,
    components_from_times,
    effective_trip_pch_after_reassignment,
    recompute_pch_from_times,
)

__all__ = [
    "Chunk",
    "ChunkKind",
    "ChunkResult",
    "EngineInput",
    "EngineResult",
    "FloorEvent",
    "FloorEventKind",
    "TripPchComponents",
    "WinningOption",
    "components_from_times",
    "compute_pay",
    "effective_trip_pch_after_reassignment",
    "recompute_pch_from_times",
]
