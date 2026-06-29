"""Parse a Trip Pairing Packet PDF into per-trip records.

Spec §10 + §9 describes what the packet provides:

- One page per trip pairing. Header: ``Trip Paring No. = 766/766/767//////``
  (yes, "Paring" — that's how it's printed). Trailing slashes are filler.
- Function rows (FLT / LAYOVER / RESERVE / etc.) with raw times for each leg.
- Summary row with show/release times, Total DH, total duty time, DPG,
  Sch. Block, TAFB.
- PCH section with the four §3.E component PCHs, the ``TRIP PCH VALUE``,
  and a ``DH+Trip PCH`` (= TRIP PCH VALUE + deadhead, for cross-check).

The packet also contains a header schedule page, weekly/monthly totals,
and a reserve-standby (R1) pairing page that uses a different layout.
We skip those for now and parse only the standard trip pages — that's
what §9's monthly validation check needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pdfplumber


# ── Public dataclasses ──────────────────────────────────────────────────
@dataclass(frozen=True)
class TripPairing:
    trip_id: str                  # canonical, e.g. "766/766/767" (no trailing /)
    raw_trip_id: str              # as printed in the packet
    start_day_of_week: str        # "Sunday" / "Saturday"
    end_day_of_week: str

    sch_block_hours: Decimal      # from Sch. Block
    duty_hours: Decimal           # from Total Flt Duty (the duty-time column)
    tafb_hours: Decimal           # from TAFB
    total_dh_hours: Decimal       # from Total DH
    dpg_pch: Decimal              # from DPG column (= workdays × 3.82)
    workdays: int                 # derived from dpg_pch / 3.82

    flight_op_pch: Decimal        # printed §3.E component
    duty_rig_pch: Decimal
    trip_rig_pch: Decimal
    cumulative_dpg_pch: Decimal
    deadhead_pch: Decimal

    trip_pch_value: Decimal       # printed TRIP PCH VALUE
    dh_plus_trip_pch: Decimal     # printed DH+Trip PCH (cross-check)

    page_index: int               # zero-based; for debugging back to PDF

    # Scheduled duty window in LOCAL time, from the summary row's "L Day Show"
    # / "L Day Duty Off" (report → release; their span == duty_hours). Lets the
    # day view reconstruct a duty window from the packet when iCal legs are
    # missing (aged out of BlueOne's rolling feed). "HH:MM", "" if not parsed.
    sched_duty_on: str = ""
    sched_duty_off: str = ""


# ── Public entry point ──────────────────────────────────────────────────
def parse_trip_pairing_packet(pdf_path: str) -> dict[str, TripPairing]:
    """Return per-trip data keyed by canonical trip_id.

    Non-trip pages (cover, summary, R1 reserve standby) are silently
    skipped — they're identified by the absence of a ``Trip Paring No.``
    header.
    """
    out: dict[str, TripPairing] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if "Trip Paring No" not in text:
                continue
            trip = _parse_trip_page(text, page_idx)
            if trip is not None:
                out[trip.trip_id] = trip
    return out


# ── Regex anchors ───────────────────────────────────────────────────────
_TRIP_ID_RE = re.compile(r"Trip Paring No\.\s*=\s*(\S+)")
_START_DAY_RE = re.compile(r"Z-Trip Start Day\s*=\s*(\w+)")
_END_DAY_RE = re.compile(r"Z-Trip End Day\s*=\s*(\w+)")
_FLT_OP_RE = re.compile(r"Flight Operation PCH\s+([\d.]+)")
_DUTY_RIG_RE = re.compile(r"Duty Rig PCH\s+([\d.]+)")
_TRIP_RIG_RE = re.compile(r"Trip Rig PCH\s+([\d.]+)")
_CUM_DPG_RE = re.compile(r"Cumulative DPG PCH\s+([\d.]+)")
_DH_PCH_RE = re.compile(r"DeadHead PCH\s+([\d.]+)")
_TRIP_VAL_RE = re.compile(r"TRIP PCH VALUE:\s+([\d.]+)")
_DH_TRIP_RE = re.compile(r"DH\+Trip PCH\s+([\d.]+)")
_SUMMARY_HDR_RE = re.compile(
    r"Z Show Z Duty Off LDAY Show L Day Duty Off Total DH Total Flt Duty DPG Sch\. Block TAFB"
)
_DPG_KW = Decimal("3.82")
_DPG_DIV = Decimal("3.82")


# ── Per-page parser ─────────────────────────────────────────────────────
def _parse_trip_page(text: str, page_idx: int) -> TripPairing | None:
    raw_id_match = _TRIP_ID_RE.search(text)
    if raw_id_match is None:
        return None
    raw_id = raw_id_match.group(1)
    trip_id = raw_id.rstrip("/")

    start_day = _match_or_blank(_START_DAY_RE, text)
    end_day = _match_or_blank(_END_DAY_RE, text)

    summary = _extract_summary_row(text)
    if summary is None:
        return None
    total_dh, duty_hrs, dpg_pch, block_hrs, tafb_hrs, sched_on, sched_off = summary

    flight_op = _decimal_match(_FLT_OP_RE, text)
    duty_rig = _decimal_match(_DUTY_RIG_RE, text)
    trip_rig = _decimal_match(_TRIP_RIG_RE, text)
    cum_dpg = _decimal_match(_CUM_DPG_RE, text)
    dh_pch = _decimal_match(_DH_PCH_RE, text)
    trip_val = _decimal_match(_TRIP_VAL_RE, text)
    dh_plus_trip = _decimal_match(_DH_TRIP_RE, text)

    # Workdays derived from DPG (no separate field).
    workdays = int((dpg_pch / _DPG_DIV).to_integral_value()) if dpg_pch > 0 else 0

    return TripPairing(
        trip_id=trip_id,
        raw_trip_id=raw_id,
        start_day_of_week=start_day,
        end_day_of_week=end_day,
        sch_block_hours=block_hrs,
        duty_hours=duty_hrs,
        tafb_hours=tafb_hrs,
        total_dh_hours=total_dh,
        dpg_pch=dpg_pch,
        workdays=workdays,
        flight_op_pch=flight_op,
        duty_rig_pch=duty_rig,
        trip_rig_pch=trip_rig,
        cumulative_dpg_pch=cum_dpg,
        deadhead_pch=dh_pch,
        trip_pch_value=trip_val,
        dh_plus_trip_pch=dh_plus_trip,
        page_index=page_idx,
        sched_duty_on=sched_on,
        sched_duty_off=sched_off,
    )


def _extract_summary_row(
    text: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, str, str] | None:
    """Find the summary header line and return
    (Total DH, Duty, DPG, Block, TAFB, sched_duty_on, sched_duty_off) from the
    line directly below it. Times are decimal hours; the duty window is the
    "HH:MM" LOCAL clock from "L Day Show" / "L Day Duty Off" ("" if absent).

    Data row shape (13 tokens):
        <Z Show d t> <Z Duty Off d t> <L Day Show d t> <L Day Duty Off d t>
        <Total DH> <Total Flt Duty> <DPG> <Sch. Block> <TAFB>
    The last 5 are the totals; the four leading date-time PAIRS are the
    Zulu/local show & duty-off clocks.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _SUMMARY_HDR_RE.search(line):
            if i + 1 >= len(lines):
                return None
            row = lines[i + 1].split()
            if len(row) < 5:
                return None
            total_dh = _hhmm_to_hours(row[-5])
            duty = _hhmm_to_hours(row[-4])
            try:
                dpg = Decimal(row[-3])
            except InvalidOperation:
                return None
            block = _hhmm_to_hours(row[-2])
            tafb = _hhmm_to_hours(row[-1])
            # L Day Show = row[-9] (date) row[-8] (time); L Day Duty Off =
            # row[-7] row[-6]. Only present when the full 4 pairs precede the
            # totals (≥ 13 tokens); degrade gracefully otherwise.
            sched_on = sched_off = ""
            if len(row) >= 13:
                sched_on = _norm_clock(row[-8])
                sched_off = _norm_clock(row[-6])
            return total_dh, duty, dpg, block, tafb, sched_on, sched_off
    return None


def _norm_clock(token: str) -> str:
    """Normalize a "H:MM" / "HH:MM" clock token to zero-padded "HH:MM" ("" on
    anything unparseable)."""
    parts = token.split(":")
    if len(parts) != 2:
        return ""
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return ""
    if not (0 <= h < 24 and 0 <= m < 60):
        return ""
    return f"{h:02d}:{m:02d}"


# ── Helpers ─────────────────────────────────────────────────────────────
def _hhmm_to_hours(s: str) -> Decimal:
    """Convert 'H:MM' / 'HH:MM' to Decimal hours. '0:00' → 0; '7:05' → 7 + 5/60."""
    if ":" not in s:
        try:
            return Decimal(s)
        except InvalidOperation:
            return Decimal("0")
    h, m = s.split(":", 1)
    return Decimal(h) + (Decimal(m) / Decimal("60"))


def _decimal_match(pattern: re.Pattern[str], text: str) -> Decimal:
    m = pattern.search(text)
    if m is None:
        return Decimal("0")
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return Decimal("0")


def _match_or_blank(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else ""
