# Overnight Group Split + Off-Day Pickup Surfacing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two defects found in the 2026-07-23 prod incident: (1) the leg-grouper fuses two civil days' flying into one trip across an overnight rest, manufacturing a bogus reassignment (the "13.21 PCH on July 24" bug); (2) a company-added trip on an OFF day (the pilot's real July 23 callout, feed flights 2720/2721) is invisible — logged as `UNMATCHED_TRIP_REVIEW`, never surfaced, never credited. Plus UTC→ANC-local hygiene in two leftover sites.

**Architecture:** All changes ride the existing feed→reconciliation→apply_actuals pipeline. Fix 1 splits **unmatched** leg groups at overnight-rest boundaries (a gap that crosses an Anchorage-local midnight and is ≥ 6 h), then re-matches the pieces — matched groups are never touched, so current behavior for every matched pairing is preserved. Fix 2 extends the existing `FeedReassignment` proposal/confirm/reject machinery (same decision store, same routes, same day-page card) with a new `kind="OFF_DAY_PICKUP"` that adds a pickup `Trip` paying the §3.E recompute (DPG-floored) instead of attaching a version to a baseline trip. A new shared `nac_pay/timeutil.py` unifies the two hardcoded Anchorage-timezone helpers (an open item from PR #42).

**Tech Stack:** Python 3.11, frozen dataclasses, pytest (`-n auto` via pytest-xdist), FastAPI + Jinja2. No DB schema changes (the decisions table is keyed `(date_iso, signature)` and needs no new column).

## Global Constraints

- **Branch + PR workflow**: all work on branch `fix/overnight-split-offday-pickup`; never commit to `main`. (User memory: feedback_branch_pr_workflow.)
- **Autonomy grant (this run only)**: the author approved running end-to-end without per-step approval — including PR creation, merge to main, and prod deploy — provided progress notifications are sent (see Task 6). Blockers → notify and stop, don't guess.
- **DB isolation**: any one-off script that imports app storage must run with `NAC_PAY_DATA_DIR=$(mktemp -d)` — never against `~/.nac-pay/data`. Plain pytest is already isolated by conftest.
- **Test verification**: single-file pytest runs may run foreground; the FULL suite must run via `run_in_background` with `-n auto`, and success is judged by pytest's own exit code plus `grep -c "FAILED\|ERROR"` — never a piped `| tail` exit code.
- **Prod data**: never copy the pilot's documents off the box. Prod verification is a read-only `docker exec … python -c` probe.
- **Engine comments**: cite mechanisms, not §-numbers, as authoritative (CBA citations are unverified shorthand).
- Timezone: all civil-date attribution is America/Anchorage (`timeutil.DOMICILE_TZ`); non-ANC domicile remains explicitly out of scope.
- **Out of scope** (leave review-only / unchanged): unmatched sequences landing on an RSV day (the reserve-callout + manual ⚡ flow owns those); splitting *matched* multi-day pairings; the `merge_feed_bytes` ghost-leg latent issue.

---

### Task 1: Shared domicile-timezone helper (`nac_pay/timeutil.py`)

Pure refactor — dedupes the two hardcoded `America/Anchorage` helpers so Task 2 can use the conversion from the parsers layer (which currently has no tz code and must not import the app layer).

**Files:**
- Create: `src/nac_pay/timeutil.py`
- Create: `tests/test_timeutil.py`
- Modify: `src/nac_pay/schedule/apply_actuals.py` (delete local `_DOMICILE_TZ` + `_local_date` defs near lines 96–100, import instead)
- Modify: `src/nac_pay/app/services.py` (same, defs near lines 245–247)

**Interfaces:**
- Produces: `nac_pay.timeutil.DOMICILE_TZ: ZoneInfo`, `nac_pay.timeutil.local_date(dt: datetime) -> date`. Tasks 2–4 import these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_timeutil.py
from datetime import date, datetime, timezone

from nac_pay.timeutil import DOMICILE_TZ, local_date


def test_local_date_evening_anc_is_next_day_utc():
    # 18:00 AKDT Jul 24 = 02:00 UTC Jul 25 — civil date must be Jul 24.
    dt = datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)
    assert local_date(dt) == date(2026, 7, 24)


def test_local_date_winter_offset_is_utc_minus_9():
    # AKST (no DST): 08:59 UTC Jan 2 = 23:59 AKST Jan 1.
    dt = datetime(2026, 1, 2, 8, 59, tzinfo=timezone.utc)
    assert local_date(dt) == date(2026, 1, 1)


def test_domicile_tz_key():
    assert str(DOMICILE_TZ) == "America/Anchorage"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_timeutil.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'nac_pay.timeutil'`

- [ ] **Step 3: Write the module**

```python
# src/nac_pay/timeutil.py
"""Domicile (Anchorage) timezone helpers.

Feed timestamps are UTC; the Final Award, packet, and every /day route key
on the pilot's Anchorage-local civil date. This is the single home for that
conversion so the parsers, schedule, and app layers can't drift (the July 6
and July 23 2026 incidents were both UTC-vs-local date-attribution bugs).

NOTE: assumes an ANC domicile. A non-ANC base would need a profile-driven
timezone — tracked as an open item, deliberately not built yet.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

DOMICILE_TZ = ZoneInfo("America/Anchorage")


def local_date(dt: datetime) -> date:
    """Anchorage-local civil date of an aware (UTC) timestamp."""
    return dt.astimezone(DOMICILE_TZ).date()
```

- [ ] **Step 4: Point the two existing sites at it**

In `src/nac_pay/schedule/apply_actuals.py`: delete the `_DOMICILE_TZ = ZoneInfo("America/Anchorage")` assignment and the `def _local_date(...)` function (keep the explanatory comment above them, moving it onto the import). Add to the imports:

```python
from nac_pay.timeutil import DOMICILE_TZ as _DOMICILE_TZ, local_date as _local_date
```

Remove `from zoneinfo import ZoneInfo` if now unused (`grep -n "ZoneInfo\|_DOMICILE_TZ" src/nac_pay/schedule/apply_actuals.py` to confirm remaining uses — the module keeps the `_DOMICILE_TZ` alias because the day-view duty-window formatting may use it; if nothing else uses it, import only `local_date`).

In `src/nac_pay/app/services.py`: same treatment for its `_DOMICILE_TZ`/`_local_date` (defined near line 245). services.py DOES use `_DOMICILE_TZ` directly (duty-window `strftime` around line 1290), so keep both aliases there.

- [ ] **Step 5: Run the touched suites**

Run: `pytest tests/test_timeutil.py tests/schedule/test_apply_actuals.py tests/parsers/test_reconciliation.py -q`
Expected: all PASS (pure refactor — any failure means the refactor changed behavior; stop and fix before proceeding).

- [ ] **Step 6: Commit**

```bash
git add src/nac_pay/timeutil.py tests/test_timeutil.py src/nac_pay/schedule/apply_actuals.py src/nac_pay/app/services.py
git commit -m "refactor: single shared Anchorage local-date helper (nac_pay.timeutil)"
```

---

### Task 2: Split unmatched leg groups at overnight rests (the 13.21 bug)

The grouper (`_group_legs_chronologically`) chains station-connected legs with gaps ≤ 12 h and never breaks at a day boundary. In the July 23 incident, 769's evening arrival chained into 720's 05:41 departure the next morning, fusing `768/769` (Jul 24) with `720/721/1780/1781` (Jul 25) into one six-leg "trip" attributed entirely to Jul 24 and recomputed over the merged span → 13.21 PCH.

**Fix (minimal blast radius):** after grouping and matching, if a group is **UNMATCHED**, split it at any leg gap that (a) crosses an Anchorage-local midnight and (b) is ≥ `OVERNIGHT_REST_MIN_HOURS = 6.0`, then re-match each piece. Matched groups are returned untouched, so any genuine multi-day pairing that matches the packet keeps working exactly as today. The 6 h threshold is safe in both directions: no legal rest period is under ~8 h, and no intra-duty ground gap that crosses midnight approaches 6 h.

**Files:**
- Modify: `src/nac_pay/parsers/reconciliation.py` (`reconcile_feed_to_packet` body ~lines 128–145; new helper + constant below `DEFAULT_LAYOVER_MAX_HOURS`)
- Test: `tests/parsers/test_reconciliation.py`

**Interfaces:**
- Consumes: `nac_pay.timeutil.local_date` (Task 1).
- Produces: unchanged public API — `reconcile_feed_to_packet` may now return MORE (smaller) `ReconciledTrip`s for previously-fused unmatched groups. New module constant `OVERNIGHT_REST_MIN_HOURS: float = 6.0`.

- [ ] **Step 1: Write the failing tests** (reuse the file's existing `FlightLegEvent` builder — `grep -n "def _leg\|def _feed\|FlightLegEvent(" tests/parsers/test_reconciliation.py` and follow its construction pattern; build a `ParsedFeed` the same way neighboring tests do)

```python
def test_unmatched_fused_group_splits_at_overnight_rest():
    """July 23 2026 prod incident: 768/769 flown ANC evening Jul 24 chained
    into 720/721/1780/1781 the next morning (8.5h overnight gap, crosses ANC
    midnight). The fused 6-leg sequence matches nothing; it must split at the
    rest so each civil day's flying reconciles on its own."""
    utc = timezone.utc
    legs = (
        # 18:00–19:15 / 19:50–21:09 ANC Jul 24  (= 02:00Z+ Jul 25)
        _leg("768", datetime(2026, 7, 25, 2, 0, tzinfo=utc),
             datetime(2026, 7, 25, 3, 15, tzinfo=utc), org="ANC", dst="OME"),
        _leg("769", datetime(2026, 7, 25, 3, 50, tzinfo=utc),
             datetime(2026, 7, 25, 5, 9, tzinfo=utc), org="OME", dst="ANC"),
        # 05:41 ANC Jul 25 — 8.53h after 769 in, across ANC-local midnight
        _leg("720", datetime(2026, 7, 25, 13, 41, tzinfo=utc),
             datetime(2026, 7, 25, 15, 11, tzinfo=utc), org="ANC", dst="OME"),
        _leg("721", datetime(2026, 7, 25, 16, 1, tzinfo=utc),
             datetime(2026, 7, 25, 17, 26, tzinfo=utc), org="OME", dst="ANC"),
        _leg("1780", datetime(2026, 7, 25, 19, 0, tzinfo=utc),
             datetime(2026, 7, 25, 20, 35, tzinfo=utc), org="ANC", dst="DGG"),
        _leg("1781", datetime(2026, 7, 25, 21, 35, tzinfo=utc),
             datetime(2026, 7, 25, 23, 10, tzinfo=utc), org="DGG", dst="ANC"),
    )
    packet = {
        "768/769": _trip_pairing("768/769", "5.25"),
        "720/721/1780/1781": _trip_pairing("720/721/1780/1781", "6.08"),
    }
    result = reconcile_feed_to_packet(_feed(legs), packet)

    assert len(result.trips) == 2
    assert [t.flight_sequence for t in result.trips] == [
        "768/769", "720/721/1780/1781",
    ]
    assert all(t.match_status is MatchStatus.MATCHED for t in result.trips)


def test_unmatched_redeye_with_short_turn_stays_fused():
    """A midnight-crossing quick turn (55 min) is NOT a rest — the group
    must stay whole even though it's unmatched."""
    utc = timezone.utc
    legs = (
        # 22:30–23:45 ANC, then 00:40–01:55 ANC next civil day
        _leg("990", datetime(2026, 7, 25, 6, 30, tzinfo=utc),
             datetime(2026, 7, 25, 7, 45, tzinfo=utc), org="ANC", dst="OME"),
        _leg("991", datetime(2026, 7, 25, 8, 40, tzinfo=utc),
             datetime(2026, 7, 25, 9, 55, tzinfo=utc), org="OME", dst="ANC"),
    )
    result = reconcile_feed_to_packet(_feed(legs), {})
    assert len(result.trips) == 1
    assert result.trips[0].flight_sequence == "990/991"


def test_matched_multiday_group_is_never_split():
    """If the fused sequence matches a packet pairing (a genuine multi-day
    trip), the overnight split must not run."""
    utc = timezone.utc
    legs = (
        _leg("900", datetime(2026, 7, 25, 2, 0, tzinfo=utc),
             datetime(2026, 7, 25, 3, 15, tzinfo=utc), org="ANC", dst="OME"),
        # 8.5h overnight rest in OME, crosses ANC midnight
        _leg("901", datetime(2026, 7, 25, 11, 45, tzinfo=utc),
             datetime(2026, 7, 25, 13, 0, tzinfo=utc), org="OME", dst="ANC"),
    )
    packet = {"900/901": _trip_pairing("900/901", "8.00")}
    result = reconcile_feed_to_packet(_feed(legs), packet)
    assert len(result.trips) == 1
    assert result.trips[0].match_status is MatchStatus.MATCHED
```

(If `tests/parsers/test_reconciliation.py` has no `_trip_pairing` builder, import/copy the one from `tests/schedule/test_apply_actuals.py:109` — copy is fine, matching this file's local-builder style.)

- [ ] **Step 2: Run tests to verify the right failures**

Run: `pytest tests/parsers/test_reconciliation.py -v -k "overnight or redeye or never_split"`
Expected: `test_unmatched_fused_group_splits_at_overnight_rest` FAILS (gets 1 fused unmatched trip `768/769/720/721/1780/1781`); the other two PASS already (they pin current behavior so the fix can't overshoot).

- [ ] **Step 3: Implement the split**

In `src/nac_pay/parsers/reconciliation.py` — add import + constant + helper:

```python
from nac_pay.timeutil import local_date

# A station-chained gap this long that also crosses an Anchorage-local
# midnight is an overnight REST, not a layover: the two sides are different
# civil days' flying. Applied only to UNMATCHED groups — a fused sequence
# that matches a packet pairing is a genuine multi-day trip and stays whole.
# 6.0 splits every legal rest (≥~8h) while never splitting an intra-duty
# midnight turn (gaps well under 6h). See the 2026-07-23 incident: 768/769
# (Jul 24) + 720/721/1780/1781 (Jul 25) fused across an 8.5h overnight gap.
OVERNIGHT_REST_MIN_HOURS: float = 6.0


def _split_at_overnight_rests(
    group: list[FlightLegEvent],
    min_rest_hours: float = OVERNIGHT_REST_MIN_HOURS,
) -> list[list[FlightLegEvent]]:
    parts: list[list[FlightLegEvent]] = []
    current: list[FlightLegEvent] = [group[0]]
    for leg in group[1:]:
        last = current[-1]
        gap_hours = (leg.dt_start_utc - last.dt_end_utc).total_seconds() / 3600.0
        overnight = (
            gap_hours >= min_rest_hours
            and local_date(leg.dt_start_utc) > local_date(last.dt_end_utc)
        )
        if overnight:
            parts.append(current)
            current = [leg]
        else:
            current.append(leg)
    parts.append(current)
    return parts
```

Replace the body of `reconcile_feed_to_packet`'s grouping section (currently `grouped = ...` then a single list-comprehension) with:

```python
    grouped = _group_legs_chronologically(feed.flight_legs, layover_max_hours)
    reconciled: list[ReconciledTrip] = []
    for group in grouped:
        rt = _reconcile_one(group, packet)
        if rt.match_status is MatchStatus.MATCHED:
            reconciled.append(rt)
            continue
        parts = _split_at_overnight_rests(group)
        if len(parts) == 1:
            reconciled.append(rt)
        else:
            reconciled.extend(_reconcile_one(part, packet) for part in parts)
```

- [ ] **Step 4: Run the reconciliation suite**

Run: `pytest tests/parsers/test_reconciliation.py -v`
Expected: all PASS (new tests + every existing one).

- [ ] **Step 5: Run the downstream suite** (apply_actuals consumes reconciliation output)

Run: `pytest tests/schedule/ tests/integration/ -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nac_pay/parsers/reconciliation.py tests/parsers/test_reconciliation.py
git commit -m "fix: split unmatched feed groups at overnight rests (ANC-local midnight, >=6h gap)"
```

---

### Task 3: Surface company-added trips on OFF days (`kind="OFF_DAY_PICKUP"`)

Today an unmatched feed trip on a date with no baseline trip is a log-only `UNMATCHED_TRIP_REVIEW` — invisible, zero credit. New behavior for **non-RSV** dates: create a `FeedReassignment` proposal (`kind="OFF_DAY_PICKUP"`) and add a pickup `Trip` paying the §3.E recompute from actuals (which DPG-floors at 3.82 — exactly what the company quoted for 2720/2721). Same decision store / confirm / reject / company-PCH routes as reroutes; premium defaults to 1.0× and the pilot promotes it (e.g. 150% open time) on the day page — premiums are never preassigned. RSV dates keep the current review-only behavior.

**Files:**
- Modify: `src/nac_pay/schedule/apply_actuals.py` — `AppliedEventKind` (~line 79), `FeedReassignment` dataclass (~line 120), the `idx is None` branch of the unmatched loop (~line 286)
- Modify: `src/nac_pay/app/templates/day.html` — the card at lines 81–135
- Test: `tests/schedule/test_apply_actuals.py`, `tests/app/test_reassign.py`

**Interfaces:**
- Consumes: existing `_recomputed_reroute_pch(rt, None)`, `pickups` list, `decisions` / `pch_overrides` maps, `baseline_rsv_by_date`.
- Produces: `FeedReassignment.kind: str` field (`"REROUTE"` default / `"OFF_DAY_PICKUP"`), module constants `REASSIGN_KIND_REROUTE = "REROUTE"`, `REASSIGN_KIND_OFF_DAY_PICKUP = "OFF_DAY_PICKUP"`, `AppliedEventKind.OFF_DAY_PICKUP`. Task 5 and the template consume `fr.kind`.

- [ ] **Step 1: Write the failing engine-level tests** (in `tests/schedule/test_apply_actuals.py`, using its `_empty_month`, `_unmatched_trip`, `D` helpers; for the RSV test copy the RSV-day construction from the existing reserve-callout tests in the same file — `grep -n "RSV" tests/schedule/test_apply_actuals.py`)

```python
def test_offday_pickup_surfaces_as_proposal_with_dpg_floor():
    """2026-07-23 incident: company adds 2720/2721 on an OFF day. Must become
    a FeedReassignment proposal (kind OFF_DAY_PICKUP) + a pickup Trip paying
    the recompute — block 2.57 loses to the 3.82 DPG floor."""
    on = date(2026, 6, 12)
    baseline = _empty_month()                       # no trips, no RSV days
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    assert len(reassigns) == 1
    fr = reassigns[0]
    assert fr.kind == "OFF_DAY_PICKUP"
    assert fr.signature == "2720/2721"
    assert fr.original_aid == "OFF"
    assert fr.original_pch == D("0")
    assert fr.new_pch == D("3.82")
    assert fr.effective_pch == D("3.82")
    assert fr.status == "PROPOSED"
    assert fr.applied is True

    added = updated.trips[-1]
    assert added.trip_id == "2720/2721"
    assert added.published_pch == D("3.82")
    assert added.premium_category is PremiumCategory.OPEN_TIME_BID_PERIOD
    assert added.dates == (on,)

    assert any(e.kind is AppliedEventKind.OFF_DAY_PICKUP for e in events)
    assert all(e.kind is not AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)


def test_offday_pickup_rejected_adds_nothing():
    on = date(2026, 6, 12)
    baseline = _empty_month()
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions={(on.isoformat(), "2720/2721"): "REJECTED"},
    )
    fr = reassigns[0]
    assert fr.status == "REJECTED" and fr.applied is False
    assert fr.effective_pch == D("0")
    assert all(t.trip_id != "2720/2721" for t in updated.trips)


def test_offday_pickup_company_pch_override_wins():
    on = date(2026, 6, 12)
    baseline = _empty_month()
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions={(on.isoformat(), "2720/2721"): "CONFIRMED"},
        feed_reassignment_pch_overrides={(on.isoformat(), "2720/2721"): D("4.50")},
    )
    fr = reassigns[0]
    assert fr.status == "CONFIRMED" and fr.override_pch == D("4.50")
    assert fr.effective_pch == D("4.50")
    assert updated.trips[-1].published_pch == D("4.50")


def test_unmatched_on_rsv_day_stays_review_only():
    """Reserve days keep the current behavior — the callout flow owns them."""
    # Build a baseline whose 2026-06-12 Day is duty_type RSV (copy the Day
    # construction used by the reserve-callout tests earlier in this file).
    on = date(2026, 6, 12)
    baseline = _empty_month(days=(_rsv_day(on),))    # reuse/extract the file's RSV-day builder
    rt = _unmatched_trip("2720/2721", on_date=on)
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    _updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)
    assert reassigns == ()
    assert any(e.kind is AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/schedule/test_apply_actuals.py -v -k offday_pickup`
Expected: FAIL — `FeedReassignment` has no `kind`, `AppliedEventKind` has no `OFF_DAY_PICKUP`, and the branch doesn't exist. (`test_unmatched_on_rsv_day_stays_review_only` may already pass — fine, it pins the boundary.)

- [ ] **Step 3: Implement**

In `src/nac_pay/schedule/apply_actuals.py`:

(a) Add to `AppliedEventKind`:

```python
    OFF_DAY_PICKUP = "OFF_DAY_PICKUP"
```

(b) Below the `REASSIGN_REJECTED` constant:

```python
# FeedReassignment.kind — a reroute replaces an FA-scheduled trip; an
# off-day pickup is a company-added trip on a day with no scheduled flying.
REASSIGN_KIND_REROUTE = "REROUTE"
REASSIGN_KIND_OFF_DAY_PICKUP = "OFF_DAY_PICKUP"
```

(c) Add the field (last, with default — frozen dataclass) to `FeedReassignment`, and extend its docstring with one sentence: `kind` distinguishes a reroute of a scheduled day from a company-added trip on a day off (original_aid is `"OFF"`, original_pch 0, effective = the credited recompute/override).

```python
    kind: str = REASSIGN_KIND_REROUTE  # REROUTE | OFF_DAY_PICKUP
```

(d) Replace the `if idx is None:` branch of the unmatched loop with:

```python
        if idx is None:
            if first_date in baseline_rsv_by_date:
                # Reserve day — the callout path (matched loop) and the manual
                # ⚡ flow own these; an unmatched sequence here stays a review
                # item rather than guessing at callout pay.
                events.append(
                    AppliedEvent(
                        kind=AppliedEventKind.UNMATCHED_TRIP_REVIEW,
                        date=first_date,
                        trip_id=None,
                        detail=(
                            f"Flew sequence {rt.flight_sequence} ({len(rt.legs)} legs, "
                            f"actual_block={rt.actual_block_hours:.2f}h) — not in packet; "
                            "needs pilot categorization (charter? non-bid reassignment?)"
                        ),
                        delta_pch=None,
                    )
                )
                continue

            # Company-added trip on a day with NO scheduled flying (OFF /
            # leave): surface it like a reassignment — auto-credit, badge for
            # confirm/reject — instead of burying it in a log event (the
            # 2026-07-23 2720/2721 callout was invisible exactly this way).
            # Pay is the §3.E recompute from actuals, which DPG-floors at
            # 3.82; premium stays 1.0× until the pilot sets it (§7 — premiums
            # are never preassigned).
            signature = rt.flight_sequence
            new_pch = _recomputed_reroute_pch(rt, None)
            decision = decisions.get((first_date.isoformat(), signature))

            if decision == REASSIGN_REJECTED:
                feed_reassignments.append(
                    FeedReassignment(
                        date=first_date, signature=signature,
                        original_aid="OFF", original_pch=Decimal("0"),
                        new_pch=new_pch, effective_pch=Decimal("0"),
                        status=REASSIGN_REJECTED, applied=False,
                        kind=REASSIGN_KIND_OFF_DAY_PICKUP,
                    )
                )
                events.append(
                    AppliedEvent(
                        kind=AppliedEventKind.OFF_DAY_PICKUP,
                        date=first_date, trip_id=signature,
                        detail=(
                            f"Off-day pickup {signature} rejected — "
                            "day remains OFF"
                        ),
                        delta_pch=None,
                    )
                )
                continue

            status = decision if decision == REASSIGN_CONFIRMED else REASSIGN_PROPOSED
            override = pch_overrides.get((first_date.isoformat(), signature))
            credited = override if override is not None else new_pch
            pickups.append(
                Trip(
                    trip_id=signature,
                    published_pch=credited,
                    reason_code=ReasonCode.FLOWN,
                    premium_category=PremiumCategory.OPEN_TIME_BID_PERIOD,
                    workdays=rt.calendar_days_touched,
                    entry_mode=EntryMode.SIMPLE,
                    label=f"Company pickup {signature} on {first_date.isoformat()} (feed, day off)",
                    dates=(first_date,),
                )
            )
            feed_reassignments.append(
                FeedReassignment(
                    date=first_date, signature=signature,
                    original_aid="OFF", original_pch=Decimal("0"),
                    new_pch=new_pch, effective_pch=credited,
                    status=status, applied=True, override_pch=override,
                    kind=REASSIGN_KIND_OFF_DAY_PICKUP,
                )
            )
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.OFF_DAY_PICKUP,
                    date=first_date, trip_id=signature,
                    detail=(
                        f"Company-added trip {signature} on a day off "
                        + (
                            f"(company PCH {override:.2f}"
                            if override is not None
                            else f"(recomputed {new_pch:.2f}"
                        )
                        + f"); crediting {credited:.2f} at 1.0× — set the premium "
                        "on the day page if it qualifies"
                        + (" — confirmed" if status == REASSIGN_CONFIRMED
                           else " — needs confirmation")
                    ),
                    delta_pch=credited,
                )
            )
            continue
```

- [ ] **Step 4: Run engine tests**

Run: `pytest tests/schedule/test_apply_actuals.py -v`
Expected: all PASS, including the four new tests.

- [ ] **Step 5: Day-page copy for the new kind**

In `src/nac_pay/app/templates/day.html`, inside the `{% if data.feed_reassignment %}` card (line 81): make the heading and the two body paragraphs kind-aware. Change the `<h2>`:

```jinja
    <h2 class="card-title">
      {% if fr.kind == "OFF_DAY_PICKUP" %}New trip on your day off{% else %}Company reassignment detected{% endif %}
    </h2>
```

For the REJECTED paragraph, wrap the existing copy so OFF_DAY_PICKUP reads correctly (no FA original to revert to):

```jinja
    {% if fr.kind == "OFF_DAY_PICKUP" %}
    <p class="subtle">
      You rejected the feed's <strong>{{ fr.signature }}</strong> pickup —
      this day remains OFF (0.00 PCH).
    </p>
    {% else %}
    <p class="subtle">
      You rejected the feed's <strong>{{ fr.signature }}</strong> reassignment —
      the calendar shows the Final Award original
      <strong>{{ fr.original_aid }}</strong> ({{ "%.2f"|format(fr.original_pch) }} PCH).
    </p>
    {% endif %}
```

For the main (non-rejected) paragraph, same pattern:

```jinja
    {% if fr.kind == "OFF_DAY_PICKUP" %}
    <p>
      The schedule feed shows the company added
      <strong>{{ fr.signature }}</strong> on this day off. Crediting
      {% if fr.override_pch is not none %}
      company-assigned {{ "%.2f"|format(fr.override_pch) }}
      {% else %}
      recomputed {{ "%.2f"|format(fr.new_pch) }}
      {% endif %}
      → <strong>{{ "%.2f"|format(fr.effective_pch) }} PCH</strong> at 1.0×.
      If this pickup qualifies for a premium (e.g. 150% open time), set it
      in Reason &amp; premium below.
    </p>
    {% else %}
    ... (existing "reassigned from X to Y" paragraph, unchanged) ...
    {% endif %}
```

And in the CONFIRMED-block "Undo" button, make the label kind-aware:

```jinja
        <button type="submit" class="btn btn--secondary">
          {% if fr.kind == "OFF_DAY_PICKUP" %}Undo — remove pickup{% else %}Undo — revert to {{ fr.original_aid }}{% endif %}
        </button>
```

No changes to `services.py` day/calendar plumbing: the day view already selects `fr` by date regardless of trips (`services.py:1314`), the calendar badge keys off `applied` + `PROPOSED` (`services.py:680`), and the added pickup `Trip` renders in its cell like existing matched pickups. Confirm/reject/PCH routes are signature-keyed and work unchanged.

- [ ] **Step 6: App-level test** (in `tests/app/test_reassign.py`, following its existing client/fixture pattern — it already tests the confirm/reject routes for reroutes; mirror one test) — assert that a day whose pipeline produces an OFF_DAY_PICKUP renders "New trip on your day off" and the signature, and that POSTing `/day/<date>/reassignment/reject` flips it to the rejected copy. If constructing the feed fixture for an app test is disproportionate (these tests drive the real pipeline off fixture documents), a template-render unit test via the existing test-client day-page pattern is acceptable — but prefer the route test if a same-file precedent exists.

Run: `pytest tests/app/test_reassign.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nac_pay/schedule/apply_actuals.py src/nac_pay/app/templates/day.html tests/schedule/test_apply_actuals.py tests/app/test_reassign.py
git commit -m "feat: surface company-added trips on OFF days as confirmable pickups"
```

---

### Task 4: UTC → local-date hygiene (two leftover sites)

Same bug family as PR #42. Site 1: `services.py:1303` matches the day-view packet trip by `rt.first_dt_utc.date() == target` — an ANC-evening trip has the wrong UTC date, so the day page silently loses its packet duty-window. Site 2: `ReconciledTrip.calendar_days_touched` (`reconciliation.py:111,114`) counts UTC dates; it feeds `workdays` in `_recomputed_reroute_pch`, so an ANC-evening off-day pickup would count 2 workdays and overpay the cumulative-DPG candidate.

**Files:**
- Modify: `src/nac_pay/app/services.py:1303`
- Modify: `src/nac_pay/parsers/reconciliation.py` (`calendar_days_touched`)
- Test: `tests/parsers/test_reconciliation.py`

**Interfaces:** consumes `timeutil.local_date`; no signature changes.

- [ ] **Step 1: Write the failing test**

```python
def test_calendar_days_touched_uses_anchorage_local_dates():
    """A 15:00→17:00 ANC afternoon leg spans 23:00Z→01:00Z — two UTC dates,
    ONE civil day. Days-touched feeds workday counting and must be local."""
    utc = timezone.utc
    legs = (
        _leg("768", datetime(2026, 7, 24, 23, 0, tzinfo=utc),
             datetime(2026, 7, 25, 1, 0, tzinfo=utc), org="ANC", dst="OME"),
    )
    result = reconcile_feed_to_packet(_feed(legs), {})
    assert result.trips[0].calendar_days_touched == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/parsers/test_reconciliation.py -v -k days_touched`
Expected: FAIL — returns 2 (UTC dates {Jul 24, Jul 25}).

- [ ] **Step 3: Fix both sites**

`reconciliation.py` — in `calendar_days_touched`, replace the two `.date()` derivations and update the docstring's "(UTC)" note to "(Anchorage-local)":

```python
        days = {local_date(leg.dt_start_utc) for leg in self.legs}
        if self.legs:
            days.add(local_date(self.legs[-1].dt_end_utc))
        return len(days)
```

`services.py:1303` — replace `rt.first_dt_utc.date() == target` with `_local_date(rt.first_dt_utc) == target`.

- [ ] **Step 4: Run the touched suites**

Run: `pytest tests/parsers/test_reconciliation.py tests/schedule/ tests/app/test_day_detail.py -q`
Expected: all PASS. If an existing test pinned the UTC behavior, inspect it — the LOCAL date is correct by spec (§10 / PR #42 precedent); update the test's expectation and say so in the commit message.

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/parsers/reconciliation.py src/nac_pay/app/services.py tests/parsers/test_reconciliation.py
git commit -m "fix: Anchorage-local dates for days-touched and day-view packet lookup"
```

---

### Task 5: Integration regression — the July 23–25 incident, both feed states

One test that replays the incident end-to-end through `reconcile_feed_to_packet` → `apply_actuals_to_month` → `apply_feed_cancellations`, in both feed states I observed in prod (dates transposed to June to reuse this file's builders — semantics are identical; docstring records the real July dates).

**Files:**
- Test: `tests/schedule/test_apply_actuals.py` (append; reuse `_empty_month`, `_scheduled_trip`, `_trip_pairing`, `_leg`, and import `reconcile_feed_to_packet`, `OffEvent` as the file's existing imports allow)

**Interfaces:** consumes everything from Tasks 2–4. Feed construction: build the `ParsedFeed` the same way `tests/parsers/test_reconciliation.py` does (import or replicate its `_feed` helper; keep `off_days` settable for state B).

- [ ] **Step 1: Write the test (should pass if Tasks 2–4 are correct — it's the acceptance gate, and a failure here means a prior task is wrong)**

```python
def test_july23_incident_transitional_and_final_feed_states():
    """Replay of the 2026-07-23 prod incident (dates as June).

    FA: 23rd OFF, 24th 768/R1 (5.25), 25th 720/721/1780/1781 (6.08).
    State A (transitional): feed still has 768/769 on the 24th evening AND
    the 25th's four legs (8.5h overnight gap chains them), plus the new
    2720/2721 on the 23rd. Bug was: six legs fused → bogus 13.21
    "reassignment" of the 24th; 2720/2721 invisible.
    State B (final): 768/769 legs removed, LEA OFF/PAY PROTECTED on the 24th.
    """
    utc = timezone.utc
    d23, d24, d25 = date(2026, 6, 23), date(2026, 6, 24), date(2026, 6, 25)
    baseline = _empty_month(trips=(
        _scheduled_trip("768/R1", "5.25", d24),
        _scheduled_trip("720/721/1780/1781", "6.08", d25),
    ))
    packet = {
        "768/769/R1": _trip_pairing("768/769/R1", "5.25"),
        "720/721/1780/1781": _trip_pairing("720/721/1780/1781", "6.08"),
    }
    legs_23 = (   # 14:30–15:45, 16:20–17:39 ANC on the 23rd
        _leg("2720", datetime(2026, 6, 23, 22, 30, tzinfo=utc),
             datetime(2026, 6, 23, 23, 45, tzinfo=utc), org="ANC", dst="OME"),
        _leg("2721", datetime(2026, 6, 24, 0, 20, tzinfo=utc),
             datetime(2026, 6, 24, 1, 39, tzinfo=utc), org="OME", dst="ANC"),
    )
    legs_24 = (   # 18:00–19:15, 19:50–21:09 ANC on the 24th
        _leg("768", datetime(2026, 6, 25, 2, 0, tzinfo=utc),
             datetime(2026, 6, 25, 3, 15, tzinfo=utc), org="ANC", dst="OME"),
        _leg("769", datetime(2026, 6, 25, 3, 50, tzinfo=utc),
             datetime(2026, 6, 25, 5, 9, tzinfo=utc), org="OME", dst="ANC"),
    )
    legs_25 = (   # 05:41 → 15:10 ANC on the 25th
        _leg("720", datetime(2026, 6, 25, 13, 41, tzinfo=utc),
             datetime(2026, 6, 25, 15, 11, tzinfo=utc), org="ANC", dst="OME"),
        _leg("721", datetime(2026, 6, 25, 16, 1, tzinfo=utc),
             datetime(2026, 6, 25, 17, 26, tzinfo=utc), org="OME", dst="ANC"),
        _leg("1780", datetime(2026, 6, 25, 19, 0, tzinfo=utc),
             datetime(2026, 6, 25, 20, 35, tzinfo=utc), org="ANC", dst="DGG"),
        _leg("1781", datetime(2026, 6, 25, 21, 35, tzinfo=utc),
             datetime(2026, 6, 25, 23, 10, tzinfo=utc), org="DGG", dst="ANC"),
    )

    # ── State A: transitional feed ──────────────────────────────────────
    rec = reconcile_feed_to_packet(_feed(legs_23 + legs_24 + legs_25), packet)
    updated, events, reassigns = apply_actuals_to_month(
        baseline, rec, packet=packet,
    )
    # No bogus reroute of the 24th: 768/769 matched its own packet trip.
    assert all(fr.kind != "REROUTE" for fr in reassigns)
    # The 23rd surfaces as an off-day pickup at the DPG floor.
    pickup = next(fr for fr in reassigns if fr.kind == "OFF_DAY_PICKUP")
    assert pickup.date == d23 and pickup.signature == "2720/2721"
    assert pickup.effective_pch == D("3.82")
    # The 24th and 25th keep their published values.
    by_id = {t.trip_id: t for t in updated.trips}
    assert by_id["768/R1"].effective_pch == D("5.25")
    assert by_id["720/721/1780/1781"].effective_pch == D("6.08")

    # ── State B: final feed (cancellation posted, 768/769 gone) ─────────
    rec_b = reconcile_feed_to_packet(_feed(legs_23 + legs_25), packet)
    updated_b, _ev, reassigns_b = apply_actuals_to_month(
        baseline, rec_b, packet=packet,
    )
    cancel = OffEvent(
        uid="lea-1", label="OFF/PAY PROTECTED",
        dt_start_utc=datetime(2026, 6, 24, 8, 0, tzinfo=utc),
        dt_end_utc=datetime(2026, 6, 25, 7, 59, tzinfo=utc),
    )
    updated_b, cancel_events = apply_feed_cancellations(updated_b, (cancel,))
    by_id_b = {t.trip_id: t for t in updated_b.trips}
    assert by_id_b["768/R1"].cancelled_pay_protected is True
    assert by_id_b["768/R1"].effective_pch == D("5.25")
    assert by_id_b["720/721/1780/1781"].effective_pch == D("6.08")
    assert any(fr.kind == "OFF_DAY_PICKUP" and fr.date == d23 for fr in reassigns_b)
    assert len(cancel_events) == 1
```

(Adjust `OffEvent` construction to its real signature — `grep -n "class OffEvent" src/nac_pay/parsers/*.py`. If `_scheduled_trip`'s duty-extension path needs `matched` trips populated in `ReconciliationResult`, populate `matched=`/`unmatched=` from `rec.matched`/`rec.unmatched` as `apply_actuals_to_month` expects — pass `rec` straight through, as `_pipeline` does.)

- [ ] **Step 2: Run it**

Run: `pytest tests/schedule/test_apply_actuals.py -v -k july23_incident`
Expected: PASS. If it fails, a prior task is wrong — debug there, do not adjust the assertions to fit.

- [ ] **Step 3: Commit**

```bash
git add tests/schedule/test_apply_actuals.py
git commit -m "test: end-to-end regression for the 2026-07-23 fused-group / invisible-pickup incident"
```

---

### Task 6: Full verification, PR, merge, deploy, prod verify, notify

- [ ] **Step 1: Full suite in background**

Run (Bash `run_in_background: true`): `cd /Users/Manny/Python_Projects/NAC-Pay && pytest -n auto --timeout=60 -q`
Then verify **pytest's own exit code** from the task output and `grep -cE "FAILED|ERROR"` on the captured output (expect 0 matches). Expected: ~555+ passed in under 4 minutes.

- [ ] **Step 2: Push branch + open PR**

```bash
git push -u origin fix/overnight-split-offday-pickup
gh pr create --title "Split feed groups at overnight rests; surface off-day pickups" --body "<summary of the three fixes, the 2026-07-23 incident, and test evidence>"
```

- [ ] **Step 3: Merge to main** (authorized for this run) — `gh pr merge --squash --delete-branch`, then `git checkout main && git pull --ff-only`. The branch is not stacked, so the PR-#48-style auto-close hazard doesn't apply.

- [ ] **Step 4: Deploy**

```bash
ssh -i ~/.ssh/amis-key.pem ubuntu@35.80.137.164 'cd /opt/nac-pay && git pull --ff-only origin main && cd deploy && docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build'
curl -s https://pch-ledger.com/api/health
```

Expected: compose rebuilds and reports the container started; health endpoint returns OK.

- [ ] **Step 5: Prod verification probe (read-only)** — rerun the incident probe and assert the fixed state:

```bash
ssh -i ~/.ssh/amis-key.pem ubuntu@35.80.137.164 'docker exec nac-pay python -c "
from nac_pay.app import services
r = services._pipeline(2026, 7, \"u_511fcb054f5544f691cc48d2\")
frs = {str(fr.date): fr for fr in r.feed_reassignments}
fr23 = frs.get(\"2026-07-23\")
assert fr23 is not None and fr23.kind == \"OFF_DAY_PICKUP\" and fr23.signature == \"2720/2721\", fr23
print(\"Jul 23 pickup:\", fr23.signature, fr23.effective_pch, fr23.status)
t24 = next(t for t in r.updated_month.trips if any(str(d)==\"2026-07-24\" for d in t.dates))
assert t24.cancelled_pay_protected and t24.effective_pch == __import__(\"decimal\").Decimal(\"5.25\"), t24
t25 = next(t for t in r.updated_month.trips if any(str(d)==\"2026-07-25\" for d in t.dates))
print(\"Jul 24 cancelled 5.25 OK; Jul 25:\", t25.effective_pch)
"'
```

Expected: `Jul 23 pickup: 2720/2721 3.82 PROPOSED` (or the recompute if actual times beat 3.82 by then), `Jul 24 cancelled 5.25 OK; Jul 25: 6.08`. Note: by deploy time the pilot will have FLOWN 2720/2721; if actuals extended the duty, `effective_pch ≥ 3.82` is the correct assertion.

- [ ] **Step 6: Notify the pilot** — send push notifications (PushNotification tool; load schema via ToolSearch) at these milestones, not more often: (1) execution started; (2) all local tests green / PR opened; (3) merged + deployed + prod-verified, including the line "July 23 now shows pickup 2720/2721 — open the day page to Confirm and set the premium to Open time 150% if it qualified"; (4) any blocker, with what's blocked and why. The pilot must set the premium himself — the app never preassigns premiums.

- [ ] **Step 7: Update docs** — append a dated changelog entry to `docs/SYSTEM_CONTEXT.md` (top of Changelog, matching its entry style: the incident, the three fixes, PR number, test count) and commit it via a tiny follow-up PR `docs/changelog-<pr#>` (same merge authorization).

---

## Post-plan notes (context for the reviewer)

- **Why 6.0 h and only unmatched groups:** legal rest is never under ~8 h and intra-duty midnight ground gaps never reach 6 h, so 6.0 has margin both ways; restricting the split to unmatched groups means no currently-matched pairing (including genuine multi-day trips in the packet) can change behavior.
- **Why the pickup pays the recompute, not a packet value:** 2720/2721-style extra sections aren't in the packet; `_recomputed_reroute_pch(rt, None)` is `max(block, duty-rig, trip-rig, workdays×DPG)` from actual times — 3.82 for the July 23 callout, exactly what crew scheduling quoted. The company-PCH box (existing route) covers any discrepancy, and published-protection doesn't apply because there's no published value on an OFF day.
- **What stays broken on purpose:** unmatched sequences on RSV days (review-only, callout flow owns them), non-ANC domicile boundary attribution, `merge_feed_bytes` ghost-leg edge — all pre-existing, all documented in memory/SYSTEM_CONTEXT.
