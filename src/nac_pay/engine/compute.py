"""The two-stage pay engine (§6).

Stage 1: base monthly PCH = max(adjusted_floor, workdays*DPG, earned_sum).
Stage 2: dollars = sum(chunk raw_pch * rate * multiplier) + top-up at regular rate.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .constants import DPG, PCH_DP
from .floor import compute_adjusted_floor
from .models import (
    ChunkResult,
    EngineInput,
    EngineResult,
    WinningOption,
)

_PCH_QUANT = Decimal("1").scaleb(-PCH_DP)  # 0.01
_DOLLAR_QUANT = Decimal("0.01")


def _q_pch(x: Decimal) -> Decimal:
    return x.quantize(_PCH_QUANT, rounding=ROUND_HALF_UP)


def _q_dollar(x: Decimal) -> Decimal:
    return x.quantize(_DOLLAR_QUANT, rounding=ROUND_HALF_UP)


def compute_pay(inp: EngineInput) -> EngineResult:
    # ── Stage 1: base monthly PCH (raw) ──────────────────────────────────
    option1 = compute_adjusted_floor(inp.line_value, inp.floor_events, inp.chunks)

    workday_total = sum((c.workdays for c in inp.chunks), 0)
    option2 = Decimal(workday_total) * DPG

    option3 = sum(
        (c.raw_pch for c in inp.chunks if c.in_guarantee),
        Decimal("0"),
    )

    base_monthly_pch = max(option1, option2, option3)
    if base_monthly_pch == option1 and option1 >= option2 and option1 >= option3:
        winning = WinningOption.FLOOR
    elif base_monthly_pch == option3 and option3 >= option2:
        winning = WinningOption.EARNED
    else:
        winning = WinningOption.WORKDAYS_DPG

    # ── Stage 2: dollars ─────────────────────────────────────────────────
    per_chunk: list[ChunkResult] = []
    earned_dollars = Decimal("0")
    for c in inp.chunks:
        rate = c.rate_override if c.rate_override is not None else inp.hourly_rate
        chunk_dollars = c.raw_pch * rate * c.multiplier
        earned_dollars += chunk_dollars
        per_chunk.append(
            ChunkResult(
                source_id=c.source_id,
                kind=c.kind,
                raw_pch=c.raw_pch,
                multiplier=c.multiplier,
                rate=rate,
                dollars=_q_dollar(chunk_dollars),
                label=c.label,
            )
        )

    topup_pch = max(Decimal("0"), base_monthly_pch - option3)
    topup_dollars = topup_pch * inp.hourly_rate
    total_pay = earned_dollars + topup_dollars

    return EngineResult(
        base_monthly_pch=_q_pch(base_monthly_pch),
        winning_option=winning,
        option1_floor=_q_pch(option1),
        option2_workdays_dpg=_q_pch(option2),
        option3_earned=_q_pch(option3),
        earned_dollars=_q_dollar(earned_dollars),
        topup_pch=_q_pch(topup_pch),
        topup_dollars=_q_dollar(topup_dollars),
        total_pay=_q_dollar(total_pay),
        per_chunk=tuple(per_chunk),
    )
