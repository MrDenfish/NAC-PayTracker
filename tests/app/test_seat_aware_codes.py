"""Seat-aware pilot-code keying (issue #67).

A 3-letter pilot code is only unique WITHIN a seat: the same code can belong
to two different pilots across the FO and CA Final Award sheets (the real case:
PBG = BAGIAN on the FO sheet and BIAGIONI on the CA sheet). Keying by bare code
let one silently shadow the other — the losing-seat pilot got the wrong
schedule and was invisible to onboarding's Find-my-Code. These tests pin the
(code, seat) keying, the seat-aware resolver, and the "show both seats" lookup.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from nac_pay.app import onboarding_routes
from nac_pay.app import services
from nac_pay.app.services import (
    PilotDirectoryEntry,
    _fa_grids_by_seat,
    _resolve_pilot_schedule,
)
from nac_pay.parsers.master_schedule import PilotMonthSchedule
from nac_pay.schedule.labels import Position


def _sched(code: str, last: str, position: str) -> PilotMonthSchedule:
    return PilotMonthSchedule(
        pilot_code=code, last_name=last, year=2026, month=8,
        line_value=Decimal("100"), monthly_floor=Decimal("100"),
        days=(), position=position,
    )


# ── _fa_grids_by_seat: both seats survive the slot merge ────────────────


def test_fa_grids_by_seat_keeps_both_pilots_sharing_a_code(monkeypatch):
    fo = {"PBG": _sched("PBG", "BAGIAN", "FO"), "DFI": _sched("DFI", "FISHER", "FO")}
    ca = {"PBG": _sched("PBG", "BIAGIONI", "CPT")}

    def fake_parse(path: str):
        return fo if "fo" in path.lower() else ca

    monkeypatch.setattr(services, "_parse_master_schedule", fake_parse)
    grids = _fa_grids_by_seat([Path("fo_sheet.pdf"), Path("ca_sheet.pdf")])

    assert set(grids) == {("PBG", "FO"), ("DFI", "FO"), ("PBG", "CPT")}
    assert grids[("PBG", "FO")].last_name == "BAGIAN"
    assert grids[("PBG", "CPT")].last_name == "BIAGIONI"   # not shadowed


# ── _resolve_pilot_schedule: pick the pilot's own seat ──────────────────


def test_resolver_picks_exact_seat():
    grids = {
        ("PBG", "FO"): _sched("PBG", "BAGIAN", "FO"),
        ("PBG", "CPT"): _sched("PBG", "BIAGIONI", "CPT"),
    }
    assert _resolve_pilot_schedule(grids, "PBG", Position.FO).last_name == "BAGIAN"
    assert _resolve_pilot_schedule(grids, "PBG", Position.CPT).last_name == "BIAGIONI"


def test_resolver_unique_code_falls_back_across_seat_mismatch():
    """A non-colliding code resolves even if the profile's seat doesn't match
    the sheet's (seat undetected, single-seat month, or legacy profile)."""
    grids = {("DFI", "FO"): _sched("DFI", "FISHER", "FO")}
    assert _resolve_pilot_schedule(grids, "DFI", Position.CPT).last_name == "FISHER"


def test_resolver_colliding_code_without_seat_match_is_none():
    grids = {
        ("PBG", "FO"): _sched("PBG", "BAGIAN", "FO"),
        ("PBG", "CPT"): _sched("PBG", "BIAGIONI", "CPT"),
    }
    # No seat recorded and the code is ambiguous → refuse to guess.
    assert _resolve_pilot_schedule(grids, "PBG", "") is None


# ── code-lookup: show BOTH seats for a shared code ──────────────────────


def test_code_lookup_returns_both_seats_for_shared_code(monkeypatch):
    entries = (
        PilotDirectoryEntry("PBG", "BAGIAN", "FO"),
        PilotDirectoryEntry("PBG", "BIAGIONI", "CPT"),
    )
    monkeypatch.setattr(
        onboarding_routes, "shared_pilot_directory",
        lambda *a, **k: ("August 2026", entries),
    )
    resp = onboarding_routes.onboarding_code_lookup(code="PBG")
    body = json.loads(bytes(resp.body))
    assert len(body["matches"]) == 2
    assert {m["position"] for m in body["matches"]} == {"FO", "CPT"}
    by_seat = {m["position"]: m["last_name"] for m in body["matches"]}
    assert by_seat == {"FO": "BAGIAN", "CPT": "BIAGIONI"}


def test_code_lookup_last_name_carries_seat(monkeypatch):
    entries = (
        PilotDirectoryEntry("PBG", "BAGIAN", "FO"),
        PilotDirectoryEntry("PBG", "BIAGIONI", "CPT"),
    )
    monkeypatch.setattr(
        onboarding_routes, "shared_pilot_directory",
        lambda *a, **k: ("August 2026", entries),
    )
    resp = onboarding_routes.onboarding_code_lookup(last_name="Biagioni")
    body = json.loads(bytes(resp.body))
    assert len(body["matches"]) == 1
    assert body["matches"][0]["code"] == "PBG"
    assert body["matches"][0]["position"] == "CPT"
