# LEA Day-Status Consumer — Design

**Date:** 2026-07-24 · **Approved by:** author ("go ahead and build the consumer") · **Grounded in:** the live July 31 drop experiment (see memory `project-july31-drop-test`).

## Domain facts (verified on live feed data)

- BlueOne's LEA family is the day-status channel: `LEA - OFF`, `LEA - SICK`, `LEA - OFF/PAY PROTECTED` (company cancellation, consumed since PR #49), and `LEA - TRIP DROP` (approved mid-month drop — specimen captured 2026-07-24: all-day event, FLT legs + R/S removed, DESCRIPTION carries no note).
- All drops are company-approved; pre-award drops are baked into the FA (out of scope). Mid-month approved drops appear in the feed.
- Crew-scheduling notes do NOT export to the feed — nothing is designed around them.
- `OffEvent.label` is the exact text after `LEA - ` (e.g. `"TRIP DROP"`, `"SICK"`).

## Design

### 1. Feed-detected drops (`LEA - TRIP DROP`) — confirm-gated forfeit

**Reuse over invention:** the manual drop flow already does everything (a `VersionType.DROP` user-version → `apply_user_versions` stamps `VOLUNTARY_DROP` → engine forfeits PCH + floor 1:1 → day page and calendar show DROPPED). The feed path only adds *detection* and a *confirm gate*:

- New pure derivation `detect_feed_drops(month, off_days, rejected_dates)` in `schedule/apply_actuals.py`, run at the END of `_pipeline` (it never mutates the month). For each `TRIP DROP` off-event whose ANC-local date carries a Trip:
  - trip reason is `VOLUNTARY_DROP` → status **CONFIRMED** (the DROP version exists / pilot already dropped it);
  - date in `rejected_dates` → **REJECTED**;
  - else → **PROPOSED** — no pay change (published stays credited: the app never forfeits without a click), `AppliedEventKind.FEED_DROP` logged, badge shown.
  - No trip on the date → ignored (nothing to forfeit).
- New record `FeedDrop(date, original_aid, published_pch, status)` on `PipelineResult.feed_drops`.
- **Confirm** (`POST /day/<date>/feed-drop/confirm`): files the same DROP version the manual route files (company approval implied — the signal IS the company's), note `"Company drop (feed: LEA - TRIP DROP)"`, clears any REJECTED decision. Everything downstream (forfeit math, DROPPED tag both views, history + Restore link) works unchanged.
- **Reject** (`POST /day/<date>/feed-drop/reject`): persists REJECTED in new table `feed_drop_decisions` (PK `(user_id, date_iso)`, status + decided_at; new table → `create_all` creates it, no ALTER migration needed). Absence of a row = PROPOSED. Known edge (documented): restoring a confirmed feed drop re-proposes the badge — the pilot then Rejects to silence it.
- **UI:** day-page card ("Company drop detected — the feed shows this trip was dropped (LEA - TRIP DROP). Confirming forfeits X.XX PCH and lowers the guarantee accordingly.") with Confirm/Reject; calendar reuses the amber `needs_confirm` badge for PROPOSED dates.

### 2. Sick-day seeding (`LEA - SICK`)

New transform `apply_lea_reason_seeds(month, off_days)` run BEFORE `apply_overrides_to_month` (pilot override remains the final word): a `SICK`-labelled off-event on a date with a Trip or paying Day whose reason is `FLOWN` sets reason `SICK` and logs `AppliedEventKind.LEA_REASON_SEED`. No pay effect (SICK keeps published, protected) — the July 2/3 tags would have appeared with zero manual entry. Only the exact label `SICK` seeds; other labels are untouched.

### 3. DROPPED display unification

`load_day`'s `is_dropped` becomes `any(DROP version) OR trip.reason_code is VOLUNTARY_DROP` — closing the divergence where a reason-only drop (dropdown) showed DROPPED on the calendar but not the day page. The calendar side already reads the stamped reason.

## Out of scope

- Consuming `LEA - OFF` (no-op) and unknown future labels (ignored).
- Auto-seeding PTO/other reasons — no feed specimens yet; SICK only.
- Feed-note display (notes don't export).

## Testing

Schedule-level: detection statuses ×4, seeding (FLOWN→SICK, non-FLOWN untouched, non-SICK labels untouched). Storage: decision store round-trip. App-level: feed with `LEA - TRIP DROP` on a June trip day → card renders, confirm → DROPPED + forfeit visible in month total, reject → badge gone + published kept; `LEA - SICK` → auto SICK tag, pilot override back to FLOWN wins. Prod verify: July 31 shows the PROPOSED drop badge; pay unchanged (89.33) until the author confirms in the UI.
