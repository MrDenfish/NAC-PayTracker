"""Settings GET/POST tests."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import load_dashboard, load_persisted_profile


client = TestClient(app)
D = Decimal


# ── GET ────────────────────────────────────────────────────────────────


def test_settings_get_renders_default_values():
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Dennis FISHER" in r.text
    assert "124.59" in r.text
    assert 'href="/settings" class="nav-link nav-link--active"' in r.text


def test_settings_get_shows_saved_banner_when_query_present():
    r = client.get("/settings?saved=1")
    assert r.status_code == 200
    assert "Saved." in r.text


# ── POST ───────────────────────────────────────────────────────────────


def test_settings_post_persists_profile_changes():
    r = client.post(
        "/settings",
        data={
            "name": "Dennis FISHER",
            "position": "CPT",
            "hourly_rate": "150.00",
            "sick_bank_days": "12",
            "pto_bank_days": "20",
            "feed_url": "https://feed.example.com/x.ics",
            "feed_auto_update": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"

    persisted = load_persisted_profile()
    assert persisted.profile.position.value == "CPT"
    assert persisted.profile.hourly_rate == D("150.00")
    assert persisted.profile.sick_bank_days == 12
    assert persisted.profile.pto_bank_days == 20
    assert persisted.feed_url == "https://feed.example.com/x.ics"
    assert persisted.feed_auto_update is True


def test_settings_post_invalidates_pipeline_cache():
    """A Settings save must clear the cache so the next pay calculation
    uses the new hourly_rate. We bump from $124.59 to $200 and verify
    the dashboard total changes proportionally."""
    initial = load_dashboard(2026, 6)
    initial_pay = initial.total_pay

    client.post(
        "/settings",
        data={
            "name": initial.pilot.name,
            "position": initial.pilot.position.value,
            "hourly_rate": "200.00",
            "sick_bank_days": "0",
            "pto_bank_days": "0",
            "feed_url": "",
            "feed_auto_update": "",
        },
        follow_redirects=False,
    )
    after = load_dashboard(2026, 6)
    # Same 65.78 PCH × new $200.00 = $13,156.00; cache must have refreshed.
    assert after.total_pay == D("13156.00")
    assert after.total_pay != initial_pay


def test_settings_post_rejects_negative_rate():
    r = client.post(
        "/settings",
        data={
            "name": "x", "position": "FO",
            "hourly_rate": "-1.00",
            "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_settings_post_rejects_non_decimal_rate():
    r = client.post(
        "/settings",
        data={
            "name": "x", "position": "FO",
            "hourly_rate": "abc",
            "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_settings_post_rejects_invalid_position():
    r = client.post(
        "/settings",
        data={
            "name": "x", "position": "WIZARD",
            "hourly_rate": "100",
            "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
