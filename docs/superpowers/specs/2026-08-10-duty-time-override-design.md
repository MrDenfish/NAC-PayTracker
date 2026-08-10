# Pilot-Overridable Duty On / Duty Off + Schedule-Deviation Banner — Design

**Date:** 2026-08-10 · **Approved by:** author ("Yes looks good") · **Follows:** PR #76 (duty anchored to the packet's scheduled report; matcher tolerates unscheduled extra legs).

**Ships as two PRs.** PR A is the override (storage + form + precedence). PR B is the deviation banner. Each is independently useful; PR A is the primary request.

## Domain facts

- **Duty ON = the scheduled report time**, 1:00 before the *scheduled* first departure. The pilot reports on the published schedule, so a late push lengthens the duty day rather than moving its start (owner, 2026-06-27 and again 2026-08-10). PR #76 made the feed and pay paths honour this via the packet's `L Day Show`.
- **The iCal feed cannot supply a duty start.** BlueOne publishes only `UID`, `DTSTART`, `DTEND`, `SUMMARY`, `DESCRIPTION`, and `DTSTART` is rewritten to the delayed time before the leg freezes. The packet is the only surviving source. Verified 2026-08-10 — do not re-investigate.
- **Extra unscheduled legs are common** (diversion, tech stop, a field that won't take the landing). They are still the awarded pairing, and when leg count differs from the schedule the packet's block time no longer describes the day.
- The packet's `sched_duty_on` / `sched_duty_off` are **trip-level** (one show per pairing, day 1 only) bare local `"HH:MM"` strings with no date.
- There is no stored report clock today. The amend form's `#reassign-report` input **has no `name` attribute** — it has been inert since PR #39, feeding only the client-side `recompute()` that folds a number into `duty_hours`. `duty_on` is reconstructed downstream as `duty_off − duty_hours`.

## Design

### 1. Storage (PR A)

Two new **nullable** columns on `user_assignment_versions`:

- `duty_on_local VARCHAR(5)` — bare local `"HH:MM"`
- `duty_off_local VARCHAR(5)`

Added through `db._ADDED_COLUMNS` + `_ensure_added_columns` (idempotent `ADD COLUMN`, SQLite + Postgres). Nullable is mandatory and reads must handle NULL — existing rows have `duty_hours` but no clocks and must keep resolving exactly as they do today. **Verify against a simulated old-schema DB before deploy.** Bare `"HH:MM"` matches `VersionLeg.out_local` / `in_local` and the packet's own format.

`duty_hours` stays and is still written, but is now **derived server-side from the two clocks**. This is what keeps every downstream consumer working untouched: `_build_day_detail`'s `winner.duty_hours` path, the duty-rig candidate, and `recompute_pch_from_times` → `pch_value`.

**Midnight rule:** `duty_off_local` is on the same local date as `duty_on_local` unless it is `<=` it, in which case it is the next day. A trip ending 01:30 after an 04:41 show is 20.82 h, never negative. This rule lives in one helper and is used by both the derivation and the display.

### 2. Amend form (PR A)

- `#reassign-report` gains `name="duty_on_local"` — finishing the inert field.
- New `duty_off_local` time input beside it, defaulting to the currently computed duty off.
- The `duty_hours` input becomes a **computed read-only output**, fed by the existing `recompute()`. Clocks are the only editable representation, so the page cannot display a duty window that disagrees with its own duration — the exact defect class PR #76 fixed.
- Server handler accepts both clocks, derives `duty_hours`, and passes all three to `store.save`.

### 3. Duty resolution precedence (PR A)

One helper, three tiers, highest wins:

1. **Pilot override** — `duty_on_local` / `duty_off_local` on the winning version
2. **Packet scheduled show** for the front; last actual block-in + `TRIP_END_PAD_HOURS` for the back (PR #76 behaviour)
3. **Actual first block-out − `REPORT_PAD_HOURS`** when no packet trip resolves (reroute, off-day pickup, unparsed show)

### 3a. `DUTY_CORRECTION` — the override must work DOWNWARD (PR A)

**The problem.** `Trip.effective_pch` is `max(published, *all version pch_values)`, and `apply_actuals._apply_duty_extension` appends its own `AssignmentVersion` ("Duty extension from iCal", `apply_actuals.py:1028`) whenever actual block or duty beats published. A pilot override that *shortens* duty produces a lower `pch_value`, so `max()` ignores it and the correction silently does nothing. `CORRECTION` cannot rescue this: `correction_of` targets a **user** version seq, and the auto extension is not a user version — it is regenerated from the feed on every pipeline run.

**The fix: a duty correction is an INPUT, not a competing candidate.** New `VersionType.DUTY_CORRECTION` (a `StrEnum` value on an existing string column — no migration). When one exists for a date, `apply_actuals` uses the pilot's duty hours **in place of** `_actual_duty_hours(rt)` when recomputing the §3.E components. The recomputed trip PCH then flows through the normal `max(published, recomputed)` path untouched.

Why this and not "suppress the auto version": the auto extension credits **block** as well as duty. Suppressing it would discard flight-op credit the pilot never disputed — on Aug 8 that would drop a correct 7.13 to whatever duty alone gave. Replacing only the duty input keeps flight-op, trip-rig, and cumulative-DPG intact.

Consequences, all intended:

- Downward correction where **block still wins** → block keeps winning. Correct.
- Downward correction where **duty rig was winning** → effective drops to the corrected value, but never below `published`. The §3.E guarantee is structural and still holds.
- Upward correction → lifts exactly as any recompute does today.

Plumbing: `apply_actuals_to_month` takes a new `duty_overrides: dict[str, Decimal]` (date_iso → duty hours), the same shape as the existing `feed_reassignment_pch_overrides` parameter. The engine keeps its headless invariant — no import of `nac_pay.storage` from the engine; `_pipeline` resolves the dict and passes it in.

This also fixes the history label: a clock fix files a `DUTY_CORRECTION`, not a `REASSIGNMENT` for something that was never reassigned.

### 4. Deviation detection + banner (PR B)

A pure derivation at the end of `_pipeline`, same shape as `detect_feed_drops` — never mutates the month, recomputed every run. Emits one `ScheduleDeviation(date, kinds, detail, signature)` per affected day.

Triggers, either one sufficient:

- **Structural** — the flown sequence carries legs the packet pairing does not. Post-PR #76 these days now *match* (anchored subsequence), so they are exactly the days that would otherwise pass silently: Aug 8's `720/721/1780/1781/1781` against `720/721/1780/1781`.
- **Timing** — the actual duty window differs from the packet's scheduled window by more than **0:15**.

Only for days with a **resolvable packet trip**. Unmatched days already surface as reroutes or off-day pickups; double-notifying them is noise. Note this deliberately excludes *missing*-leg days (`730/731` flown against packet `730/731/732/733`): the matcher leaves them unmatched by design, so they are already on the reassignment path and are not a deviation case here.

`signature = flown_sequence + "|" + duty delta rounded to 2dp`, where the duty window is **always the feed-derived one, never the pilot's override**. This matters: if the signature were computed from the overridden window, correcting your duty times would change the delta, change the signature, and re-raise the banner you had just reviewed. Conversely, comparing only feed actuals means an override can never re-raise a reviewed day, while a genuinely new deviation (a new leg appears, the feed times move again) still does.

**Reviewed** persists in a new table `deviation_decisions` (`user_id`, `date_iso`, `signature`, `decided_at`; absence of a row = unreviewed). New *tables* need no migration — `create_all` handles them. Same pattern as `feed_drop_decisions`.

**UI:** a banner at the top of the day page naming what deviated, with a link into the amend form and a `Reviewed` button; plus a marker on the calendar cell so deviated days are visible without opening each one. Server-rendered, no JS, works offline in the PWA — matching the existing reroute confirm-badge pattern.

**Accepted consequence:** at a 0:15 threshold a typical month raises several banners, not one or two. On live August data Aug 1 flags at +0:40 (11.40 h actual vs 10.73 h scheduled). The author confirmed a trip running forty minutes long is worth knowing about.

## Out of scope

- Editing duty on a day with no assignment version (the override rides the existing amend flow only).
- Inline editing on the Times card — considered and rejected; a second edit path for a pay-affecting value.
- A modal dialog — rejected in favour of the banner.
- Per-leg scheduled departure times (the packet's function rows are not parsed; only the trip-level summary show time is).
- Deviation detection for unmatched days.
- Non-ANC domicile timezone for the clocks — the open `timeutil` item, unchanged here.

## Testing

**Storage:** round-trip of both clocks; NULL-clock rows resolve as before; `_ensure_added_columns` against a simulated old-schema DB.

**Derivation:** `duty_hours` from clocks; the midnight-crossing rule; precedence across all three tiers (override beats packet show beats actual-out fallback).

**`DUTY_CORRECTION` (the case the whole feature turns on):**
- Downward correction on a day where duty rig was winning → effective **drops** to the corrected value (this is the test that would have caught the silent no-op).
- Downward correction on a day where block still wins → block keeps winning, flight-op credit is NOT lost (the Aug 8 shape: 7.13 must survive a shortened duty).
- Effective never falls below `published`, in either direction.
- Upward correction lifts as any recompute does.
- No `DUTY_CORRECTION` for a date → `_actual_duty_hours` used exactly as today.

**Form/route:** posting both clocks persists them and derives `duty_hours`; posting neither leaves old behaviour intact; the derived `pch_value` reflects the overridden duty; the filed version's type is `DUTY_CORRECTION`, not `REASSIGNMENT`.

**Detection (PR B):** structural trigger (extra leg); timing trigger above and below the 0:15 threshold; no deviation raised for a day without a packet trip; a feed change re-raises a reviewed day; **a pilot duty override does NOT re-raise a reviewed day** (the signature ignores the override); `Reviewed` round-trip.

**Prod verify:** Aug 8 2026 — override duty on/off, confirm the day page and duty rig follow, and confirm the credited PCH moves only if duty rig becomes the winning candidate. Measure the retroactive impact read-only across all accounts before deploying, as done for PR #76.
