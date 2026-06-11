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
