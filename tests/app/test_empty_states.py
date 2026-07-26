"""Missing-month and unknown-pilot pages render HTML, not raw JSON.

Covers the five data routes (calendar/pay/compare/discrepancies/day) when
``_pipeline`` raises ``MonthDataError`` for either flavor — no documents at
all for the month, or the pilot's code isn't in the published Final Award —
plus a check that the dashboard's separate (bare ``except ValueError``)
empty state still works now that ``MonthDataError`` subclasses it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import invalidate_caches
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.onboarding import mark_completed
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    PersistedPilotProfile,
    PilotProfileStore,
    SharedDocumentsStore,
    get_data_dir,
)

_FA_FIXTURE = "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"
_PACKET_FIXTURE = "MAY  2026  Trip Pairing Packet.pdf"


def _fixture(name: str) -> bytes:
    return (Path(__file__).resolve().parents[2] / "docs" / name).read_bytes()


def _publish_shared(year: int, month: int) -> None:
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_packet(year, month, "packet-shared.pdf", _fixture(_PACKET_FIXTURE), uploaded_by="admin")
    invalidate_caches()


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signed_up_docless_user(monkeypatch, email: str) -> tuple[TestClient, str]:
    """Sign up + verify + mark onboarding completed, with NO documents and
    NO pilot profile saved anywhere — a brand-new real pilot on their first
    visit to a month page."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    client.post(
        "/signup",
        data={
            "email": email,
            "password": "long enough password",
            "confirm": "long enough password",
        },
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    uid = find_by_email(email)
    assert uid is not None
    mark_completed(uid)
    return client, uid


def test_calendar_missing_month_renders_html(monkeypatch):
    client, _uid = _signed_up_docless_user(monkeypatch, "nodocs1@example.com")
    r = client.get("/calendar?year=2026&month=7")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "haven't been published" in r.text
    assert "{" not in r.text[:1]  # not a JSON body


def test_all_five_routes_render_html(monkeypatch):
    client, _uid = _signed_up_docless_user(monkeypatch, "nodocs2@example.com")
    for path in ("/calendar", "/pay", "/compare", "/discrepancies", "/day/2026-07-15"):
        r = client.get(path if "day" in path else f"{path}?year=2026&month=7")
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("text/html"), path


def test_unknown_pilot_code_flavor(monkeypatch):
    _publish_shared(2026, 5)
    client, uid = _signed_up_docless_user(monkeypatch, "zzz@example.com")
    PilotProfileStore(get_data_dir(), uid).save(
        PersistedPilotProfile(
            profile=PilotProfile(
                pilot_id="ZZZ",
                name="Zed ZULU",
                position=Position.FO,
                hourly_rate=Decimal("100.00"),
            )
        )
    )
    r = client.get("/calendar?year=2026&month=5")
    assert r.status_code == 404
    assert "ZZZ" in r.text
    assert "check your pilot code in Settings" in r.text


def test_dashboard_still_renders_empty_state_for_docless_user(monkeypatch):
    """MonthDataError subclasses ValueError, so the dashboard's own bare
    ``except ValueError`` (a different, pre-existing empty state) keeps
    catching it and still renders ``dashboard_empty.html`` with a 200."""
    client, _uid = _signed_up_docless_user(monkeypatch, "nodocs3@example.com")
    r = client.get("/")
    assert r.status_code == 200
    assert "No data for this month yet" in r.text
