"""Option 1: the adjusted-MPG floor.

Section 3.D, per the spec's worked examples:

1. Start with floor = max(line_value, MPG).
2. Drops (voluntary, lesser trade, unprotected unavailability) reduce 1:1.
3. If drops push the floor below the actual remaining schedule PCH
   (chunks excluding "on top" credits like open-time pickups), the floor
   **forfeits** down to that actual remaining PCH — the line→MPG headroom
   is lost along with the drop. See worked check #4:
     floor 65 → drop 3×3.82 → naive 53.54, but actual remaining
     14×3.82 = 53.48, so option1's pre-on-top base = 53.48.
4. Open-time pickups and involuntary-callout EXCESS sit *on top* of that
   base — they're not subject to forfeit.

Drops without chunks (e.g., if the schedule layer doesn't model the
dropped trips) still reduce 1:1 but skip the forfeit cap, because we have
no ground-truth "remaining" to clamp to.
"""

from __future__ import annotations

from decimal import Decimal

from .constants import MPG
from .models import Chunk, ChunkKind, FloorEvent, FloorEventKind

_DROP_KINDS = frozenset(
    {
        FloorEventKind.VOLUNTARY_DROP,
        FloorEventKind.LESSER_TRADE,
        FloorEventKind.UNPROTECTED_UNAVAIL,
    }
)

_ON_TOP_KINDS = frozenset(
    {
        FloorEventKind.OPEN_TIME_PICKUP,
        FloorEventKind.INVOLUNTARY_EXCESS,
    }
)

# Chunk kinds whose PCH lives "on top" of the floor (paired with an on-top event).
_ON_TOP_CHUNK_KINDS = frozenset({ChunkKind.OPEN_TIME})


def compute_adjusted_floor(
    line_value: Decimal,
    events: tuple[FloorEvent, ...],
    chunks: tuple[Chunk, ...] = (),
) -> Decimal:
    starting_floor = max(line_value, MPG)

    total_drops = Decimal("0")
    total_on_top = Decimal("0")
    for ev in events:
        if ev.kind in _DROP_KINDS:
            total_drops += ev.delta_pch
        elif ev.kind in _ON_TOP_KINDS:
            total_on_top += ev.delta_pch
        else:
            raise ValueError(f"Unknown floor event kind: {ev.kind}")

    naive_reduced = starting_floor - total_drops

    if total_drops > 0 and chunks:
        remaining_pre_on_top = sum(
            (c.raw_pch for c in chunks if c.in_guarantee and c.kind not in _ON_TOP_CHUNK_KINDS),
            Decimal("0"),
        )
        floor_base = min(naive_reduced, remaining_pre_on_top)
    else:
        floor_base = naive_reduced

    return floor_base + total_on_top
