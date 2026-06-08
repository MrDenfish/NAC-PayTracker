"""§3.E.1.b reassignment greater-of rule.

When a trip is reassigned, rerouted, cancelled, or has its duty extended,
the pilot is paid the GREATER of the originally-published PCH and the
recomputed PCH. The rule protects against reduction and captures uplift.

Real-data anchor: FLT 766 in the May 2026 Trip Pairing Packet (page 8)
has TRIP PCH VALUE = 4.17. The May 1 reassignment ("additional leg added
to FLT 766, new PCH 5.00") therefore yields effective PCH 5.00.

FLT 724 doesn't appear in the May packet — likely a non-bid/charter
flight reassigned ad hoc. The duty-extension event (13.0 duty hrs ÷ 2
= 6.50 duty-rig PCH) is tested as the §3.E generic duty-extension rule
against a representative original.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.engine import (
    components_from_times,
    effective_trip_pch_after_reassignment,
)

D = Decimal


# ── Real-data anchor: FLT 766, May 2026 packet ───────────────────────────
class TestFlt766FromMayPacket:
    """FLT 766 published trip "766/766/767": TRIP PCH VALUE 4.17 (packet p.8).

    Component breakdown (for the §9 validator we'll build later):
      Flight Op = 4.17  ← winner
      Duty Rig  = 3.54
      Trip Rig  = 1.45
      Cum. DPG  = 3.82
      Deadhead  = 0.00
    """

    ORIGINAL_PUBLISHED = D("4.17")
    REASSIGNED_NEW = D("5.00")  # leg added per May 1 event

    def test_reassignment_uplift_pays_new(self):
        effective = effective_trip_pch_after_reassignment(
            self.ORIGINAL_PUBLISHED, self.REASSIGNED_NEW
        )
        assert effective == D("5.00")

    def test_published_components_reproduce_417(self):
        # Block 4:10 = 4.166... ≈ 4.17. The packet uses 2dp rounding.
        block = D("4.17")
        duty = D("7.0833")  # 7:05 = 7 + 5/60
        tafb = D("7.0833")
        comps = components_from_times(
            block_hours=block,
            duty_hours=duty,
            tafb_hours=tafb,
            workdays=1,
        )
        assert round(comps.trip_pch, 2) == D("4.17")
        assert round(comps.duty_rig, 2) == D("3.54")
        assert round(comps.trip_rig, 2) == D("1.45")
        assert comps.cumulative_dpg == D("3.82")


# ── Duty extension rule (FLT 724-style) ─────────────────────────────────
class TestDutyExtension:
    """§3.E: duty extension recomputes via `max(new_duty_rig, current_trip_pch)`.

    FLT 724 event: duty extended to 13.0 hrs → new duty_rig = 6.50 PCH.
    Without a packet original, we test the rule itself against representative
    originals — both the uplift direction (new > original) and the
    protection direction (new < original).
    """

    NEW_DUTY_HOURS = D("13.0")
    NEW_DUTY_RIG = NEW_DUTY_HOURS / D("2")  # 6.50

    def test_new_duty_rig_is_6_50(self):
        assert self.NEW_DUTY_RIG == D("6.50")

    def test_uplift_pays_extended_duty(self):
        # Original 5.00, duty extends to 13.0 hrs → 6.50 PCH wins
        effective = effective_trip_pch_after_reassignment(D("5.00"), self.NEW_DUTY_RIG)
        assert effective == D("6.50")

    def test_protection_pays_original_when_extension_is_smaller(self):
        # Original 8.00; even a 13-hr duty (6.50 rig) doesn't beat it
        effective = effective_trip_pch_after_reassignment(D("8.00"), self.NEW_DUTY_RIG)
        assert effective == D("8.00")

    def test_spec_worked_example_duty_extension(self):
        # Spec §3.E: "trip PCH 5, duty extends 8→12 hrs → max(12÷2, 5) = 6"
        new_rig = D("12") / D("2")
        assert effective_trip_pch_after_reassignment(D("5"), new_rig) == D("6")


# ── The rule itself: greater-of, idempotent, chains correctly ────────────
@pytest.mark.parametrize(
    "original,recomputed,expected",
    [
        # Uplift cases
        (D("4.17"), D("5.00"), D("5.00")),
        (D("3.82"), D("4.50"), D("4.50")),
        # Protection cases (the real point of the rule)
        (D("5.33"), D("4.00"), D("5.33")),
        (D("8.00"), D("6.50"), D("8.00")),
        # Edge: equal
        (D("5.00"), D("5.00"), D("5.00")),
    ],
)
def test_greater_of_table(original, recomputed, expected):
    assert effective_trip_pch_after_reassignment(original, recomputed) == expected


def test_chain_of_revisions_takes_overall_max():
    # Mirrors §13: a day holds a stack of versions; effective PCH = max over all.
    # E.g., 5.33 original → revised to 4.00 → revised to 6.08 → revised to 3.50
    # The pilot is paid 6.08 (the high-water mark across the chain).
    effective = effective_trip_pch_after_reassignment(
        D("5.33"), D("4.00"), D("6.08"), D("3.50")
    )
    assert effective == D("6.08")


def test_single_value_returns_itself():
    # No reassignment yet → trivial pass-through
    assert effective_trip_pch_after_reassignment(D("4.17")) == D("4.17")
