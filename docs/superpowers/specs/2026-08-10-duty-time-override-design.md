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

Pay follows automatically — clocks → `duty_hours` → duty rig → `pch_value` — through existing machinery. The feed-side `_actual_duty_hours` is deliberately untouched: a filed version already outranks it downstream.

### 4. Deviation detection + banner (PR B)

A pure derivation at the end of `_pipeline`, same shape as `detect_feed_drops` — never mutates the month, recomputed every run. Emits one `ScheduleDeviation(date, kinds, detail, signature)` per affected day.

Triggers, either one sufficient:

- **Structural** — the flown sequence carries legs the packet pairing does not. Post-PR #76 these days now *match* (anchored subsequence), so they are exactly the days that would otherwise pass silently: Aug 8's `720/721/1780/1781/1781` against `720/721/1780/1781`.
- **Timing** — the actual duty window differs from the packet's scheduled window by more than **0:15**.

Only for days with a **resolvable packet trip**. Unmatched days already surface as reroutes or off-day pickups; double-notifying them is noise. Note this deliberately excludes *missing*-leg days (`730/731` flown against packet `730/731/732/733`): the matcher leaves them unmatched by design, so they are already on the reassignment path and are not a deviation case here.

`signature = flown_sequence + "|" + duty delta rounded to 2dp`. A *changed* deviation re-raises a previously reviewed day; an unchanged one stays quiet.

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

**Form/route:** posting both clocks persists them and derives `duty_hours`; posting neither leaves old behaviour intact; the derived `pch_value` reflects the overridden duty.

**Detection (PR B):** structural trigger (extra leg / missing leg); timing trigger above and below the 0:15 threshold; no deviation raised for a day without a packet trip; signature change re-raises a reviewed day; `Reviewed` round-trip.

**Prod verify:** Aug 8 2026 — override duty on/off, confirm the day page and duty rig follow, and confirm the credited PCH moves only if duty rig becomes the winning candidate. Measure the retroactive impact read-only across all accounts before deploying, as done for PR #76.
