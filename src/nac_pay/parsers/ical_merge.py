"""Merge a freshly fetched iCal feed with the stored copy so completed legs
that have aged out of BlueOne's rolling window are preserved.

BlueOne serves only a short feed window (~24h forward); once a leg completes
it drops from the feed. The hourly updater (and a manual re-upload) would
otherwise overwrite the stored ``feed.ics`` and erase those flown legs —
silent loss of actuals (e.g. June 27's NC720 outbound).

``merge_feed_bytes`` keeps any stored event that is already **frozen** (ended
plus a 15-minute release pad before ``now``) and is **absent** from the
incoming feed. Upcoming / in-progress events are governed entirely by the
incoming feed, so a genuine cancellation or reassignment of a future trip
still propagates — only completed history is protected.

Events are matched by UID (BlueOne assigns a stable, unique integer per leg).
Raw VEVENT text is preserved verbatim; the VCALENDAR wrapper comes from the
incoming feed. Assumes a flat VEVENT list (no inter-event components), which
is the BlueOne feed shape.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# A leg is immutable once it has ended plus the contractual trip-end pad
# (mirrors the duty-window TRIP_END_PAD); kept local so this module has no
# engine dependency.
_FREEZE_PAD = timedelta(minutes=15)

_UID_RE = re.compile(r"^UID:(.+)$", re.MULTILINE)
_DTSTART_RE = re.compile(r"^DTSTART[^:]*:([0-9T]+Z?)", re.MULTILINE)
_DTEND_RE = re.compile(r"^DTEND[^:]*:([0-9T]+Z?)", re.MULTILINE)
_VEVENT_RE = re.compile(r"BEGIN:VEVENT.*?END:VEVENT", re.DOTALL)


class _Event:
    __slots__ = ("uid", "dtstart", "dtend", "raw")

    def __init__(self, uid, dtstart, dtend, raw):
        self.uid = uid
        self.dtstart = dtstart
        self.dtend = dtend
        self.raw = raw


def _unfold(text: str) -> str:
    """Undo RFC 5545 line folding (CRLF/LF + space/tab continues a line) so
    UID/DTSTART/DTEND can be read even if a producer folded them."""
    for fold in ("\r\n ", "\r\n\t", "\n ", "\n\t"):
        text = text.replace(fold, "")
    return text


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _events(text: str) -> list[_Event]:
    out: list[_Event] = []
    for m in _VEVENT_RE.finditer(text):
        raw = m.group(0)
        unf = _unfold(raw)
        uid_m = _UID_RE.search(unf)
        ds = _DTSTART_RE.search(unf)
        de = _DTEND_RE.search(unf)
        out.append(
            _Event(
                uid=uid_m.group(1).strip() if uid_m else None,
                dtstart=_parse_dt(ds.group(1) if ds else None),
                dtend=_parse_dt(de.group(1) if de else None),
                raw=raw,
            )
        )
    return out


def merge_feed_bytes(
    existing: bytes | None, incoming: bytes, now: datetime,
) -> bytes:
    """Merge the stored feed with a fresh fetch, preserving frozen (completed)
    events the fetch dropped. Returns the merged ``.ics`` bytes.

    - No existing feed → ``incoming`` unchanged.
    - Incoming with no parseable events → keep ``existing`` (never wipe history
      on a malformed fetch).
    - Nothing to preserve → ``incoming`` unchanged.
    """
    if not existing:
        return incoming
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    inc_text = incoming.decode("utf-8", errors="replace")
    inc_events = _events(inc_text)
    if not inc_events:
        return existing
    old_events = _events(existing.decode("utf-8", errors="replace"))

    inc_uids = {e.uid for e in inc_events if e.uid}

    def is_frozen(e: _Event) -> bool:
        # Preserve completed legs; on an unparseable DTEND, err toward keeping.
        return e.dtend is None or (e.dtend + _FREEZE_PAD < now)

    preserved = [
        e for e in old_events
        if e.uid and e.uid not in inc_uids and is_frozen(e)
    ]
    if not preserved:
        return incoming

    # Reassemble under the incoming feed's VCALENDAR wrapper.
    first = inc_text.find("BEGIN:VEVENT")
    last_end = inc_text.rfind("END:VEVENT")
    nl_after = inc_text.find("\n", last_end)
    header = inc_text[:first]
    footer = inc_text[nl_after + 1:] if nl_after != -1 else ""
    newline = "\r\n" if "\r\n" in inc_text else "\n"

    merged = inc_events + preserved
    merged.sort(key=lambda e: (e.dtstart is None, e.dtstart or now))
    body = "".join(e.raw.rstrip("\r\n") + newline for e in merged)
    return (header + body + footer).encode("utf-8")
