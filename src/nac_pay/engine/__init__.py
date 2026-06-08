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

__all__ = [
    "Chunk",
    "ChunkKind",
    "ChunkResult",
    "EngineInput",
    "EngineResult",
    "FloorEvent",
    "FloorEventKind",
    "WinningOption",
    "compute_pay",
]
