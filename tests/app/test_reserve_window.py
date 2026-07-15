"""R/S reserve-window display on the day page.

The feed's ``R/S - Reserve or Standby at <base>`` events were parsed but
never consumed, so the "RES part" of a fly-then-reserve pairing (e.g. the
pilot's July 16 2026 ``722/R1`` pickup: FLT 722 + FLT 723 + an R/S window)
was invisible on the day page. The Times card now shows the window,
attributed by Anchorage-local date.
"""

from __future__ import annotations

from nac_pay.app.services import invalidate_caches

from tests.app.test_reassign import _bootstrap_user_with_june

# An R/S window on June 2 (a bundled-FA trip day): 17:31–23:16 UTC
# = 09:31–15:16 Anchorage local (AKDT, UTC−8) — the July 16 shape.
_RS_FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//nac-pay-tests//reserve-window//
BEGIN:VEVENT
UID:9990002
DTSTART:20260602T173100Z
DTEND:20260602T231600Z
SUMMARY:R/S - Reserve or Standby at ANC
DESCRIPTION:1021S at ANC
END:VEVENT
END:VCALENDAR
"""


def _upload_rs_feed(client) -> None:
    client.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "ICAL_FEED"},
        files={"upload": ("f.ics", _RS_FEED, "application/octet-stream")},
        follow_redirects=False,
    )
    invalidate_caches()


def test_reserve_window_shows_on_day_page(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "rs-window@x.test")
    _upload_rs_feed(client)

    day = client.get("/day/2026-06-02")
    assert day.status_code == 200
    assert "Reserve window (R/S)" in day.text
    # 17:31–23:16 UTC rendered Anchorage-local (AKDT).
    assert "09:31" in day.text
    assert "15:16" in day.text
    assert "Reserve / standby at ANC" in day.text


def test_no_reserve_window_without_rs_event(monkeypatch):
    """A trip day with no R/S event on its date shows no reserve window.
    (The bundled June feed's R/S events live on other dates.)"""
    client, _ = _bootstrap_user_with_june(monkeypatch, "rs-none@x.test")

    day = client.get("/day/2026-06-02")
    assert day.status_code == 200
    assert "Reserve window (R/S)" not in day.text
