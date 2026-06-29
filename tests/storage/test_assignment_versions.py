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


# ── hard delete ────────────────────────────────────────────────────


def _rsv(s, date_iso, **kw):
    base = dict(version_type=VersionType.REASSIGNMENT, assignment_id="X",
                entry_mode=VersionEntryMode.SIMPLE, pch_value=Decimal("5.0"))
    base.update(kw)
    return s.save(date_iso=date_iso, **base)


def test_delete_removes_the_row():
    s = _store("user-del-basic")
    _rsv(s, "2026-06-02")
    _rsv(s, "2026-06-02", pch_value=Decimal("6.0"))
    assert s.delete("2026-06-02", 1) == [1]
    remaining = [v.seq for v in s.list_for_date("2026-06-02")]
    assert remaining == [2]


def test_delete_cascades_to_corrections_of_the_target():
    """Deleting a reassignment that a correction supersedes removes the
    correction too — the log never points at a deleted seq."""
    s = _store("user-del-cascade")
    _rsv(s, "2026-06-02")                                   # v1
    _rsv(s, "2026-06-02", version_type=VersionType.CORRECTION,
         correction_of=1, pch_value=Decimal("5.2"))         # v2 corrects v1
    deleted = s.delete("2026-06-02", 1)
    assert deleted == [1, 2]
    assert s.list_for_date("2026-06-02") == []


def test_delete_correction_only_removes_itself():
    s = _store("user-del-corr-only")
    _rsv(s, "2026-06-02")                                   # v1
    _rsv(s, "2026-06-02", version_type=VersionType.CORRECTION,
         correction_of=1, pch_value=Decimal("5.2"))         # v2
    assert s.delete("2026-06-02", 2) == [2]
    assert [v.seq for v in s.list_for_date("2026-06-02")] == [1]


def test_delete_missing_seq_is_noop():
    s = _store("user-del-missing")
    _rsv(s, "2026-06-02")
    assert s.delete("2026-06-02", 99) == []
    assert s.delete("2026-06-02", 0) == []
    assert [v.seq for v in s.list_for_date("2026-06-02")] == [1]


def test_delete_top_seq_frees_it_for_reuse():
    """save() assigns seq = max(existing)+1, so deleting the highest seq
    frees that number for the next save. Harmless: cascade already removed
    any correction that referenced it, so no row points at a stale seq."""
    s = _store("user-del-reuse")
    _rsv(s, "2026-06-02")          # v1
    _rsv(s, "2026-06-02")          # v2
    s.delete("2026-06-02", 2)
    v = _rsv(s, "2026-06-02")      # max is now 1 → next seq is 2 again
    assert v.seq == 2
    assert [x.seq for x in s.list_for_date("2026-06-02")] == [1, 2]


def test_delete_middle_seq_does_not_reuse():
    """Deleting a non-top seq leaves the max intact, so the next save still
    advances past it (no reuse of the gap)."""
    s = _store("user-del-gap")
    _rsv(s, "2026-06-02")          # v1
    _rsv(s, "2026-06-02")          # v2
    _rsv(s, "2026-06-02")          # v3
    s.delete("2026-06-02", 2)
    v = _rsv(s, "2026-06-02")      # max is 3 → next is 4
    assert v.seq == 4


# ── manual legs (per-version, for the merged Legs display) ──────────


def test_save_and_list_legs():
    from nac_pay.storage import VersionLeg
    s = _store("user-legs")
    v = _rsv(s, "2026-06-02")
    s.save_legs("2026-06-02", v.seq, [
        VersionLeg("720", "06:41", "09:00"),
        VersionLeg("721", "10:00", "12:00"),
    ])
    legs = s.list_legs_for_date("2026-06-02")
    assert [lg.flight for lg in legs[v.seq]] == ["720", "721"]
    assert legs[v.seq][0].out_local == "06:41"


def test_save_legs_replaces_prior_set():
    from nac_pay.storage import VersionLeg
    s = _store("user-legs-replace")
    v = _rsv(s, "2026-06-02")
    s.save_legs("2026-06-02", v.seq, [VersionLeg("720", "06:00", "09:00")])
    s.save_legs("2026-06-02", v.seq, [VersionLeg("999", "01:00", "02:00")])
    legs = s.list_legs_for_date("2026-06-02")
    assert [lg.flight for lg in legs[v.seq]] == ["999"]


def test_delete_version_cascades_its_legs():
    from nac_pay.storage import VersionLeg
    s = _store("user-legs-cascade")
    v = _rsv(s, "2026-06-02")
    s.save_legs("2026-06-02", v.seq, [VersionLeg("720", "06:00", "09:00")])
    s.delete("2026-06-02", v.seq)
    assert s.list_legs_for_date("2026-06-02") == {}
