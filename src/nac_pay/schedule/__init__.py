"""Schedule layer (Layer 2) — domain model + lowering to engine inputs.

Owns Trip, Day, Leg, PilotProfile, labels (ReasonCode, PremiumCategory).
The engine knows nothing about these; schedule lowers them into the
engine's Chunk / FloorEvent vocabulary.
"""

from .apply_actuals import (
    AppliedEvent,
    AppliedEventKind,
    FeedReassignment,
    apply_actuals_to_month,
)
from .apply_overrides import apply_overrides_to_month
from .apply_user_versions import apply_user_versions_to_month
from .from_master_schedule import ConversionWarning, month_from_master_schedule
from .labels import (
    DutyType,
    EntryMode,
    Position,
    PremiumCategory,
    PremiumScope,
    ReasonCode,
)
from .lower import lower_month
from .models import (
    AssignmentVersion,
    Day,
    Leg,
    Month,
    PilotProfile,
    Trip,
)

__all__ = [
    "AppliedEvent",
    "AppliedEventKind",
    "FeedReassignment",
    "AssignmentVersion",
    "ConversionWarning",
    "Day",
    "DutyType",
    "EntryMode",
    "Leg",
    "Month",
    "PilotProfile",
    "Position",
    "PremiumCategory",
    "PremiumScope",
    "ReasonCode",
    "Trip",
    "apply_actuals_to_month",
    "apply_overrides_to_month",
    "apply_user_versions_to_month",
    "lower_month",
    "month_from_master_schedule",
]
