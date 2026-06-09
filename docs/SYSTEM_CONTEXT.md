# NAC Pilot Pay Tracker — System Context (Living Document)

> **Audience:** Developers and AI assistants (Claude Code) building this program, plus the pilots who use it.
> **How to use:** Read top-to-bottom for full context, or jump to a section. If you are an AI assistant starting a new conversation, this is your single-source briefing. Build from Section 6 (the pay engine) outward.
> **Keeping it current:** Update the [Changelog](#changelog) whenever significant decisions or features change. Stale docs are worse than no docs.

---

## 1. Why This Exists

**The problem:** A Northern Air Cargo (NAC) pilot's pay is governed by the JCBA-2019 contract, Section 3 (Compensation). The rules are intricate — guarantees, rigs, premiums, reserve callouts, protected absences — and the company's published numbers can contain errors. A pilot has no easy way to independently track what they earned each month or to confirm the company got it right.

**What this program does:** It tracks a pilot's monthly pay per Section 3, using the company's own published values as the baseline and only recomputing when actual operations deviate. It also runs a one-time-per-month validation check that recomputes the contract's pay formulas from the packet's raw times and flags any discrepancy against the published values.

**Target user:** Initially a single pilot (the author, an ANC 737 First Officer). Architected so it can later become a multi-pilot app for any NAC pilot with **no rewrite of the pay logic** — all pilot-specific facts live in a profile object.

**What it is NOT:**
- Not a crew-scheduling system. It does **not** track annual FAR flight-time limits, construct lines, or manage bids — those are crew scheduling's job.
- It does **not** look up, verify, or assign pay rates. The pilot enters their hourly rate in preferences; the program uses it as given.
- It does **not** reproduce company/management administrative functions (paycheck timing, direct deposit, records — Section 3 subsections V–AA).

---

## 2. Architecture & The Two-Stage Pay Engine

The entire program is built around one idea: **PCH accounting and dollar calculation are two separate stages.**

```
┌─────────────────────────────────────────────────────────────────┐
│  INPUTS (normalize into ONE internal trip/activity model)         │
│                                                                   │
│  Master Schedule ──┐                                              │
│  (Final Awards)    │   the GUARANTEE + per-day published PCH      │
│                    │                                              │
│  Trip Pairing ─────┤──►  internal model  ──►  PAY ENGINE          │
│  Packet            │     (trips, legs,        (two stages)        │
│                    │      days, labels)                           │
│  iCal feed ────────┤   live actual leg times, change detection    │
│                    │                                              │
│  Manual entry ─────┘   pilot can add/edit/override anything       │
└─────────────────────────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┴──────────────────────────┐
        │  STAGE 1 — base monthly PCH (in raw PCH)            │
        │    = max( adjusted floor, workdays×DPG, earned sum )│
        │                                                     │
        │  STAGE 2 — dollars                                  │
        │    = Σ(chunk PCH × rate × multiplier)               │
        │      + guarantee top-up at regular rate             │
        └─────────────────────────────────────────────────────┘
```

**Stack:** Python (implementation language). The coding is handled by a separate Claude Code instance using this spec.

**Two foundational rules that pervade everything:**

1. **Published value is the guarantee; actual operations only ever push pay *up*, never down.** Trip PCH comes from the published TRIP PCH VALUE / Master Schedule value. The program recomputes only when something deviates (reassignment, reroute, cancellation, deadhead, duty extension), and pays the *greater of* published vs. recomputed.

2. **Raw PCH for the guarantee; multipliers only for dollars.** A chunk of premium PCH (e.g., 17.19 PCH of open time) counts at its *raw* value toward the monthly PCH total and guarantee comparison, but is *paid* at its multiplier (1.5×). The two layers are kept separate.

---

## 3. The Three Data Sources (plus manual entry)

| Source | Role | Provides | Has PCH? |
|--------|------|----------|----------|
| **Master Schedule (Final Awards)** | **Authoritative guarantee.** Parsed first each month. | Per-pilot, per-day assignment + duty type + published PCH; line value and floored monthly guarantee. | **Yes** (published, no computation needed) |
| **Trip Pairing Packet** | **Catalog + validation.** | Every trip's published PCH and its four-component breakdown; per-leg times. | Yes (with full breakdown) |
| **iCal feed** | **Live actuals.** Change detection + actual times. | Leg-by-leg flight data: flight #, route, tail, local out/in times, customer, crew. | **No** (must be reconciled against packet to inherit PCH) |
| **Manual entry** | **Override everything.** | Pilot can add/edit any trip, day, leg, label, or value. All data is editable. | Pilot's choice (value or times) |

**Dual-entry model (applies to every pay unit):**
- **Simple mode** — pilot enters the PCH value directly (the published guarantee). The default; used most of the time.
- **Detailed mode** — pilot enters actual start/end times; the program computes PCH from them.
- Effective PCH = `max(published_value, computed_from_actual)`. Detailed mode only matters when actual operations beat the published value (e.g., a duty extension).

---

## 4. Core Data Model

**Pilot profile** (the only multi-pilot-relevant config; keep separate so logic is reusable):
- `hourly_rate` — entered by the pilot in preferences. The program never looks this up or verifies it. Assumes one applicable rate per month; a per-trip rate override is available for the rare both-seats month.
- `position` (First Officer / Captain) — can also be inferred from the iCal crew lines (whichever line carries the pilot's name = CPT or FO). `fleet` is **737** — the only type NAC operates (the 767 has been retired) — so it's effectively fixed.
- (Longevity, hire date, contract DOS, Appendix A rate tables — **not needed**, since rate is entered directly.)

**Month**: `line_value` (from Master Schedule), `floor`, `sick_bank_days`, `pto_bank_days`, and a list of day/trip records.

**Trip / Day / Leg**:
- Trip: `trip_id`, published `pch_value`, list of legs, scheduled vs. actual times, `reason_code` (label), `premium_category` (label), entry mode.
- Leg: flight #, route, tail, scheduled & actual out/in times → block; landing flag. (Leg-level granularity is required for the Landing premium and for duty-extension recompute.)
- Day: assignment ID, duty type, PCH, reason code. Workday counting per the rule in §5.

**Two label families** (both pilot-editable; see §7):
- `reason_code` — *why a scheduled trip wasn't flown* (keeps published PCH or zeroes it).
- `premium_category` — *does this pay at a premium* (applies a multiplier).

---

## 5. Constants & Formulas

| Name | Value / Formula | Source |
|------|-----------------|--------|
| MPG (monthly floor) | 65 PCH | 3.D.1 |
| DPG (daily guarantee) | 3.82 PCH per workday | 3.D.2 |
| Flight Operation PCH | block_hours × 1 (1:1) | 3.E |
| Duty Rig PCH | duty_hours ÷ 2 (1:2) | 3.E |
| Trip Rig PCH | TAFB_hours ÷ 4.90 | 3.E |
| Cumulative DPG PCH | workdays × 3.82 | 3.E / 3.D.2 |
| Trip PCH (a trip's value) | `max(Flight Op, Duty Rig, Trip Rig, Cumulative DPG, Deadhead)` | 3.E |
| Guarantee floor | `max(line_value, 65)` | 3.D / Master Schedule |
| Classroom training/day | `max(4.0, (classroom_hours − lunch_deduction) ÷ 2)`; lunch_deduction = 1 hr if lunch in curriculum | 3.G.1 |
| Simulator/period | `max(5.0, sim_minutes/60 + 0.5 × num_briefs_and_debriefs)` | 3.G.2 |
| Training day guarantee | `max(scheduled_master_value, G.1/G.2 actual)` — master-scheduled value is guaranteed regardless of actual time | 3.G + pilot note |
| Home Study | `max(1.0, module_hours) × 0.5` PCH-equivalent, paid at regular rate, additive, no premiums | 3.H |
| Taxi (STUBBED) | `max(1/6, actual)` — not implemented; see §11 | 3.M |
| Rounding | 2 decimals, matching the packet | — |

**Workday counting (3.D.2):** a single duty period spanning two calendar days = **1** workday; two separate duty periods touching the same day = **2** workdays. Count from duty periods (report/release), not calendar dates.

**Premium multipliers** (per-chunk; see §7):

| Premium | Multiplier | Scope | Source |
|---------|-----------|-------|--------|
| Open time — qualifying | 1.5× | trip/assignment | 3.P.2 |
| Open time — non-qualifying (incl. bid period) | 1.0× | trip/assignment | 3.P.1 |
| Reassignment / reroute | 1.0×, pay `max(original, new)` | trip/assignment | 3.E.1.b |
| Overtime | 1.5× | trip/assignment | 3.Q |
| Junior Assignment (1st in rolling 3 mo) | 2.0× | trip/assignment | 3.R.1 |
| Junior Assignment (2nd+ in rolling 3 mo) | 2.5× | trip/assignment | 3.R.2 |
| Landing Credit | 1.5× | **single leg** (each segment over the limit) | 3.T |
| Hostile Area (STUBBED) | 2.0× | **duty period** (any hostile op in the period) | 3.U |
| NRFO with specialized training | 1.5× | assignment | 3.L.2 |
| Instructor/CA/APD (STUBBED) | varies | — | 3.S |

---

### Terminology & pay-stub category mapping (read this to avoid a common mix-up)

- **Line value (Final Award value)** is the month's guaranteed floor *input* (e.g., May = 65.29) → `floor = max(line_value, 65)`. Do **not** call this "base monthly PCH."
- **Monthly PCH** is the §6 *result*, `max(option1, option2, option3)`. When earned beats the floor, monthly PCH = earned and the **guarantee top-up is 0**. (A top-up is only the *shortfall* paid to lift you **up** to the floor when you earned less than it — never additional pay above the line.)
- **Open time** is 1.5× only when it **qualifies** for premium; non-qualifying open time (including bid-period pickups) is 1.0×. The company/pilot marks which.
- **Reassignments** are always 1.0×, paid `max(original, new)` (3.E.1.b) — not a premium. "Straight time" = 1.0×.
- **How internal categories roll up to the stub's two main earnings lines:**
  - **Regular pay** (stub) = scheduled line + reserve straight time + reassignments + any non-qualifying open time — everything at 1.0×.
  - **Open time** (stub) = only the qualifying 1.5× pickups.

  So in the May test: Regular pay 80.29 = line 65.29 + ~15.0 of straight-time additions (the reassignments + three added reserves); Open time 3.82 = the one qualifying premium pickup. Monthly PCH = max(65.29 floor, workdays×DPG, **84.11 earned**) = **84.11**; top-up = 0.

---

## 6. The Pay Engine (the algorithm to build first)

```python
# ── STAGE 1: base monthly PCH, all in RAW PCH ──────────────────────────

# Option 1 — adjusted MPG (the guarantee floor as events move it):
floor = max(line_value, 65)
# Apply each event in the order it occurs:
#   + open-time pickups            (added ON TOP of the floored value)
#   + involuntary-assignment / reserve-callout EXCESS = max(0, callout_pch - DPG)
#   - voluntary drops              (1:1; if this pushes below the floor, the floor
#                                    FOLLOWS DOWN to the actual remaining PCH — forfeit)
#   - lesser trades                (by the differential)
#   - unprotected unavailability   (by the dropped PCH)
#   (protected absences and company actions do NOT reduce the floor)
option1 = adjusted_floor

# Option 2 — daily guarantee across scheduled workdays:
option2 = scheduled_workdays * 3.82

# Option 3 — sum of everything actually earned/credited (raw PCH):
option3 = sum(chunk.raw_pch for chunk in all_credited_chunks)
#   chunks include: trip pairings, reserve days (at max(DPG, callout)), training,
#   PTO, leave (protected at scheduled PCH), moving, NRFO, taxi, open time, etc.

base_monthly_pch = max(option1, option2, option3)

# ── STAGE 2: dollars ───────────────────────────────────────────────────

earned_dollars = sum(chunk.raw_pch * hourly_rate * chunk.multiplier
                     for chunk in all_credited_chunks)   # each chunk at its own rate

topup_pch     = max(0, base_monthly_pch - option3)        # guarantee shortfall
topup_dollars = topup_pch * hourly_rate * 1.0             # top-up paid at REGULAR rate

total_pay = earned_dollars + topup_dollars
```

**Worked checks (must all hold):**

- *Normal month* — line 68, flown fully: option1=68, option3=68 → **68 PCH**, paid 68×rate.
- *Light protected month* — flew 40 + sick-credited 10, floor 65: option1=65, option3=50 → **65 PCH**; dollars = 50×rate + 15×rate top-up = 65×rate.
- *Reserve callout* — 17 reserve days (line 64.94 → floor 65), day 5 callout flies a 4.50 trip: option1 = 65 + (4.50−3.82) = 65.68; option3 = 16×3.82 + 4.50 = 65.62 → **65.68 PCH**; dollars = 65.62×rate + 0.06×rate top-up = 65.68×rate.
- *Voluntary drop + open time* — drop 3 reserve days (floor forfeits to 53.48), pick up 17.19 open time: → **70.67 PCH**; dollars = 53.48×rate + 17.19×rate×1.5.

---

## 7. Labels: Reason Codes & Premium Categories

Two pilot-editable label families drive the engine. Both default from the inputs but the pilot always has final say.

### Reason codes (why a scheduled trip wasn't flown)

| Code | Effect | Notes |
|------|--------|-------|
| PTO | Keep published PCH; floor preserved | Often preassigned on Master Schedule |
| SICK | Keep published PCH; floor preserved | Capped by Sick Leave Bank (3.J.1) |
| JURY | Keep published PCH; floor preserved | Open time on a day off during jury → premium (3.J.3.b) |
| BEREAVEMENT | Keep published PCH; floor preserved | — |
| TRAINING | Keep published PCH; floor preserved | Master-scheduled value guaranteed (§5) |
| MOVING | Keep published PCH; floor preserved | Paid move (3.K) |
| FAR | Keep published PCH; floor preserved | No computation — just a label (3.O) |
| MILITARY | `max(pro-rated MPG, earned PCH)`; dropped duties not credited | Special (3.J.2) |
| FMLA / unpaid LOA | 0 PCH | e.g., CULLY in the example schedule |
| (voluntary drop / lesser trade) | Reduces accumulated; can forfeit floor | Not a "keep" — see engine |

**Sources of the label:** (1) preassigned on the Final Award (the duty-type row seeds the code automatically), and (2) pilot edits/overrides after publication. Both write the same field; default = whatever the Final Award published.

### Premium categories (does this pay at a premium)

The pilot selects a category via a dropdown/checkbox when entering a trip; the program applies the multiplier from §5. A **custom/manual** option (custom multiplier or direct value) handles circumstances not predefined. Premiums are **never preassigned** on the Final Award — they only arise mid-month on company assignment, and the iCal feed may not tag them, so the pilot enters them. The pilot picks the *type*, not the percentage. For Junior Assignment the program **suggests** 2.0× vs 2.5× from the rolling-3-month counter but the pilot can override.

**Premium scopes:** trip/assignment (open time, overtime, JA), single leg (landing), duty period (hostile). The engine applies "this multiplier to this chunk of PCH at this scope."

---

## 8. Section 3 Subsection Rules (reference)

- **A. Longevity / B. Hourly Pay Rates** — Out of scope. Rate is pilot-entered; no Appendix A tables, no longevity→column mapping, no 2% escalation. Rate changes = pilot updates the preference. (Also covers W, pay when changing positions.)
- **C. Pay Credit Hours** — Monthly pay = monthly PCH × rate; premiums layer on top (see §6).
- **D. Pay Guarantees** — MPG 65 / DPG 3.82; floor = `max(line_value, 65)`. Drops/lesser-trades reduce & can forfeit the floor; protected absences and company actions don't; open time stacks on top (D.7). Unprotected unavailability reduces the floor by the dropped PCH.
- **E. Trip Pairing PCH** — Trip value = greatest of the four components (+ deadhead). Use the **published value**; recompute via these formulas only on deviation (reassignment/reroute/cancellation/deadhead/**duty extension**), then pay the greater of published vs. recomputed. Duty extension example: trip PCH 5, duty extends 8→12 hrs → `max(12÷2, 5)` = 6.
- **F. Reserve PCH** — Each reserve day = `max(DPG, assigned trip PCH)`. Split-trip R-1 = trip + 50% DPG; R-2 = trip + 100% DPG per RAP; callout from the RAP = trip + `max(callout PCH, the RAP credit)`. R-1/R-2 → R-4 reassignment = flat +1 PCH; flown from R-4 = `max(trip PCH, DPG)` per day. (Packet already folds split-trip credit into the published TRIP PCH VALUE.)
- **G. Training PCH** — Classroom & simulator formulas in §5; G.3 excess days-off = DPG each (min days-off per 12.D); G.4 reassigned-after-award = day-by-day `max(displaced line PCH, training credit)`. Master-scheduled value is guaranteed. Protected (no floor reduction).
- **H. Home Study** — `hours × 0.5` PCH-equivalent at regular rate, additive, **not** in the guarantee, no premiums. 1-hr minimum per module. Module hours = FAA/negotiated value (manual entry for now).
- **I. PTO** — Normal vacation: each day `max(scheduled PCH, DPG)`, protected, consumes a PTO day. I.3 payout-in-lieu = DPG × forgone days, additive. I.4 company-cancel options: **Opt 1** surrender PTO days → 4 PCH/day @ 200%; **Opt 2** keep days → 4 PCH/day @ 100%; both add 150% on overlapping flying (200% under I.5 excessive cancellations). I.7 separation payout = DPG × unused PTO days × rate. *Feature:* compute both options and show the trade-off.
- **J. Leave of Absence** — Sick/Jury/Bereavement: credit scheduled PCH, protected (Sick capped by bank). Military: `max(pro-rated MPG, earned)`, dropped duties not credited. FMLA/unpaid: 0.
- **K. Moving Travel Day** — Paid move (Section 6) → credit removed flying's PCH. Protected-style.
- **L. NRFO** — `max(scheduled, actual)` via E; +150% on all its PCH if specialized training required (additive).
- **M. Taxi** — STUBBED (see §11).
- **N. Monthly Cumulative PCH** — The greater-of-three rollup; see §6.
- **O. PCH Credit / FAR Limits** — Reduced to a `FAR` reason code that keeps published PCH. No annual flight-time tracker (out of scope, crew scheduling owns it).
- **P. Open Time Premium** — 1.5× **when it qualifies** for premium; 1.0× when it doesn't (e.g., bid-period pickups, or company-designated non-premium open time). P.3 last-minute cancel/replace still pays open-time premium on `max(scheduled PCH, DPG)`. P.2.a–d sick-interaction: build straight-pickup rule first (credit only what's flown), defer the trade+sick combinations.
- **Q. Overtime Premium** — 150% for day-off duty (irregular ops / staying with a repair aircraft) or voluntary unassigned-duty pickup (not reassignment/reroute).
- **R. Junior Assignment** — 200% (1st), 250% (2nd+ in rolling 3 months). Needs a rolling-3-month JA counter.
- **S. Instructor / Check Airman / APD** — STUBBED (see §11).
- **T. Landing Credit** — 150% on each leg over the per-duty-period landing limit (Section 15). **Leg-scoped**; actual-operations driven; uses per-leg block.
- **U. Hostile Area** — STUBBED (see §11). Flat 200%, duty-period scope.
- **V–AA** — Out of scope (eligibility, position-change proration, bypass pay, paycheck timing, direct deposit, records). Eligibility (V) is delegated to the pilot's categorization; picking one category per chunk is inherently consistent with no-stacking.

---

## 9. The Monthly Validation Check

Runs **once per packet load** (not on the pay path). For each trip in the packet:

1. Recompute the four E components from the printed raw times: Flight Op = block; Duty Rig = duty ÷ 2; Trip Rig = TAFB ÷ 4.90; Cumulative DPG = workdays × 3.82 (+ deadhead).
2. Compare each recomputed component *and* the recomputed max against the packet's printed component values and its TRIP PCH VALUE (tolerance ±0.01 for rounding).
3. Flag any mismatch.

This gives two independent safety nets: it catches packet errors and bugs in our own formula. Deadhead is the only nontrivial component; the packet's "Total DH" field flags trips that have it (refine then). For split-trips, add F.2/F.3 ("trip + RAP credit") as additional candidates in the max.

---

## 10. Input Parsing

### Master Schedule (Final Awards) — parsed first each month
- One page per fleet/position/month grid. Columns = calendar days (+ greyed next-month spillover for boundary trips); a **WD** summary column.
- Left columns: pilot 3-letter code + last name. One band per pilot.
- **Each day cell = 4 rows:** (1) assignment ID (line # / trip IDs / leave label), (2) duty type (FLT, RSV, PTO, FMLA, RGS CLASS, SIM, DH, VX, OFF), (3) **blank placeholder — always skip**, (4) **PCH value**.
- WD column: days-assigned (top, usually blank) + monthly totals (bottom). When the line sum < 65, a separate **65** is printed = `max(line_value, 65)`. (Confirmed on the page: 64.94 → 65.)
- The duty-type row seeds each day's reason code automatically.

### Trip Pairing Packet
- Per-trip page shows raw times (Sch. Block, Duty, TAFB, Total DH), the four component PCH values, and the **TRIP PCH VALUE**. Used for the validation check and as the PCH source when reconciling feed legs.

### iCal feed (live)
Event types are distinguished by a **summary prefix**. Known formats:
- **`FLT -` (flight leg):** `FLT - <flight#> <ORG>-<DST> <tail>`, local out/in times, `Customer:`, `CPT <name>`, `FO <name>`. No PCH, no grouping. Reconcile against the packet (match key ≈ flight # + date + route + departure time) to group legs into trips, inherit published PCH, and rebuild trip/duty boundaries. Unmatched legs = open-time pickups / reassignments / charter → flag.
- **`R/S -` (reserve / standby):** `R/S - Reserve or Standby at <base>` (e.g., ANC), the RAP window in local time (e.g., 3 AM–3 PM), and a **line designator** (e.g., `1021S`) that ties back to the Master Schedule line. Credits at DPG (§5, F); a callout appears as a `FLT -` leg landing on that reserve day.
- **`LEA -` (leave / non-duty):** `LEA - OFF` for a scheduled day off (all-day, 0 PCH). Other LEA subtypes (PTO, etc.) likely share this prefix.
- Crew lines on `FLT -` events give the pilot's position. (Fleet is 737-only — the 767 is retired — so no tail→fleet check is needed.)
- Live updates: on change, **save the original and recompute with the new** — the 3.E.1.b reassignment protection (greater of original vs. new); see §13 assignment history.

### Pay Stub (reference / compare target)
- Issued **semi-monthly** (e.g., 05/16–05/31), aligned to month halves so each month is exactly two checks. Net Pay at top, then an **Earnings** table.
- Columns: **Pay Type | Hours | Pay Rate | Current | YTD**, one row per category.
- Category rows use the company's own labels — align internal categories to these: **Regular Pay, Open Time, Paid Time Off, Sick, Home Study, Jury Duty** (Group Term Life is a benefit, not PCH).
- **Confirms the §6 model:** premium rows show **raw hours** at a **multiplied rate** — e.g., Open Time 3.82 hrs at $186.885 (= base $124.59 × 1.5) ≈ $713.90. Dollars = raw PCH × (base rate × multiplier).
- **Fixed MPG advance:** a fixed **32.50 PCH** (half of 65) is advanced mid-month and reversed at month-end (the 80.29 / −32.50 Regular Pay lines net to 47.79 = "Total Hours Worked," + 3.82 open time = 51.61 = "Total Hours"). Never varies. Handled only by the monthly-level compare in §13, since paycheck timing (subsection Y) is otherwise out of scope.

### Deferred discussion (not yet specified)
- The full **monthly load / matching process**: exact match keys & time tolerance, unmatched-leg handling, and trip/duty-boundary reconstruction from a flat leg list.
- Remaining iCal formats to sample: **training (CLASS/SIM), deadhead (DH), layover**, and the **R-1/R-2/R-4 distinction** within reserve.

---

## 11. Scope: Stubs & Out-of-Scope

**Stubbed** (recognized, minimal placeholder, easy to fill later):
- **M. Taxi** — real-world taxi is block time inside a trip (already counted) or a call-in at DPG 3.82 (floor already handles it); the literal 1/6 rule isn't used.
- **S. Instructor/Check Airman/APD** — does not apply to the author; revisit if other pilots need it.
- **U. Hostile Area** — very rare; expose `HOSTILE AREA → 2.0×, duty-period scope` as a manual category and let the generic premium engine handle it if it ever occurs. No detection logic.

**Out of scope:**
- A/B rate machinery & Appendix A — rate is pilot-entered.
- V–AA — administrative/management-side.
- Annual FAR flight-time accumulator — crew scheduling owns it.

---

## 12. Open Questions

- **Military proration (J.2):** exact "pro-rated MPG" method (by days available? by scheduled PCH?).
- **PTO I.4.a.(2):** PCH count for vacation days not covered by a PTO day (insufficient bank).
- **JA (R):** confirm 3rd-and-beyond JA in the rolling window also pays 2.5×; build the rolling-3-month counter.
- **Rolling-12-month** vacation-cancellation counter for I.5 (200% escalation) — build now or stub.
- **FAR (O) / Landing (T):** the actual numeric limits live in Section 15 (configurable constants); for Landing, which leg(s) count as "excess" when over by more than one (assume chronologically last).
- **Split-trip validation:** confirm whether the packet uses duty-rig or F.2 "trip + 50% DPG" for split-trips (validation should take the greater).
- **iCal non-flight event formats:** reserve / training / layover / deadhead prefixes (sample needed).
- **Training:** how to count briefs/debriefs if not itemized (default 1 brief + 1 debrief = +1.0), and multi-session classroom-day aggregation.
- **P.2.b–d:** trade-plus-sick interaction (defer; build straight-pickup P.2.a first).

---

## 13. User Interface (GUI)

**Platform:** A web app in the style of the StockAgent example — **FastAPI + HTMX + Jinja2**. **Desktop-first**, with a mobile version to follow. The pay engine (§6) stays headless; the GUI is a thin layer over it.

**Design principle — mirror documents the pilot already reads,** so there's no learning curve:
- The **calendar** mirrors the Master Schedule grid (color-coded duty types, per-day PCH).
- The **pay breakdown** mirrors the pay stub (per-category rows, raw hours beside the multiplied rate — see §10 → Pay Stub).

### Core screens

| Screen | Purpose | Mirrors |
|--------|---------|---------|
| Dashboard | Headline: month-to-date PCH and $ vs. guarantee. The "am I paid right?" glance. | — |
| Calendar (month) | Per-day assignment, duty type, PCH; tap to drill in. | Master Schedule |
| Trip/Day detail & edit | Dual-entry (value vs. times), reason-code + premium-category dropdowns, assignment history. | — |
| Pay breakdown | Per-category ledger; raw PCH × multiplied rate; guarantee top-up. | Pay stub |
| Discrepancies | Monthly validation flags (trips that don't match the packet). | — |
| Compare to pay stub | Pilot enters the two monthly checks; program nets the fixed advance and compares by category. | Pay stub |
| Settings | Hourly rate, fleet/position, sick/PTO banks, feed URL. | — |

### Readability principles
- Lead with the big number; progressively disclose detail on tap.
- Speak the pilot's vocabulary (PCH, MPG, DPG, TAFB).
- Badge **published vs. actual** values so it's obvious where extra pay came from.
- Show **"what changed"** (original vs. revised) whenever the feed updates a day.

### Feed-driven workflow
- The iCal feed **seeds the initial load** when the schedule publishes.
- A **once-daily update** pulls changes thereafter.
- Manual entry can add/edit/override anything at any time.

### Assignment history & the change rule (realizes E.1.b)
Each day holds a stack of assignment versions:
- The **most current** assignment is primary — what the pilot sees.
- Prior versions are **stored and viewable** for reference.
- The day's **PCH = max(original, current)**. Example: 720/772 (5.33) → 720/1780 (6.08) shows *and* pays 6.08. If a revision were worth *less* than 5.33, the pilot still sees the current assignment, but PCH stays **protected at 5.33**, and the GUI shows the protection explicitly (e.g., "current 720/X (4.00) — protected at 5.33 from original 720/772").

### Compare-to-pay-stub (monthly level)
Pay is issued twice a month, each check carrying a **fixed 32.50 PCH MPG advance** (half the 65 guarantee, never varies). Comparison is done at the **monthly** level: sum both checks' per-category "Current" amounts, net the fixed +32.50 / −32.50 advance and reversal (they cancel within a month), and compare the category sums against the program's computed monthly figures. Because the advance is fixed and the periods align to month halves, this is exact.

### Per-screen detail

1. **Dashboard** (home) — month switcher + identity line (incl. the hourly rate everything derives from); three metric cards (total PCH, gross $, vs-guarantee — green when above); a status strip (schedule loaded, feed last update, live discrepancy count); a compact by-category bar with premium chunks tagged by multiplier; quick links to the other screens. Lead with the number, disclose detail on tap.
2. **Calendar (month)** — mirrors the Master Schedule grid: Monday-start weeks, cells color-coded by duty type (with a legend), each showing assignment + PCH. Flag icons mark reassigned days (↔), reserve callouts (⚡), and validation discrepancies (⚠). Footer keeps a running month total vs. the 65 guarantee. Tap a day → detail/edit.
3. **Day detail & edit** — opens from a calendar day. An assignment-history block shows the current assignment over any stored original, with "paid the greater." An entry-mode toggle switches Value ↔ Actual times (the dual-entry). A Reason dropdown (Flown / PTO / Sick / Jury / Bereavement / FAR / Military / Day off) and a Premium dropdown (None / Open time / Overtime / Junior assignment / Landing / Custom — shows the multiplier, not a percentage). A legs table (block → flight-operation, duty → duty rig) drives the recompute in Actual-times mode. Save / Cancel.
4. **Pay breakdown** — mirrors the pay stub: a per-category earnings table (Pay type | PCH | Rate | Amount) with premium rows shown as raw PCH × the multiplied rate. Below it, the greater-of-three guarantee computation with the winning option highlighted and the top-up (when any) stated, paid at the regular rate.
5. **Compare to pay stub** — a verdict banner (is there a discrepancy, and how much); a tracker-vs-company table by category with the mismatch highlighted, zero rows collapsed, and a gross total; source chips for the two semi-monthly checks with the fixed 32.50 advance netted, plus an import/enter affordance. Reconciliation is monthly.
6. **Discrepancies** — one queue for validation flags, compare mismatches, and unmatched feed legs; each tagged by source with a direct action (review / view / categorize); money-owed items sort to the top.
7. **Settings** — profile (hourly rate — the basis for every figure; position; fleet fixed at 737), leave banks (sick / PTO — these cap crediting), schedule feed (masked URL + test, auto-update toggle), and contract constants (MPG 65, DPG 3.82 — editable only on a new contract).

---

## Changelog

| Date | Change |
|------|--------|
| 2026-06-09 | **iCal feed parser shipped — sample confirms three documented prefixes only.** Built `parsers.parse_ical_feed` returning typed `FlightLegEvent` / `ReserveEvent` / `OffEvent` records plus an `UnknownEvent` bucket so the parser fails open on undocumented formats. Sample at `docs/iCal_schedule_feed.ics` (Dennis FISHER's June 7–29 2026 roster) contains 7 FLT legs / 9 R/S reserves / 12 LEA OFF and **no other prefixes** — meaning the §10-deferred formats (CLASS/SIM training, DH deadhead, layover, R-1/R-2/R-4 reserve distinction) remain genuinely unsampled. Two cross-source matching keys established: `FlightLegEvent.flight_no_short` strips the iCal-only `NC` carrier prefix to match the Master Schedule / Packet form (`NC768 → 768`); `ReserveEvent.line_designator_short` strips the iCal-only trailing letter (`1021S → 1021`) to match the Master Schedule line. Block hours come from `DTEND - DTSTART` (UTC). |
| 2026-06-07 | **Terminology + category-mapping clarified (§5).** Resolved a "base monthly PCH vs line value" mix-up surfaced during the May acceptance test: line value = the floor input; "monthly PCH" = the §6 greater-of result; a guarantee top-up only lifts you *up* to the floor (never adds above the line). Clarified that open time is 1.5× only when it qualifies (else 1.0×), reassignments are 1.0× `max(original, new)`, and that on the stub "Regular pay" aggregates line + reserve straight time + reassignments + non-qualifying open time, while "Open time" is only the qualifying 1.5× pickups. |
| 2026-06-07 | **Per-screen detail added to §13; 737-only.** Folded the seven finalized screen designs (Dashboard, Calendar, Day detail & edit, Pay breakdown, Compare to pay stub, Discrepancies, Settings) into §13 with purpose, key elements, what each mirrors, and on-screen actions. Removed B767 references — NAC now operates the 737 only (767 retired), so fleet is effectively fixed and the tail→fleet check is dropped. |
| 2026-06-07 | **GUI design + pay-stub format added (§13, §10).** Chose a web app (FastAPI + HTMX, desktop-first then mobile) that mirrors existing documents — calendar = Master Schedule, breakdown = pay stub. Defined the core screens, the feed-driven workflow (initial load + once-daily update), and the assignment-history/change rule (current assignment displayed, prior versions stored, day PCH = max(original, current)). Documented the pay-stub format, which **confirmed the raw-PCH × multiplied-rate model** (Open Time billed at base × 1.5). Compare-to-pay-stub works at the monthly level, netting the fixed 32.50 PCH semi-monthly MPG advance. |
| 2026-06-07 | **Initial spec assembled.** Complete walkthrough of JCBA-2019 Section 3 (Compensation), subsection by subsection, with the author. Established: the two-stage pay engine (raw-PCH guarantee via greater-of-three, then per-chunk dollar multipliers); guarantee floor = `max(line_value, 65)` (reduced only by voluntary drops/lesser-trades, increased by open time/involuntary assignments, protected against company actions and protected absences); three data sources (Master Schedule = authoritative guarantee, Trip Pairing Packet = catalog + validation, iCal feed = live actuals) plus manual entry, all dual-entry (value or actual times); two label families (reason codes + premium categories), both pilot-editable; the monthly validation check; published value is the guarantee, actual times only push pay up. Stubbed M, S, U. Out of scope: A/B rate machinery (rate pilot-entered), V–AA, annual flight-time tracker. Open questions catalogued in §12. |
