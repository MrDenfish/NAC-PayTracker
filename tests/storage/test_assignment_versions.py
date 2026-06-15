"""UserAssignmentVersionStore: append-only save + supersession resolver."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.storage import (
    UserAssignmentVersionStore,
    VersionEntryMode,
    VersionType,
    active_versions,
)


def _store(uid: str = "user-versions-tests") -> UserAssignmentVersionStore:
    return UserAssignmentVersionStore(user_id=uid)


# ── seq assignment ─────────────────────────────────────────────────


def test_seq_starts_at_1_per_date():
    s = _store()
    v = s.save(
        date_iso="2026-06-02",
        version_type=VersionType.REASSIGNMENT,
        assignment_id="722/754",
        entry_mode=VersionEntryMode.SIMPLE,
        pch_value=Decimal("6.08"),
    )
    assert v.seq == 1  # seq=0 is reserved for the trip's "Original"


def test_seq_monotonic_per_date():
    s = _store("user-seq-mono")
    v1 = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                assignment_id="X", entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("5.0"))
    v2 = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                assignment_id="Y", entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("6.0"))
    v3 = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                assignment_id="Z", entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("7.0"))
    assert (v1.seq, v2.seq, v3.seq) == (1, 2, 3)


def test_seq_isolated_per_date():
    s = _store("user-seq-per-date")
    a = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
               assignment_id="A", entry_mode=VersionEntryMode.SIMPLE,
               pch_value=Decimal("5.0"))
    b = s.save(date_iso="2026-06-03", version_type=VersionType.REASSIGNMENT,
               assignment_id="B", entry_mode=VersionEntryMode.SIMPLE,
               pch_value=Decimal("5.0"))
    assert a.seq == 1 and b.seq == 1


# ── Read paths ─────────────────────────────────────────────────────


def test_list_for_date_orders_by_seq():
    s = _store("user-list-order")
    s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
           assignment_id="A", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("5.0"))
    s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
           assignment_id="B", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("6.0"))
    rows = s.list_for_date("2026-06-02")
    assert [r.seq for r in rows] == [1, 2]


def test_list_for_month_groups_by_date():
    s = _store("user-list-month")
    s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
           assignment_id="X", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("5.0"))
    s.save(date_iso="2026-06-15", version_type=VersionType.REASSIGNMENT,
           assignment_id="Y", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("5.0"))
    s.save(date_iso="2026-07-01", version_type=VersionType.REASSIGNMENT,
           assignment_id="Z", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("5.0"))
    out = s.list_for_month(2026, 6)
    assert sorted(out.keys()) == ["2026-06-02", "2026-06-15"]
    assert len(out["2026-06-02"]) == 1


def test_user_isolation():
    s1 = _store("user-a")
    s2 = _store("user-b")
    s1.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
            assignment_id="A", entry_mode=VersionEntryMode.SIMPLE,
            pch_value=Decimal("5.0"))
    assert s1.list_for_date("2026-06-02")
    assert s2.list_for_date("2026-06-02") == []


# ── Supersession resolver ─────────────────────────────────────────


def test_active_versions_no_corrections():
    s = _store("user-no-corr")
    s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
           assignment_id="X", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("5.0"))
    s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
           assignment_id="Y", entry_mode=VersionEntryMode.SIMPLE,
           pch_value=Decimal("6.0"))
    versions = s.list_for_date("2026-06-02")
    active, sup = active_versions(versions)
    assert [v.seq for v in active] == [1, 2]
    assert sup == set()


def test_active_versions_correction_supersedes():
    """The typo scenario from the Phase G design discussion."""
    s = _store("user-typo")
    v1 = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                assignment_id="X", entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("5.0"))
    v2 = s.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                assignment_id="Y", entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("5.3"))  # typo
    v3 = s.save(date_iso="2026-06-02", version_type=VersionType.CORRECTION,
                correction_of=v2.seq, assignment_id="Y",
                entry_mode=VersionEntryMode.SIMPLE,
                pch_value=Decimal("5.2"))  # corrected

    versions = s.list_for_date("2026-06-02")
    active, sup = active_versions(versions)
    assert v2.seq in sup
    assert [v.seq for v in active] == [v1.seq, v3.seq]
    # Max of active PCH = 5.2 (NOT 5.3 — that's what the design fixes).
    assert max(v.pch_value for v in active) == Decimal("5.2")


def test_correction_of_missing_seq_is_ignored():
    """If correction_of points at a non-existent seq, the resolver
    treats the correction as a normal version (nothing superseded).
    Defensive — the route validates this at write time, but the
    resolver shouldn't crash if the DB has an inconsistent row."""
    s = _store("user-bad-corr")
    v = s.save(date_iso="2026-06-02", version_type=VersionType.CORRECTION,
               correction_of=999, assignment_id="X",
               entry_mode=VersionEntryMode.SIMPLE,
               pch_value=Decimal("5.0"))
    versions = s.list_for_date("2026-06-02")
    active, sup = active_versions(versions)
    assert active == versions and sup == set()
