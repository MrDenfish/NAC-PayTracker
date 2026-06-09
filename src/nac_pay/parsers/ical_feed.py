"""Parse a NAC iCal schedule feed into typed event records.

The feed (produced by the BlueOne Roster system) publishes one VEVENT per
duty unit. Per spec §10 the event type is distinguished by the SUMMARY
prefix:

- ``FLT - <flight#> <ORG>-<DST> <tail>`` — a flight leg, DTSTART/DTEND
  in UTC giving scheduled out/in. Crew lines (``CPT``, ``FO``) live in
  DESCRIPTION. Flight numbers in this feed carry a ``NC`` carrier prefix
  (``NC766``) where the Master Schedule / Packet write just ``766``;
  ``FlightLegEvent.flight_no_short`` strips it for matching.
- ``R/S - Reserve or Standby at <BASE>`` — a reserve / standby day.
  DESCRIPTION carries the line designator (e.g. ``1021S at ANC``) which
  ties back to the Master Schedule reserve line (Master Schedule uses
  ``1021``, iCal uses ``1021S``).
- ``LEA - <label>`` — leave / non-duty (``LEA - OFF`` for a day off).
  Spec notes other LEA subtypes (PTO, etc.) likely share this prefix.

Anything else lands in ``unknown`` so the caller can flag it for review.

Spec §10 lists training (CLASS/SIM), deadhead (DH), layover, and the
R-1/R-2/R-4 reserve distinction as deferred — none of those appear in
our current sample either, so they remain genuinely unknown formats
until we see samples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from icalendar import Calendar

# ── Public event types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FlightLegEvent:
    uid: str
    dt_start_utc: datetime           # tz-aware
    dt_end_utc: datetime
    flight_no_raw: str               # "NC768"
    flight_no_short: str             # "768" (carrier prefix stripped for matching)
    origin: str                      # "ANC"
    destination: str                 # "BRW"
    tail: str                        # "N409YK"
    customer: str                    # "Northern Air Cargo"
    captain: str                     # "Timo Armas SAARINEN"
    first_officer: str               # "Dennis FISHER"

    @property
    def block_hours(self) -> Decimal:
        """Scheduled block time = DTEND - DTSTART, expressed in hours."""
        delta = self.dt_end_utc - self.dt_start_utc
        return Decimal(str(delta.total_seconds() / 3600))


@dataclass(frozen=True)
class ReserveEvent:
    uid: str
    dt_start_utc: datetime           # RAP window start
    dt_end_utc: datetime             # RAP window end
    base: str                        # "ANC"
    line_designator: str             # "1021S" — has the trailing 'S' suffix
    line_designator_short: str       # "1021" — to match Master Schedule


@dataclass(frozen=True)
class OffEvent:
    uid: str
    dt_start_utc: datetime
    dt_end_utc: datetime
    label: str                       # "OFF" (could be PTO, etc., per spec)


@dataclass(frozen=True)
class UnknownEvent:
    uid: str
    dt_start_utc: datetime
    dt_end_utc: datetime
    summary: str
    description: str


@dataclass(frozen=True)
class ParsedFeed:
    flight_legs: tuple[FlightLegEvent, ...]
    reserves: tuple[ReserveEvent, ...]
    off_days: tuple[OffEvent, ...]
    unknown: tuple[UnknownEvent, ...]

    @property
    def total_events(self) -> int:
        return (
            len(self.flight_legs)
            + len(self.reserves)
            + len(self.off_days)
            + len(self.unknown)
        )


# ── Public entry point ──────────────────────────────────────────────────


def parse_ical_feed(source: str | Path | bytes) -> ParsedFeed:
    """Parse an iCal feed from a file path, raw text, or bytes."""
    if isinstance(source, (str, Path)) and Path(str(source)).is_file():
        raw: bytes = Path(str(source)).read_bytes()
    elif isinstance(source, bytes):
        raw = source
    else:
        raw = str(source).encode("utf-8")

    cal = Calendar.from_ical(raw)
    flights: list[FlightLegEvent] = []
    reserves: list[ReserveEvent] = []
    offs: list[OffEvent] = []
    unknown: list[UnknownEvent] = []

    for component in cal.walk("VEVENT"):
        ev = _parse_vevent(component)
        if isinstance(ev, FlightLegEvent):
            flights.append(ev)
        elif isinstance(ev, ReserveEvent):
            reserves.append(ev)
        elif isinstance(ev, OffEvent):
            offs.append(ev)
        else:
            unknown.append(ev)

    return ParsedFeed(
        flight_legs=tuple(flights),
        reserves=tuple(reserves),
        off_days=tuple(offs),
        unknown=tuple(unknown),
    )


# ── Per-VEVENT dispatch ────────────────────────────────────────────────
_FLT_SUMMARY_RE = re.compile(
    r"^FLT\s*-\s*(?P<flight>\S+)\s+(?P<org>[A-Z]{3})-(?P<dst>[A-Z]{3})\s+(?P<tail>\S+)\s*$"
)
_RS_SUMMARY_RE = re.compile(
    r"^R/S\s*-\s*Reserve or Standby at\s+(?P<base>[A-Z]{3})\s*$"
)
_LEA_SUMMARY_RE = re.compile(r"^LEA\s*-\s*(?P<label>.+)$")

_DESC_CUSTOMER_RE = re.compile(r"Customer:\s*(.+)")
_DESC_CPT_RE = re.compile(r"\bCPT\s+(.+)")
_DESC_FO_RE = re.compile(r"\bFO\s+(.+)")
_RS_DESC_RE = re.compile(r"\s*(\S+)\s+at\s+([A-Z]{3})\s*$")

_NC_PREFIX_RE = re.compile(r"^NC", re.IGNORECASE)


def _parse_vevent(
    component,
) -> FlightLegEvent | ReserveEvent | OffEvent | UnknownEvent:
    uid = str(component.get("UID", ""))
    summary = str(component.get("SUMMARY", "")).strip()
    description = _decode_description(component.get("DESCRIPTION", ""))
    dt_start = _to_utc(component.get("DTSTART").dt)
    dt_end = _to_utc(component.get("DTEND").dt)

    if (m := _FLT_SUMMARY_RE.match(summary)):
        return _build_flight_leg(uid, dt_start, dt_end, m, description)
    if (m := _RS_SUMMARY_RE.match(summary)):
        return _build_reserve(uid, dt_start, dt_end, m.group("base"), description)
    if (m := _LEA_SUMMARY_RE.match(summary)):
        return OffEvent(
            uid=uid,
            dt_start_utc=dt_start,
            dt_end_utc=dt_end,
            label=m.group("label").strip(),
        )
    return UnknownEvent(
        uid=uid,
        dt_start_utc=dt_start,
        dt_end_utc=dt_end,
        summary=summary,
        description=description,
    )


def _build_flight_leg(
    uid: str,
    dt_start: datetime,
    dt_end: datetime,
    m: re.Match[str],
    description: str,
) -> FlightLegEvent:
    raw_flight = m.group("flight")
    short = _NC_PREFIX_RE.sub("", raw_flight)
    customer = _first_match(_DESC_CUSTOMER_RE, description)
    cpt = _first_match(_DESC_CPT_RE, description)
    fo = _first_match(_DESC_FO_RE, description)
    return FlightLegEvent(
        uid=uid,
        dt_start_utc=dt_start,
        dt_end_utc=dt_end,
        flight_no_raw=raw_flight,
        flight_no_short=short,
        origin=m.group("org"),
        destination=m.group("dst"),
        tail=m.group("tail"),
        customer=customer,
        captain=cpt,
        first_officer=fo,
    )


def _build_reserve(
    uid: str,
    dt_start: datetime,
    dt_end: datetime,
    base: str,
    description: str,
) -> ReserveEvent:
    m = _RS_DESC_RE.search(description)
    designator = m.group(1) if m else ""
    desc_base = m.group(2) if m else base
    # Master Schedule uses the unsuffixed line number ("1021"); iCal adds
    # a trailing 'S' (or 'M'/'PM' etc. — TBD when sampled).
    short = re.sub(r"[A-Z]+$", "", designator) if designator else ""
    return ReserveEvent(
        uid=uid,
        dt_start_utc=dt_start,
        dt_end_utc=dt_end,
        base=desc_base or base,
        line_designator=designator,
        line_designator_short=short,
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _to_utc(value) -> datetime:
    """Normalize a datetime (or date) to a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # date (all-day) — promote to midnight UTC
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _decode_description(value) -> str:
    """icalendar returns DESCRIPTION as a vText; ensure plain str with
    literal ``\\n`` escapes converted to real newlines."""
    if value is None:
        return ""
    s = str(value)
    # The feed encodes line breaks as literal "\n" sequences inside the value.
    # Some senders also escape them as "\\n" — handle both.
    return s.replace("\\n", "\n").replace("\\N", "\n")


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""
