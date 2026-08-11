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
#
# NOTE on DayDetailData.duty_hours / .duty_rig_pch: those are DISPLAY
# fields (computed in _day_duty_window from raw feed leg times) — Task 6
# owns making the day-detail card reflect a DUTY_CORRECTION. Asserting on
# them here would test the card, not the money; these tests assert on the
# engine's credited value instead (Trip.effective_pch on pr.updated_month),
# which is what actually reaches the pilot's pay.


def _flight_leg(uid, start, end, flight="768"):
    """A real FlightLegEvent (not a mock) for building synthetic
    ReconciledTrip fixtures below."""
    from datetime import datetime, timezone

    from nac_pay.parsers import FlightLegEvent

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


def _reconciled_trip(legs, flight_sequence="768/768/768"):
    from nac_pay.parsers import MatchStatus, ReconciledTrip

    return ReconciledTrip(
        flight_sequence=flight_sequence,
        legs=legs,
        packet_trip=None,
        match_status=MatchStatus.UNMATCHED_NO_PACKET,
        first_dt_utc=legs[0].dt_start_utc,
        last_dt_utc=legs[-1].dt_end_utc,
        actual_block_hours=Decimal("6.00"),
    )


def _duty_correction_version(
    *, date_iso, seq, created_at, duty_hours, duty_on_local, duty_off_local,
    pch_value=Decimal("4.17"),
):
    """A real UserAssignmentVersion (not a mock), built directly so the
    test can control seq/created_at independently — the store's .save()
    always stamps created_at = now(), which can't reproduce the
    seq-vs-recency mismatch IMPORTANT-4 covers."""
    from nac_pay.storage import UserAssignmentVersion, VersionEntryMode, VersionType

    return UserAssignmentVersion(
        user_id="default", date_iso=date_iso, seq=seq,
        version_type=VersionType.DUTY_CORRECTION, correction_of=None,
        assignment_id="768", entry_mode=VersionEntryMode.DETAILED,
        pch_value=pch_value, block_hours=None, duty_hours=duty_hours,
        tafb_hours=None, deadhead_pch=None, workdays=None,
        duty_on_local=duty_on_local, duty_off_local=duty_off_local,
        reason_code="FLOWN", premium_category="NONE", notes="",
        created_at=created_at,
    )


def test_duty_correction_flows_into_the_pipeline_recompute():
    """A stored DUTY_CORRECTION changes the trip's CREDITED pch — the
    money the pilot is actually paid — not merely a display field.

    Mutation-verified: deleting ``duty_overrides=duty_overrides,`` from
    the ``apply_actuals_to_month`` call in ``_pipeline`` makes this FAIL
    (see task-5-report.md fix-round-1 section for the pasted output)."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import DEFAULT_USER_ID
    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    before = _pipeline(2026, 6, DEFAULT_USER_ID)
    before_trip = next(t for t in before.updated_month.trips if "768" in t.trip_id)
    assert before_trip.effective_pch == Decimal("4.17")

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

    after = _pipeline(2026, 6, DEFAULT_USER_ID)
    after_trip = next(t for t in after.updated_month.trips if "768" in t.trip_id)
    # duty 20.00h → duty-rig 10.00 → recomputed trip_pch beats published
    # 4.17, so effective_pch (what the engine actually pays) moves to it.
    assert after_trip.effective_pch == Decimal("10.00")


def test_duty_correction_filed_on_a_later_leg_day_still_reaches_the_trip():
    """The bundled June 2026 packet has no real multi-day pairing (every
    trip in it is 1 workday — verified against the live parse), so this
    exercises the ACTUAL mapping helper in ``services.py`` (not a mock)
    against a synthetic 3-day ``ReconciledTrip`` built from real
    ``FlightLegEvent``/``ReconciledTrip`` dataclasses. It proves a
    correction filed on day 2 of a pairing resolves to the trip's FIRST
    local date — the key ``apply_actuals_to_month`` actually looks up —
    rather than being silently dropped."""
    from nac_pay.app.services import _resolve_duty_override_key

    # A 3-day pairing: legs on 2026-06-01 (first local date), 06-02, 06-03
    # (all UTC — ANC is UTC-8 in June, so these UTC dates equal local dates
    # comfortably mid-day, no boundary ambiguity).
    legs = (
        _flight_leg("l1", (2026, 6, 1, 14, 0), (2026, 6, 1, 16, 0)),
        _flight_leg("l2", (2026, 6, 2, 14, 0), (2026, 6, 2, 16, 0)),
        _flight_leg("l3", (2026, 6, 3, 14, 0), (2026, 6, 3, 16, 0)),
    )
    trip = _reconciled_trip(legs)

    # Filed on day 2 of the pairing — not the trip's first local date.
    key = _resolve_duty_override_key((trip,), "2026-06-02")
    assert key == "2026-06-01"

    # Filed on day 3 — same trip, same key.
    key3 = _resolve_duty_override_key((trip,), "2026-06-03")
    assert key3 == "2026-06-01"

    # A date that matches no trip at all falls back to itself.
    orphan = _resolve_duty_override_key((trip,), "2026-06-09")
    assert orphan == "2026-06-09"


def test_build_duty_overrides_uses_trip_mapping_not_the_filed_date():
    """IMPORTANT 2: proves the DICT-BUILDING call site actually uses
    _resolve_duty_override_key, not the correction's raw date_iso.

    Mutation-verified: changing ``key = _resolve_duty_override_key(...)``
    to ``key = date_iso`` inside ``_build_duty_overrides`` makes this
    FAIL — the result would key on "2026-06-02" instead of the trip's
    first local date "2026-06-01"."""
    from nac_pay.app.services import _build_duty_overrides

    legs = (
        _flight_leg("l1", (2026, 6, 1, 14, 0), (2026, 6, 1, 16, 0)),
        _flight_leg("l2", (2026, 6, 2, 14, 0), (2026, 6, 2, 16, 0)),
        _flight_leg("l3", (2026, 6, 3, 14, 0), (2026, 6, 3, 16, 0)),
    )
    trip = _reconciled_trip(legs)

    v = _duty_correction_version(
        date_iso="2026-06-02", seq=1, created_at="2026-06-02T10:00:00",
        duty_hours=Decimal("20.00"), duty_on_local="03:00", duty_off_local="23:00",
    )

    result = _build_duty_overrides({"2026-06-02": [v]}, (trip,))
    assert result == {"2026-06-01": Decimal("20.00")}


def test_build_duty_overrides_prefers_latest_created_at_over_seq():
    """IMPORTANT 4: seq is allocated per (user, date) — see
    assignment_versions.py's ``save()`` — so it is not a valid recency
    ordering ACROSS different dates of the same pairing. This pins two
    corrections on different dates of ONE pairing where seq order and
    created_at order DISAGREE: the day-1 correction has a HIGHER seq
    (edited twice) but an EARLIER created_at than the day-3 correction,
    which was filed later and must win.

    Mutation-verified: reverting the recency comparison in
    ``_build_duty_overrides`` back to ``v.seq`` (as the brief originally
    specified) makes this FAIL — it would pick the day-1 correction's
    15.00h instead of the later day-3 correction's 22.00h."""
    from nac_pay.app.services import _build_duty_overrides

    legs = (
        _flight_leg("l1", (2026, 6, 1, 14, 0), (2026, 6, 1, 16, 0)),
        _flight_leg("l2", (2026, 6, 2, 14, 0), (2026, 6, 2, 16, 0)),
        _flight_leg("l3", (2026, 6, 3, 14, 0), (2026, 6, 3, 16, 0)),
    )
    trip = _reconciled_trip(legs)

    day1_correction = _duty_correction_version(
        date_iso="2026-06-01", seq=3, created_at="2026-06-05T08:00:00",
        duty_hours=Decimal("15.00"), duty_on_local="04:00", duty_off_local="19:00",
    )
    day3_correction = _duty_correction_version(
        date_iso="2026-06-03", seq=1, created_at="2026-06-06T09:00:00",
        duty_hours=Decimal("22.00"), duty_on_local="02:00", duty_off_local="00:00",
    )

    result = _build_duty_overrides(
        {"2026-06-01": [day1_correction], "2026-06-03": [day3_correction]},
        (trip,),
    )
    # Both dates resolve to the trip's first local date "2026-06-01"; the
    # LATER-filed correction (day 3, created_at 06-06) must win despite
    # its lower seq.
    assert result == {"2026-06-01": Decimal("22.00")}


def test_duty_override_key_resolves_via_overnight_leg_spill():
    """MINOR 5: a trip's last leg landing after ANC local midnight must
    extend the trip's covered dates via that leg's END date, not just leg
    START dates — the same idiom ReconciledTrip.calendar_days_touched
    uses (broken 3 times in this repo's history per the brief). A
    correction filed on the spill date must still resolve to the trip's
    first local date.

    Mutation-verified: changing ``if rt.legs:`` to ``if False:`` inside
    ``_resolve_duty_override_key`` makes this FAIL — "2026-06-02" would
    then match no trip (only reachable via the end-date spill) and fall
    back to itself instead of resolving to "2026-06-01"."""
    from nac_pay.app.services import _resolve_duty_override_key

    # dt_start 2026-06-01 20:00 UTC = June 1, 12:00 local (noon).
    # dt_end   2026-06-02 09:30 UTC = June 2, 01:30 local (past midnight).
    # No leg START touches June 2 — only the END does.
    leg = _flight_leg("l1", (2026, 6, 1, 20, 0), (2026, 6, 2, 9, 30))
    trip = _reconciled_trip((leg,))

    key = _resolve_duty_override_key((trip,), "2026-06-02")
    assert key == "2026-06-01"
