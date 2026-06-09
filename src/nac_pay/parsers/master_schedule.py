"""Parse a Master Schedule (Final Awards) PDF into per-pilot grids.

Spec §10 describes the page:

- One page per fleet/position/month grid.
- Columns: 31 day cells + a WD summary column + grey next-month spillover.
- Left columns: 3-letter pilot code + last name (one band per pilot).
- Each day cell contains four logical rows: assignment ID, duty type, a
  blank placeholder we skip, and the PCH value. In practice pdfplumber's
  table extractor concatenates the three non-blank rows with newlines
  inside a single cell, OR (less commonly) splits the band into two or
  three table rows. Our parser handles both layouts.

The parser returns ``dict[pilot_code, PilotMonthSchedule]`` so downstream
selects whichever pilot is needed.

Out of scope here:
- Constructing the schedule-layer ``Month``/``Trip``/``Day`` graph from
  the grid. That's a separate step that needs to group multi-day trips
  (assignment IDs that span consecutive days), reconcile with the packet,
  and apply pilot edits. The grid this parser returns is the raw input
  to that step.
- The printed ``65`` floor token next to a sub-65 line total. The spec
  says ``floor = max(line_value, MPG)`` — we just compute it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as date_t
from decimal import Decimal, InvalidOperation

import pdfplumber

from nac_pay.engine.constants import MPG

# Duty-type tokens that appear in the schedule's middle row of each day cell.
# Used by ``_parse_cell`` to separate the duty token from assignment markers
# in multi-token cells (e.g. "RGS\nCLASS\nGSI\n4.00" → duty=CLASS).
KNOWN_DUTY_TOKENS: frozenset[str] = frozenset(
    {
        "FLT", "RSV", "PTO", "FMLA", "CLASS", "SIM", "DH", "VX", "OFF",
        "RGS", "MOVING", "TAXI", "JURY", "SICK", "BERV", "FAR", "MIL",
    }
)


_MONTH_BY_NAME: dict[str, int] = {
    "JANUARY": 1, "JAN": 1,
    "FEBRUARY": 2, "FEB": 2,
    "MARCH": 3, "MAR": 3,
    "APRIL": 4, "APR": 4,
    "MAY": 5,
    "JUNE": 6, "JUN": 6,
    "JULY": 7, "JUL": 7,
    "AUGUST": 8, "AUG": 8,
    "SEPTEMBER": 9, "SEP": 9, "SEPT": 9,
    "OCTOBER": 10, "OCT": 10,
    "NOVEMBER": 11, "NOV": 11,
    "DECEMBER": 12, "DEC": 12,
}


@dataclass(frozen=True)
class DayCell:
    """One day's published assignment for a pilot, as printed on the Final Award."""

    date: date_t
    assignment_id: str | None
    duty_type: str | None
    pch_value: Decimal | None

    @property
    def is_off(self) -> bool:
        return (
            self.assignment_id is None
            and self.duty_type is None
            and self.pch_value is None
        ) or self.duty_type == "OFF"


@dataclass(frozen=True)
class PilotMonthSchedule:
    pilot_code: str
    last_name: str
    year: int
    month: int
    line_value: Decimal
    monthly_floor: Decimal      # max(line_value, MPG)
    days: tuple[DayCell, ...]   # one per scheduled day (1..N for the month)

    @property
    def assigned_days(self) -> tuple[DayCell, ...]:
        return tuple(d for d in self.days if not d.is_off)


# ── Public entry point ──────────────────────────────────────────────────
def parse_master_schedule(pdf_path: str) -> dict[str, PilotMonthSchedule]:
    """Parse a Final Awards PDF into one PilotMonthSchedule per pilot."""
    with pdfplumber.open(pdf_path) as pdf:
        table = pdf.pages[0].extract_tables()[0]

    year, month = _parse_year_month(table)
    day_col_indexes = _identify_day_columns(table, month, year)
    wd_col = _identify_wd_column(table)
    bands = _identify_bands(table)

    out: dict[str, PilotMonthSchedule] = {}
    for band in bands:
        sched = _build_pilot_schedule(
            table=table,
            band=band,
            day_cols=day_col_indexes,
            wd_col=wd_col,
            year=year,
            month=month,
        )
        if sched is not None:
            out[sched.pilot_code] = sched
    return out


# ── Header parsing ──────────────────────────────────────────────────────
def _parse_year_month(table: list[list[str | None]]) -> tuple[int, int]:
    """Extract month from page title ('MAY - First Officer Lines') and
    year from the revision date stamp ('4/19/2026 12:19')."""
    title_cells = [c for c in (table[0] or []) if c]
    month = 0
    for cell in title_cells:
        head = cell.strip().split()[0].upper().rstrip("-,.")
        if head in _MONTH_BY_NAME:
            month = _MONTH_BY_NAME[head]
            break

    year = 0
    for row in table[:3]:
        for cell in row or []:
            if not cell:
                continue
            m = re.search(r"\b(\d{4})\b", cell)
            if m:
                year = int(m.group(1))
                break
        if year:
            break
    if not (year and month):
        raise ValueError("Could not parse year/month from Final Awards header")
    return year, month


_DAY_OF_WEEK_TOKENS = frozenset({"M", "T", "W", "TH", "F", "S", "SU"})


def _identify_day_columns(
    table: list[list[str | None]],
    month: int,
    year: int,
) -> dict[int, date_t]:
    """Map table column index → calendar date for cells in *this* month.

    Counts day-of-week tokens in the header row between col 2 and the WD
    column. Each token marks one day; we assign dates sequentially from
    day 1.

    We *don't* trust the day-number row (row 2) because real PDFs can
    misprint it — June 2026 labels days 29-30 as "30, 31". The DOW
    sequence is reliable.
    """
    header = table[1] or []
    wd_col = _identify_wd_column(table)
    out: dict[int, date_t] = {}
    day = 1
    for col_idx in range(2, wd_col):
        token = (header[col_idx] or "").strip().upper()
        if token not in _DAY_OF_WEEK_TOKENS:
            continue
        try:
            out[col_idx] = date_t(year, month, day)
        except ValueError:
            # day exceeded days-in-month; stop assigning
            break
        day += 1
    return out


def _identify_wd_column(table: list[list[str | None]]) -> int:
    """The header row prints 'WD' over the workdays summary column."""
    header = table[1] or []
    for i, cell in enumerate(header):
        if cell and cell.strip().upper() == "WD":
            return i
    raise ValueError("Could not locate WD column in Final Awards header")


# ── Pilot band detection ────────────────────────────────────────────────
@dataclass(frozen=True)
class _Band:
    start_row: int       # inclusive
    end_row: int         # exclusive
    code_row: int        # row whose col 0 holds the 3-letter code


def _identify_bands(table: list[list[str | None]]) -> list[_Band]:
    """Each band starts at a row whose col 0 is non-empty (the 3-letter
    pilot code) and extends to (but not including) the next such row."""
    starts: list[int] = []
    for ri in range(3, len(table)):
        cell = (table[ri][0] or "").strip()
        if cell:
            starts.append(ri)
    bands: list[_Band] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(table)
        bands.append(_Band(start_row=s, end_row=e, code_row=s))
    return bands


def _find_last_name(table: list[list[str | None]], band: _Band) -> str:
    """Last name lives in col 1 somewhere inside the band — usually the
    same row as the code, but for multi-row bands it's a later row. Some
    cells stack a parenthetical annotation above the name on separate
    lines (e.g. ``"(FCF)\\nBAGIAN"``), so we tokenize by newline first."""
    for ri in range(band.start_row, band.end_row):
        cell = table[ri][1] or ""
        for token in cell.split("\n"):
            tok = token.strip()
            if not tok:
                continue
            if tok.startswith("(") and tok.endswith(")"):
                continue   # annotation like (FCF), (IOE)
            if tok.replace("-", "").replace("'", "").isalpha():
                return tok
    return ""


# ── Per-band day extraction ────────────────────────────────────────────
def _build_pilot_schedule(
    table: list[list[str | None]],
    band: _Band,
    day_cols: dict[int, date_t],
    wd_col: int,
    year: int,
    month: int,
) -> PilotMonthSchedule | None:
    code = (table[band.code_row][0] or "").strip()
    if not code:
        return None
    last_name = _find_last_name(table, band)

    days: list[DayCell] = []
    for col_idx, dt in day_cols.items():
        merged = _merge_band_cell(table, band, col_idx)
        cell = _parse_cell(merged)
        days.append(
            DayCell(
                date=dt,
                assignment_id=cell.assignment_id,
                duty_type=cell.duty_type,
                pch_value=cell.pch_value,
            )
        )

    line_value = _extract_line_value(table, band, wd_col)
    floor = max(line_value, MPG)
    return PilotMonthSchedule(
        pilot_code=code,
        last_name=last_name,
        year=year,
        month=month,
        line_value=line_value,
        monthly_floor=floor,
        days=tuple(days),
    )


def _merge_band_cell(
    table: list[list[str | None]],
    band: _Band,
    col_idx: int,
) -> str:
    """Concatenate non-empty content from every row in the band for a column."""
    parts: list[str] = []
    for ri in range(band.start_row, band.end_row):
        row = table[ri]
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v:
            parts.append(v)
    return "\n".join(parts)


def _extract_line_value(
    table: list[list[str | None]],
    band: _Band,
    wd_col: int,
) -> Decimal:
    """The WD column carries the monthly PCH total on one of the band's rows."""
    for ri in range(band.start_row, band.end_row):
        row = table[ri]
        if wd_col >= len(row):
            continue
        v = row[wd_col]
        if not v:
            continue
        try:
            return Decimal(v.strip())
        except (InvalidOperation, AttributeError):
            continue
    return Decimal("0")


# ── Cell parsing ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _CellTokens:
    assignment_id: str | None
    duty_type: str | None
    pch_value: Decimal | None


_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _parse_cell(text: str) -> _CellTokens:
    tokens = [t.strip() for t in text.split("\n") if t.strip()]
    if not tokens:
        return _CellTokens(None, None, None)

    pch: Decimal | None = None
    if _NUMERIC_RE.match(tokens[-1]):
        try:
            pch = Decimal(tokens[-1])
            tokens = tokens[:-1]
        except InvalidOperation:
            pch = None

    duty: str | None = None
    leftover: list[str] = []
    for t in tokens:
        if duty is None and t.upper() in KNOWN_DUTY_TOKENS:
            duty = t.upper()
        else:
            leftover.append(t)

    if duty is None and leftover:
        # No known duty token; treat single-token cells as duty-only labels.
        if len(leftover) == 1:
            duty = leftover[0].upper()
            leftover = []

    assignment = " / ".join(leftover) if leftover else None
    return _CellTokens(assignment, duty, pch)
