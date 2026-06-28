"""merge_feed_bytes — preserve frozen (completed) legs the fetch dropped."""

from __future__ import annotations

from datetime import datetime, timezone

from nac_pay.parsers import merge_feed_bytes, parse_ical_feed

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def _ev(uid: str, start: str, end: str, summary: str) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTART:{start}\r\n"
        f"DTEND:{end}\r\n"
        f"SUMMARY:{summary}\r\n"
        "END:VEVENT\r\n"
    )


def _cal(*events: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        + "".join(events)
        + "END:VCALENDAR\r\n"
    ).encode()


# Frozen = ended (well) before NOW; future = ends after NOW.
_FROZEN_720 = _ev("720", "20260627T140000Z", "20260627T160000Z", "FLT - NC720 ANC-OME N1")
_FROZEN_721 = _ev("721", "20260627T170000Z", "20260627T190000Z", "FLT - NC721 OME-ANC N1")
_FUTURE_999 = _ev("999", "20260630T140000Z", "20260630T160000Z", "FLT - NC999 ANC-FAI N1")


def _uids(blob: bytes) -> set[str]:
    import re
    return {m.strip() for m in re.findall(r"UID:(.+)", blob.decode())}


def test_no_existing_returns_incoming():
    inc = _cal(_FROZEN_721)
    assert merge_feed_bytes(None, inc, NOW) == inc


def test_frozen_dropped_leg_is_preserved():
    existing = _cal(_FROZEN_720, _FROZEN_721)
    incoming = _cal(_FROZEN_721)  # 720 aged out of the rolling window
    merged = merge_feed_bytes(existing, incoming, NOW)
    assert _uids(merged) == {"720", "721"}
    # The preserved leg is intact and the whole thing still parses.
    feed = parse_ical_feed(merged)
    flown = {leg.flight_no_short for leg in feed.flight_legs}
    assert "720" in flown and "721" in flown


def test_present_leg_not_duplicated():
    existing = _cal(_FROZEN_720)
    incoming = _cal(_FROZEN_720, _FROZEN_721)
    merged = merge_feed_bytes(existing, incoming, NOW)
    assert merged.decode().count("BEGIN:VEVENT") == 2
    assert _uids(merged) == {"720", "721"}


def test_future_dropped_leg_is_not_resurrected():
    """A not-yet-frozen event the fetch dropped is a genuine cancellation —
    it must NOT be preserved."""
    existing = _cal(_FROZEN_720, _FUTURE_999)
    incoming = _cal(_FROZEN_721)
    merged = merge_feed_bytes(existing, incoming, NOW)
    assert "999" not in _uids(merged)      # cancellation propagates
    assert _uids(merged) == {"720", "721"}  # frozen 720 still preserved


def test_empty_incoming_keeps_existing():
    existing = _cal(_FROZEN_720)
    incoming = _cal()  # no events (malformed/empty fetch)
    assert merge_feed_bytes(existing, incoming, NOW) == existing


def test_nothing_to_preserve_returns_incoming_unchanged():
    existing = _cal(_FROZEN_721)
    incoming = _cal(_FROZEN_721)
    assert merge_feed_bytes(existing, incoming, NOW) == incoming
