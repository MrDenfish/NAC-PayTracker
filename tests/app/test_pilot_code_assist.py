"""Pilot-code assist on onboarding step 1: live check + find-my-code,
backed by the shared (admin-published) Final Award."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import invalidate_caches, normalize_name, shared_pilot_directory
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.storage import SharedDocumentsStore, get_data_dir

_FA_FIXTURE = "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"
_PACKET_FIXTURE = "MAY  2026  Trip Pairing Packet.pdf"

# Known from the bundled fixture (see services.DEFAULT_PILOT / other tests
# that load the default pilot against this same FA).
_KNOWN_CODE = "DFI"
_KNOWN_LASTNAME_PREFIX = "fish"


def _fixture(name: str) -> bytes:
    return (Path(__file__).resolve().parents[2] / "docs" / name).read_bytes()


def _publish_shared(year: int, month: int) -> None:
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_packet(year, month, "packet-shared.pdf", _fixture(_PACKET_FIXTURE), uploaded_by="admin")
    invalidate_caches()


def _publish_shared_current_month() -> tuple[int, int]:
    today = date.today()
    _publish_shared(today.year, today.month)
    return today.year, today.month


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    """Signs up + verifies a user but deliberately does NOT mark onboarding
    completed — the pilot-code assist is exercised mid-onboarding."""
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    uid = find_by_email(email)
    assert uid is not None
    from sqlalchemy import select

    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserRow
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.subscription_status = "ACTIVE"
    return uid


# ── services.shared_pilot_directory ─────────────────────────────────


def test_shared_pilot_directory_prefers_current_then_falls_back():
    _publish_shared(2026, 5)
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert "May 2026" in label
    assert all(len(e.code) <= 4 for e in directory)
    assert all(e.position == "FO" for e in directory)   # May FA is the FO sheet
    assert directory


def test_shared_pilot_directory_empty_when_nothing_published():
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert label == ""
    assert directory == ()


def test_shared_pilot_directory_future_only_still_used():
    # Launch scenario: only NEXT month's FA has been published yet (no
    # current or past month). Codes are stable, so this is still a valid
    # check — the assist should use the nearest future month rather than
    # going empty.
    _publish_shared(2026, 9)
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert "September 2026" in label
    assert directory


def test_shared_pilot_directory_prefers_current_over_older():
    _publish_shared(2026, 5)   # older
    _publish_shared(2026, 7)   # current
    label, directory = shared_pilot_directory(today=date(2026, 7, 15))
    assert "July 2026" in label
    assert directory


# ── GET /onboarding/code-lookup ──────────────────────────────────────


def test_code_lookup_endpoint_by_code_and_lastname(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "quincy@example.com")

    r = client.get(f"/onboarding/code-lookup?code={_KNOWN_CODE}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["code"] == _KNOWN_CODE

    r2 = client.get(f"/onboarding/code-lookup?last_name={_KNOWN_LASTNAME_PREFIX}")
    assert r2.status_code == 200
    assert len(r2.json()["matches"]) >= 1

    r3 = client.get("/onboarding/code-lookup?code=ZZZ")
    assert r3.status_code == 200
    assert r3.json()["matches"] == []


def test_code_lookup_empty_when_no_shared_fa(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    _signup_and_verify(client, "rex@example.com")

    r = client.get("/onboarding/code-lookup?code=ZZZ")
    assert r.status_code == 200
    assert r.json() == {"month_label": "", "matches": []}


def test_code_lookup_last_name_normalized_prefix(monkeypatch):
    """Last-name matching is normalized-prefix (accents/apostrophes/
    hyphens/spaces folded), not a plain substring/prefix on the raw
    stored text — a query with folded-away punctuation still matches."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "uma@example.com")

    # First 3 letters, lowercase — plain normalized-prefix case.
    r = client.get("/onboarding/code-lookup?last_name=fis")
    assert r.status_code == 200
    codes = [m["code"] for m in r.json()["matches"]]
    assert _KNOWN_CODE in codes

    # A hyphen inserted mid-query is folded away by normalize_name before
    # the prefix comparison, so it still matches the real (unhyphenated)
    # fixture surname.
    r2 = client.get("/onboarding/code-lookup?last_name=fi-sh")
    assert r2.status_code == 200
    codes2 = [m["code"] for m in r2.json()["matches"]]
    assert _KNOWN_CODE in codes2


# ── normalize_name ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Muñoz", "MUNOZ"),
        ("O'Brien", "OBRIEN"),
        ("Smith-Jones", "SMITHJONES"),
        ("De La Cruz", "DELACRUZ"),
        ("muñoz", "MUNOZ"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


# ── POST /onboarding/profile hard block (Find-my-Code is the only path) ──


def test_profile_post_code_not_on_directory_rerenders_with_error(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    uid = _signup_and_verify(client, "sara@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "Sara Pilot",
            "pilot_id": "ZZZ",
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Jinja auto-escapes the apostrophe in "isn't" (isn&#39;t) — assert on
    # the surrounding text instead of the raw contraction.
    assert "Code ZZZ" in r.text
    assert "Final Award" in r.text
    assert "Find my code" in r.text

    # The rejected code must NOT come back as a disabled, dead-end search
    # box: the directory has just proven "ZZZ" invalid, so the widget
    # drops it (empty hidden field, search re-enabled) — the error banner
    # already explains why, and re-searching should just work.
    lookup_input = re.search(r'<input[^>]*id="lastname-lookup"[^>]*>', r.text)
    assert lookup_input is not None
    assert "disabled" not in lookup_input.group(0)
    hidden_input = re.search(r'<input[^>]*id="pilot-id"[^>]*>', r.text)
    assert hidden_input is not None
    assert 'value=""' in hidden_input.group(0)

    from nac_pay.storage import PilotProfileStore, get_data_dir
    assert PilotProfileStore(get_data_dir(), uid).exists() is False


# ── GET /onboarding/profile stale-code recoverability ────────────────


def test_profile_get_existing_user_stale_code_is_recoverable(monkeypatch):
    """An existing user's saved pilot_id that the CURRENTLY published FA
    no longer recognizes (an admin republish that dropped them, or a
    stale/corrected signup) must not render as a permanently disabled,
    dead-end search box on GET — same recoverability guarantee as the
    POST re-render paths. Regression test: a first fix pass added the
    route-side correction but the template independently recomputed
    pilot_id_value from persisted.profile.pilot_id, so the corrected
    value never actually reached the page — verified dead live (saved
    ZZZ + published FA without ZZZ = stuck disabled box, stale hidden
    value, no Clear link)."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "olga@example.com")

    from decimal import Decimal

    from nac_pay.schedule import PilotProfile, Position
    from nac_pay.storage import PersistedPilotProfile, PilotProfileStore, get_data_dir

    PilotProfileStore(get_data_dir(), uid).save(
        PersistedPilotProfile(
            profile=PilotProfile(
                pilot_id="ZZZ", name="Olga Pilot",
                position=Position.FO, hourly_rate=Decimal("140.00"),
            ),
        )
    )
    _publish_shared_current_month()  # fixture FA does not contain "ZZZ"

    r = client.get("/onboarding/profile")
    assert r.status_code == 200

    lookup_input = re.search(r'<input[^>]*id="lastname-lookup"[^>]*>', r.text)
    assert lookup_input is not None
    assert "disabled" not in lookup_input.group(0)

    hidden_input = re.search(r'<input[^>]*id="pilot-id"[^>]*>', r.text)
    assert hidden_input is not None
    assert 'value=""' in hidden_input.group(0)
    assert "ZZZ" not in hidden_input.group(0)

    # Name/position/rate prefill still shows — only the stale code drops.
    assert 'value="Olga Pilot"' in r.text
    assert 'value="140.00"' in r.text


def test_profile_get_existing_user_code_kept_when_no_directory_published(monkeypatch):
    """Counterpart to the stale-code test above: with NO shared FA
    published at all, there is nothing to disprove the saved code against
    — a missing FA month must not wipe an existing user's (e.g. the
    author's own) valid code display."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "peggy@example.com")

    from decimal import Decimal

    from nac_pay.schedule import PilotProfile, Position
    from nac_pay.storage import PersistedPilotProfile, PilotProfileStore, get_data_dir

    PilotProfileStore(get_data_dir(), uid).save(
        PersistedPilotProfile(
            profile=PilotProfile(
                pilot_id="PEG", name="Peggy Pilot",
                position=Position.FO, hourly_rate=Decimal("140.00"),
            ),
        )
    )
    # Deliberately no _publish_shared_current_month() call.

    r = client.get("/onboarding/profile")
    assert r.status_code == 200
    hidden_input = re.search(r'<input[^>]*id="pilot-id"[^>]*>', r.text)
    assert hidden_input is not None
    assert 'value="PEG"' in hidden_input.group(0)


def test_profile_post_blank_name_with_valid_code_keeps_clear_link(monkeypatch):
    """A DIFFERENT error (blank name) must not disturb an already-valid,
    directory-confirmed pilot_id: it stays shown, with its server-rendered
    Clear link intact — recoverability only kicks in for a code the
    directory has actually rejected."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "nia@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "",
            "pilot_id": _KNOWN_CODE,
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Enter your display name" in r.text
    hidden_input = re.search(r'<input[^>]*id="pilot-id"[^>]*>', r.text)
    assert hidden_input is not None
    assert f'value="{_KNOWN_CODE}"' in hidden_input.group(0)
    # "id=\"clear-code\"" also appears inside the <script> block's JS
    # template literal, so check the SERVER-RENDERED #code-hint div
    # specifically — the Clear link must be there without any JS running.
    hint_div = re.search(r'<div id="code-hint"[^>]*>(.*?)</div>', r.text, re.S)
    assert hint_div is not None
    assert 'id="clear-code"' in hint_div.group(1)


def test_profile_post_no_shared_fa_rerenders_with_contact_admin(monkeypatch):
    """The real UI never lets a pilot type a code before a match is found —
    with no FA published, Find-my-Code can't return anything, so pilot_id
    is blank. The admin-contact message must win here, not the 2-4-letter
    shape check (checked ordering fix), and the pilot's other entered
    values must survive the re-render."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "seth@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "Seth Pilot",
            "pilot_id": "",
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Contact the site admin" in r.text
    assert 'value="Seth Pilot"' in r.text

    from nac_pay.storage import PilotProfileStore, get_data_dir
    assert PilotProfileStore(get_data_dir(), uid).exists() is False


def test_profile_post_known_code_passes_straight_through(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "tom@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "Tom Pilot",
            "pilot_id": _KNOWN_CODE,
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/feed"
