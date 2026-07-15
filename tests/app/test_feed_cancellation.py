"""Feed-signalled company cancellation (LEA - OFF/PAY PROTECTED).

Seen live 2026-07-15: the company cancelled the day's trip; BlueOne removed
its FLT legs from the feed and posted an all-day ``LEA - OFF/PAY PROTECTED``
event in their place. The pipeline marks the scheduled trip
``cancelled_pay_protected`` — published PCH still credited (a company action
never reduces pay), calendar cell flips to CANCELLED with the aid struck
through, and the day page carries an explanatory banner.
"""

from __future__ import annotations

from decimal import Decimal

from nac_pay.app.services import invalidate_caches

# Reuse the end-to-end harness (signup → verify → ACTIVE → upload bundled
# June 2026 docs) and the June-PCH probe.
from tests.app.test_reassign import _bootstrap_user_with_june, _june_total_pch

D = Decimal

# June 2 carries FA trip 722/750 (published 4.92) in the bundled June docs.
_CANCEL_FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//nac-pay-tests//feed-cancellation//
BEGIN:VEVENT
UID:9990001
DTSTART:20260602T080000Z
DTEND:20260603T075900Z
SUMMARY:LEA - OFF/PAY PROTECTED
END:VEVENT
END:VCALENDAR
"""


def _upload_cancellation_feed(client) -> None:
    """Re-upload the feed with the pay-protected LEA on June 2. The manual
    upload path merges with the stored feed (merge-preserve), so the bundled
    legs survive and the new LEA event is added alongside them."""
    client.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "ICAL_FEED"},
        files={"upload": ("f.ics", _CANCEL_FEED, "application/octet-stream")},
        follow_redirects=False,
    )
    invalidate_caches()


def test_cancelled_day_keeps_published_pch(monkeypatch):
    """Pay protection: the monthly earned PCH is IDENTICAL before and after
    the cancellation — the published value is still credited."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "cancel-pay@x.test")
    before = _june_total_pch(uid)

    _upload_cancellation_feed(client)
    after = _june_total_pch(uid)

    assert after == before, (
        f"pay-protected cancellation must not change PCH: {before} -> {after}"
    )


def test_cancelled_day_renders_on_calendar_and_day_page(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "cancel-render@x.test")
    _upload_cancellation_feed(client)

    cal = client.get("/calendar?ym=2026-6")
    assert cal.status_code == 200
    assert "CANCELLED" in cal.text
    # The cancelled aid is struck through / de-emphasized, not bold-active.
    assert "aid--cancelled" in cal.text

    day = client.get("/day/2026-06-02")
    assert day.status_code == 200
    assert "Company cancelled this trip" in day.text
    assert "OFF / PAY PROTECTED" in day.text
    # Published PCH still shown as the effective value.
    assert "4.92" in day.text
    # The header duty label flips to CANCELLED.
    assert "CANCELLED" in day.text


def test_cancellation_logs_company_cancellation_event(monkeypatch):
    from nac_pay.app.services import _pipeline
    from nac_pay.schedule import AppliedEventKind

    client, uid = _bootstrap_user_with_june(monkeypatch, "cancel-event@x.test")
    _upload_cancellation_feed(client)
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)

    cancel_events = [
        e for e in pr.applied_events
        if e.kind is AppliedEventKind.COMPANY_CANCELLATION
    ]
    assert len(cancel_events) == 1
    assert cancel_events[0].date.isoformat() == "2026-06-02"
    assert cancel_events[0].trip_id == "722/750"


def test_other_days_unaffected_by_cancellation(monkeypatch):
    """Only June 2 flips: another scheduled trip day still renders as an
    active FLT assignment."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "cancel-scope@x.test")
    _upload_cancellation_feed(client)

    day = client.get("/day/2026-06-06")
    assert day.status_code == 200
    assert "Company cancelled this trip" not in day.text
