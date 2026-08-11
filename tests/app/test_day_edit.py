"""Day detail edit form tests — POST override flow + end-to-end engine effect."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import load_day, load_pay_breakdown
from nac_pay.storage import DayOverrideStore, get_data_dir


client = TestClient(app)
D = Decimal


# ── Render ─────────────────────────────────────────────────────────────


def test_day_detail_renders_active_form_for_flt_day():
    r = client.get("/day/2026-06-12")
    assert r.status_code == 200
    # Form posts to the same URL, contains a Save button, selects are NOT disabled.
    assert 'action="/day/2026-06-12" method="post"' in r.text
    assert "Save override" in r.text
    # Reason select has Flown as the selected option.
    assert '<option value="FLOWN" selected>' in r.text


def test_day_detail_off_day_does_not_show_save_button():
    """Off days shouldn't surface the edit form save action."""
    r = client.get("/day/2026-06-07")
    assert r.status_code == 200
    assert "Save override" not in r.text
    assert "Off days don" in r.text


# ── POST: persistence ─────────────────────────────────────────────────


def test_day_post_persists_override_and_redirects():
    r = client.post(
        "/day/2026-06-12",
        data={
            "reason_code": "SICK",
            "premium_category": "NONE",
            "entry_mode": "SIMPLE",
            "custom_multiplier": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/day/2026-06-12?saved=1"

    store = DayOverrideStore(get_data_dir())
    saved = store.load_all().get("2026-06-12")
    assert saved is not None
    assert saved.reason_code == "SICK"


def test_day_post_invalid_date_returns_400():
    r = client.post(
        "/day/not-a-date",
        data={"reason_code": "FLOWN"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_day_get_after_post_shows_saved_banner_and_override_chip():
    client.post(
        "/day/2026-06-12",
        data={
            "reason_code": "SICK",
            "premium_category": "NONE",
            "entry_mode": "SIMPLE",
            "custom_multiplier": "",
        },
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-12?saved=1")
    assert r.status_code == 200
    assert "Saved." in r.text
    # Override-active note
    assert "Custom override is active" in r.text


# ── End-to-end: override affects engine ───────────────────────────────


def test_override_changes_pay_breakdown_categories():
    """Override June 12 FLT 768 from FLOWN/NONE to FLOWN/OPEN_TIME_MID_MONTH.
    The 4.17 PCH should move from Regular Pay to the Open Time row."""
    before = load_pay_breakdown(2026, 6)
    assert any(r.pay_type == "Regular Pay" for r in before.earning_rows)
    assert not any(r.pay_type == "Open Time" for r in before.earning_rows)

    client.post(
        "/day/2026-06-12",
        data={
            "reason_code": "FLOWN",
            "premium_category": "OPEN_TIME_MID_MONTH",
            "entry_mode": "SIMPLE",
            "custom_multiplier": "",
        },
        follow_redirects=False,
    )

    after = load_pay_breakdown(2026, 6)
    # Open Time row appears with 4.17 PCH at 1.5×.
    open_time = next(r for r in after.earning_rows if r.pay_type == "Open Time")
    assert open_time.pch == D("4.17")
    assert open_time.multiplier == D("1.5")
    # 4.17 × 124.59 × 1.5 = 779.3115 → $779.31
    assert open_time.amount == D("779.31")


def test_override_reason_sick_moves_pch_to_sick_category():
    """Override the June 12 trip's reason to SICK. The 4.17 PCH should move
    from Regular Pay to the Sick category (and stay at 1.0×)."""
    client.post(
        "/day/2026-06-12",
        data={
            "reason_code": "SICK",
            "premium_category": "NONE",
            "entry_mode": "SIMPLE",
            "custom_multiplier": "",
        },
        follow_redirects=False,
    )
    after = load_pay_breakdown(2026, 6)
    sick = next((r for r in after.earning_rows if r.pay_type == "Sick"), None)
    assert sick is not None
    # KEEP_PROTECTED effect: trip's published value carries through at 1.0×.
    assert sick.pch == D("4.17")
    assert sick.amount == D("519.54")


# ── Duty-time override wiring (Task 5) ──────────────────────────────────


def test_duty_correction_flows_into_the_pipeline_recompute():
    """A stored DUTY_CORRECTION changes the day's credited duty rig."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import DEFAULT_USER_ID
    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    before = load_day(2026, 6, 12)
    assert before.duty_hours is not None

    UserAssignmentVersionStore(user_id=DEFAULT_USER_ID).save(
        date_iso="2026-06-12",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="768",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("4.17"),
        duty_hours=Decimal("20.00"),
        duty_on_local="03:00",
        duty_off_local="23:00",
    )
    _pipeline.cache_clear()

    after = load_day(2026, 6, 12)
    assert after.duty_hours == Decimal("20.00")
    assert after.duty_rig_pch == Decimal("10.00")


def test_duty_correction_filed_on_a_later_leg_day_still_reaches_the_trip():
    """The bundled June 2026 packet has no real multi-day pairing (every
    trip in it is 1 workday — verified against the live parse), so this
    exercises the ACTUAL mapping helper in ``services.py`` (not a mock)
    against a synthetic 3-day ``ReconciledTrip`` built from real
    ``FlightLegEvent``/``ReconciledTrip`` dataclasses. It proves a
    correction filed on day 2 of a pairing resolves to the trip's FIRST
    local date — the key ``apply_actuals_to_month`` actually looks up —
    rather than being silently dropped."""
    from datetime import datetime, timezone
    from decimal import Decimal as D2

    from nac_pay.app.services import _resolve_duty_override_key
    from nac_pay.parsers import FlightLegEvent, MatchStatus, ReconciledTrip

    def leg(uid, start, end, flight="768"):
        return FlightLegEvent(
            uid=uid,
            dt_start_utc=datetime(*start, tzinfo=timezone.utc),
            dt_end_utc=datetime(*end, tzinfo=timezone.utc),
            flight_no_raw=f"NC{flight}",
            flight_no_short=flight,
            origin="ANC",
            destination="BRW",
            tail="N409YK",
            customer="Northern Air Cargo",
            captain="",
            first_officer="Dennis FISHER",
        )

    # A 3-day pairing: legs on 2026-06-01 (first local date), 06-02, 06-03
    # (all UTC — ANC is UTC-8 in June, so these UTC dates equal local dates
    # comfortably mid-day, no boundary ambiguity).
    legs = (
        leg("l1", (2026, 6, 1, 14, 0), (2026, 6, 1, 16, 0)),
        leg("l2", (2026, 6, 2, 14, 0), (2026, 6, 2, 16, 0)),
        leg("l3", (2026, 6, 3, 14, 0), (2026, 6, 3, 16, 0)),
    )
    trip = ReconciledTrip(
        flight_sequence="768/768/768",
        legs=legs,
        packet_trip=None,
        match_status=MatchStatus.UNMATCHED_NO_PACKET,
        first_dt_utc=legs[0].dt_start_utc,
        last_dt_utc=legs[-1].dt_end_utc,
        actual_block_hours=D2("6.00"),
    )

    # Filed on day 2 of the pairing — not the trip's first local date.
    key = _resolve_duty_override_key((trip,), "2026-06-02")
    assert key == "2026-06-01"

    # Filed on day 3 — same trip, same key.
    key3 = _resolve_duty_override_key((trip,), "2026-06-03")
    assert key3 == "2026-06-01"

    # A date that matches no trip at all falls back to itself.
    orphan = _resolve_duty_override_key((trip,), "2026-06-09")
    assert orphan == "2026-06-09"
