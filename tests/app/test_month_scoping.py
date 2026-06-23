"""Multi-month feed scoping + per-user month dropdown.

Two bugs surfaced once the hourly updater started writing the full BlueOne
roster (which spans months) into each month's feed.ics:

1. A next-month trip leaked into this month as a phantom open-time pickup,
   inflating PCH ("blending of June and July data").
2. The dashboard month dropdown showed the bundled default-user months
   (May+June) for every user, because the loaders called available_months()
   without the user_id.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from nac_pay.app import services as s
from nac_pay.app.services import _pipeline, available_months, invalidate_caches, load_dashboard
from nac_pay.parsers import ParsedFeed
from nac_pay.parsers.ical_feed import FlightLegEvent
from nac_pay.parsers.reconciliation import MatchStatus, ReconciledTrip, ReconciliationResult

from .test_phase_h import _bootstrap, _docs_dir


# ── Unit: the scoping helpers ────────────────────────────────────────


def _leg(mon: int, day: int) -> FlightLegEvent:
    dt = datetime(2026, mon, day, 15, tzinfo=timezone.utc)
    return FlightLegEvent(
        uid="u", dt_start_utc=dt, dt_end_utc=dt, flight_no_raw="NC1",
        flight_no_short="1", origin="ANC", destination="BRW", tail="N1",
        customer="c", captain="c", first_officer="f",
    )


def _rt(mon: int, day: int, status: MatchStatus = MatchStatus.MATCHED) -> ReconciledTrip:
    dt = datetime(2026, mon, day, 15, tzinfo=timezone.utc)
    return ReconciledTrip(
        flight_sequence="1", legs=(_leg(mon, day),), packet_trip=None,
        match_status=status, first_dt_utc=dt, last_dt_utc=dt,
        actual_block_hours=Decimal("1"),
    )


def test_reconciliation_filter_keeps_only_target_month():
    june, july = _rt(6, 10), _rt(7, 5)
    recon = ReconciliationResult(trips=(june, july), matched=(june, july), unmatched=())
    out = s._filter_reconciliation_to_month(recon, 2026, 6)
    assert out.trips == (june,)
    assert out.matched == (june,)


def test_reconciliation_filter_attributes_boundary_trip_to_start_month():
    """A trip whose first leg is June 30 belongs to June even if it spills
    into July; July's pipeline must NOT also claim it (no double count)."""
    boundary = _rt(6, 30)
    recon = ReconciliationResult(trips=(boundary,), matched=(boundary,), unmatched=())
    assert s._filter_reconciliation_to_month(recon, 2026, 6).trips == (boundary,)
    assert s._filter_reconciliation_to_month(recon, 2026, 7).trips == ()


def test_feed_filter_scopes_each_event_type():
    feed = ParsedFeed(flight_legs=(_leg(6, 10), _leg(7, 5), _leg(7, 20)))
    out = s._filter_feed_to_month(feed, 2026, 6)
    assert len(out.flight_legs) == 1


# ── End-to-end: a multi-month feed doesn't inflate the month ─────────


def _ics_with_july_clone() -> bytes:
    """The bundled June feed with every event also cloned into July (dates
    shifted June→July). The July copies reconcile as MATCHED trips
    (768/768/769, 722/723/754/755) — verified to leak into June as a
    phantom open-time pickup without the month-scoping filter."""
    raw = (_docs_dir() / "iCal_schedule_feed.ics").read_text()
    blocks = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", raw, re.S)
    july = [b.replace("202606", "202607").replace("UID:", "UID:JULY-", 1) for b in blocks]
    return raw.replace(
        "END:VCALENDAR", "\r\n".join(july) + "\r\nEND:VCALENDAR",
    ).encode()


def _june_earned(uid: str) -> Decimal:
    invalidate_caches()
    return _pipeline(2026, 6, uid).engine_result.option3_earned


def test_july_clone_does_not_leak_into_june(monkeypatch):
    clean_client, clean_uid = _bootstrap(monkeypatch, "scope-clean@x.test")
    clean_pch = _june_earned(clean_uid)

    client, uid = _bootstrap(monkeypatch, "scope-clone@x.test")
    # Overwrite June's feed with the multi-month (June + July clone) version.
    client.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "ICAL_FEED"},
        files={"upload": ("f.ics", _ics_with_july_clone(), "text/calendar")},
        follow_redirects=False,
    )
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)

    # The July clone is scoped out: June's earned PCH is unchanged, and no
    # June trip / applied event carries a July date.
    assert pr.engine_result.option3_earned == clean_pch
    assert "2026-07" not in str(pr.applied_events)
    for trip in pr.updated_month.trips:
        for d in getattr(trip, "dates", ()):
            assert d.month == 6


# ── Dropdown: month switcher reflects the logged-in user ─────────────


def test_dashboard_dropdown_uses_logged_in_users_months(monkeypatch):
    client, uid = _bootstrap(monkeypatch, "dropdown@x.test")
    # Give the user a July as well (reuse June bytes — the dropdown is built
    # from which months have docs, not their contents).
    for kind, source in [
        ("FINAL_AWARD", "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf"),
        ("TRIP_PACKET", "JUNE 2026 Trip Pairing Packet.pdf"),
    ]:
        client.post(
            "/documents/upload",
            data={"year": "2026", "month": "7", "kind": kind},
            files={"upload": ("d.pdf", (_docs_dir() / source).read_bytes(),
                              "application/octet-stream")},
            follow_redirects=False,
        )
    invalidate_caches()

    months = {(y, m) for (y, m, _label) in load_dashboard(2026, 6, uid).available_months}
    assert (2026, 7) in months and (2026, 6) in months
    # NOT the bundled default-user set (which would be May + June).
    assert (2026, 5) not in months
