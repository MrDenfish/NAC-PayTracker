"""Parse a NAC semi-monthly pay stub PDF.

The stub format from the bundled samples:

  Pay Statement
  Period Start Date 05/01/2026
  Period End Date 05/15/2026
  Pay Date 05/22/2026
  Net Pay $2,061.44

  Earnings
  Pay Type        Hours       Pay Rate   Current        YTD
  Regular Pay     32.500000   $124.5900  $4,049.18      $44,048.80
  Open Time       3.820000    $186.8850  $713.90        $29,142.84
  Regular Pay    -32.500000   $124.5900  ($4,049.18)    $50,002.95   ← negative
  Group Term Life                        $15.84         $158.40       ← benefit, no hours/rate
  Sick            0.000000    $0.0000    $0.00          $1,427.79
  ...
  Total Hours Worked 47.790000 Total Hours 51.610000

Notes:
- The MPG advance pattern: stub 1 of the month shows ``+32.50 Regular Pay``
  ($4,049.18 = 32.50 × $124.59); stub 2 reverses it (``-32.50``). Netting
  them gets the monthly actual.
- Negative dollar amounts print as ``($X.XX)`` (accounting style).
- ``Group Term Life`` is a benefit (employer-paid life insurance), not PCH.
  It carries no Hours / Rate columns. We expose it so the comparison can
  show "company shows this; tracker doesn't track it (out of scope)".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as date_t
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber


# ── Public types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PayStubLine:
    pay_type: str
    hours: Decimal | None        # None for benefit-only rows like Group Term Life
    rate: Decimal | None
    current_amount: Decimal      # signed
    ytd_amount: Decimal | None


@dataclass(frozen=True)
class PayStub:
    period_start: date_t
    period_end: date_t
    pay_date: date_t
    net_pay: Decimal
    earnings: tuple[PayStubLine, ...]
    total_hours_worked: Decimal | None
    total_hours: Decimal | None
    source_path: str             # for debugging

    @property
    def label(self) -> str:
        return f"{self.period_start.isoformat()} → {self.period_end.isoformat()}"


# ── Public entry point ────────────────────────────────────────────────


def parse_pay_stub(pdf_path: str | Path) -> PayStub:
    path = Path(pdf_path)
    with pdfplumber.open(str(path)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return _parse_text(text, source_path=str(path))


# ── Regex anchors ─────────────────────────────────────────────────────
_DATE_PAT = r"(\d{2}/\d{2}/\d{4})"
_PERIOD_START_RE = re.compile(rf"Period Start Date\s+{_DATE_PAT}")
_PERIOD_END_RE = re.compile(rf"Period End Date\s+{_DATE_PAT}")
_PAY_DATE_RE = re.compile(rf"Pay Date\s+{_DATE_PAT}")
_NET_PAY_RE = re.compile(r"Net Pay\s+(\$?[\d,]+\.\d{2})")
_TOTAL_HOURS_WORKED_RE = re.compile(r"Total Hours Worked\s+([\d.]+)")
_TOTAL_HOURS_RE = re.compile(r"Total Hours\s+([\d.]+)")

# Match an earnings line where Pay Type is alphabetic-ish followed by
# hours / rate / current / ytd in that order. Negative current shows in
# accounting parens.
_MONEY = r"(?:\$?[\d,]+\.\d{2}|\(\$?[\d,]+\.\d{2}\))"
_NUMERIC = r"-?[\d,]+\.\d+"
_EARNING_RE = re.compile(
    r"^(?P<pay_type>[A-Za-z][A-Za-z ]+?)\s+"
    rf"(?P<hours>{_NUMERIC})\s+"
    r"\$(?P<rate>[\d,]+\.\d+)\s+"
    rf"(?P<current>{_MONEY})"
    rf"(?:\s+(?P<ytd>{_MONEY}))?\s*$"   # YTD optional — multi-row categories
                                         # print YTD only on the last row.
)
# Benefit-only lines (Group Term Life): no hours/rate, just two money columns.
_BENEFIT_RE = re.compile(
    rf"^(?P<pay_type>[A-Za-z][A-Za-z ]+?)\s+(?P<current>{_MONEY})\s+(?P<ytd>{_MONEY})\s*$"
)


def _parse_text(text: str, source_path: str) -> PayStub:
    period_start = _parse_date(_PERIOD_START_RE.search(text))
    period_end = _parse_date(_PERIOD_END_RE.search(text))
    pay_date = _parse_date(_PAY_DATE_RE.search(text))
    if not (period_start and period_end and pay_date):
        raise ValueError(f"Could not parse stub dates from {source_path}")
    net_pay = _parse_money_match(_NET_PAY_RE.search(text)) or Decimal("0")

    earnings = _parse_earnings_lines(text)
    twh = _decimal_match(_TOTAL_HOURS_WORKED_RE, text)
    th = _decimal_match(_TOTAL_HOURS_RE, text, occurrence=2)  # 2nd "Total Hours" (the standalone)

    return PayStub(
        period_start=period_start,
        period_end=period_end,
        pay_date=pay_date,
        net_pay=net_pay,
        earnings=earnings,
        total_hours_worked=twh,
        total_hours=th,
        source_path=source_path,
    )


def _parse_earnings_lines(text: str) -> tuple[PayStubLine, ...]:
    in_section = False
    out: list[PayStubLine] = []
    seen: set[tuple[str, str]] = set()    # (pay_type, current_amount) for dedup
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Earnings"):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("Total Hours") or line.startswith("Deductions"):
            break
        if not line or line.startswith("Pay Type"):
            continue

        match = _EARNING_RE.match(line)
        if match:
            pay_type = match.group("pay_type").strip()
            hours = _to_decimal(match.group("hours"))
            rate = _to_decimal(match.group("rate"))
            current = _money_to_signed_decimal(match.group("current"))
            ytd_raw = match.group("ytd")
            ytd = _money_to_signed_decimal(ytd_raw) if ytd_raw else None
            key = (pay_type, str(current))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                PayStubLine(
                    pay_type=pay_type,
                    hours=hours,
                    rate=rate,
                    current_amount=current,
                    ytd_amount=ytd,
                )
            )
            continue

        match = _BENEFIT_RE.match(line)
        if match:
            pay_type = match.group("pay_type").strip()
            current = _money_to_signed_decimal(match.group("current"))
            ytd = _money_to_signed_decimal(match.group("ytd"))
            key = (pay_type, str(current))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                PayStubLine(
                    pay_type=pay_type,
                    hours=None,
                    rate=None,
                    current_amount=current,
                    ytd_amount=ytd,
                )
            )
            continue
    return tuple(out)


# ── Helpers ─────────────────────────────────────────────────────────────


def _parse_date(match: re.Match[str] | None) -> date_t | None:
    if match is None:
        return None
    mm, dd, yyyy = match.group(1).split("/")
    return date_t(int(yyyy), int(mm), int(dd))


def _parse_money_match(match: re.Match[str] | None) -> Decimal | None:
    if match is None:
        return None
    return _money_to_signed_decimal(match.group(1))


def _money_to_signed_decimal(s: str) -> Decimal:
    """Convert money strings like ``$1,234.56`` or ``($1,234.56)`` to Decimal.
    Accounting parens → negative."""
    s = s.strip()
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return Decimal("0")
    return -value if negative else value


def _to_decimal(s: str) -> Decimal:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _decimal_match(
    pattern: re.Pattern[str], text: str, occurrence: int = 1
) -> Decimal | None:
    """Return Nth match group as Decimal."""
    matches = list(pattern.finditer(text))
    if len(matches) < occurrence:
        return matches[-1] and _to_decimal(matches[-1].group(1)) if matches else None
    return _to_decimal(matches[occurrence - 1].group(1))
