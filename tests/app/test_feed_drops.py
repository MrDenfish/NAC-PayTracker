"""LEA day-status consumer end-to-end: feed drops + sick seeding.

Grounded in the 2026-07-24 live experiment: an approved mid-month drop
arrives as an all-day ``LEA - TRIP DROP`` event (legs + R/S removed), and
BlueOne posts ``LEA - SICK`` on real sick days.
"""

from __future__ import annotations

from tests.app.test_reassign import _bootstrap_user_with_june, _docs_dir


def _upload_feed_with_lea(client, label: str, ymd: str, next_ymd: str) -> None:
    """Re-upload the bundled June feed with an all-day LEA event injected."""
    base = (_docs_dir() / "iCal_schedule_feed.ics").read_text()
    vevent = (
        "BEGIN:VEVENT\n"
        f"UID:lea-test-{label.replace(' ', '-')}-{ymd}\n"
        f"DTSTART:{ymd}T080000Z\n"
        f"DTEND:{next_ymd}T075900Z\n"
        f"SUMMARY:LEA - {label}\n"
        f"DESCRIPTION: {label}\n"
        "END:VEVENT\n"
        "END:VCALENDAR"
    )
    modified = base.replace("END:VCALENDAR", vevent)
    client.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "ICAL_FEED"},
        files={"upload": ("f.ics", modified.encode(), "application/octet-stream")},
        follow_redirects=False,
    )


def test_feed_drop_proposes_then_confirm_forfeits(monkeypatch):
    """LEA - TRIP DROP on a trip day (June 12, FLT 768 @ 4.17): the day page
    shows the confirm card and keeps paying published; confirming files a
    DROP version — the day flips to DROPPED and forfeits."""
    client, _ = _bootstrap_user_with_june(monkeypatch, "feeddrop1@x.test")
    _upload_feed_with_lea(client, "TRIP DROP", "20260612", "20260613")

    # The static legend always shows a "⚠ confirm" sample — the CELL badge
    # is the one carrying the title attribute.
    _cell_badge = "open the day to confirm or reject"

    body = client.get("/day/2026-06-12").text
    assert "Company drop detected" in body
    assert "Confirm drop — forfeit 4.17" in body
    assert ">DROPPED<" not in body                  # still paying published
    cal = client.get("/calendar?ym=2026-6").text
    assert _cell_badge in cal                        # amber badge on the cell

    client.post("/day/2026-06-12/feed-drop/confirm", follow_redirects=False)
    body = client.get("/day/2026-06-12").text
    assert ">DROPPED<" in body
    assert "Company drop detected" not in body       # card gone once dropped
    cal = client.get("/calendar?ym=2026-6").text
    assert _cell_badge not in cal


def test_feed_drop_reject_keeps_published_and_silences_badge(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "feeddrop2@x.test")
    _upload_feed_with_lea(client, "TRIP DROP", "20260612", "20260613")

    client.post("/day/2026-06-12/feed-drop/reject", follow_redirects=False)
    body = client.get("/day/2026-06-12").text
    assert "Company drop dismissed" in body
    assert ">DROPPED<" not in body
    assert ">FLT<" in body                           # still the scheduled trip
    cal = client.get("/calendar?ym=2026-6").text
    assert "open the day to confirm or reject" not in cal


def test_lea_sick_autoseeds_reason_and_pilot_override_wins(monkeypatch):
    """LEA - SICK on June 2 (FLT 722/750): the SICK tag + absence tint appear
    with zero manual entry (the July 2/3 case); an explicit pilot override
    back to FLOWN still wins."""
    client, _ = _bootstrap_user_with_june(monkeypatch, "feedsick@x.test")
    _upload_feed_with_lea(client, "SICK", "20260602", "20260603")

    body = client.get("/day/2026-06-02").text
    assert ">SICK<" in body
    assert "duty-bg--absence" in body

    client.post(
        "/day/2026-06-02",
        data={"reason_code": "FLOWN", "premium_category": "NONE",
              "entry_mode": "SIMPLE", "custom_multiplier": ""},
        follow_redirects=False,
    )
    body = client.get("/day/2026-06-02").text
    assert ">FLT<" in body
    assert "duty-bg--absence" not in body
