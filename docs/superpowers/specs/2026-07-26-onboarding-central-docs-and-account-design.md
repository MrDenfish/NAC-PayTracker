# Onboarding Overhaul: Central Documents, Feed Link, Account Deletion, Friendly Empty States

**Date:** 2026-07-26
**Status:** Approved design (Approach A)
**Origin:** First outside-pilot signup test (iPhone). The tester stalled on onboarding
step 2: didn't know how/where to save the Final Award and Trip Pairing PDFs, and the
iCal field wanted a file when BlueOne hands out a link. Also surfaced: raw JSON error
bodies on data pages for months with no documents, and no way to delete an account.

## Goals

1. A new pilot completes signup → onboarding → working calendar with **zero file
   uploads** (documents are published centrally by the site admin once per month).
2. The iCal feed is connected by **pasting a link**, not uploading a file.
3. A pilot can **delete their account** (immediate hard delete for now; the design
   leaves room for a grace-period variant when there are paying subscribers).
4. No route ever shows a raw `{"detail": ...}` JSON body to a browser.
5. Onboarding helps a pilot **identify their 3-letter pilot code** instead of failing
   silently later when the code doesn't match the Final Award.
6. Pilots can **view the full Final Award and Trip Pairing PDFs** in the app.

## Non-goals (deliberate)

- No company/base/fleet scoping (Aloha Air Cargo is "down the road"; the keying below
  can grow a scope column without rework).
- No role system — admin is an env-var allowlist, not a schema concept.
- No grace-period deletion yet; no Stripe cancellation handling (Stripe is still fake).
- The two-packets-per-month revision case stays deferred (unchanged from before).

---

## 1. Central document store + admin upload

### Storage

- New `SharedDocumentsStore` (mirrors `UserDocumentsStore`, `storage/documents.py`).
- Files: `{data_dir}/shared/docs/{year}-{month:02}/`
  - `final_award_{slot}.pdf` — **multi-slot.** Usual case: slot 0 = FO sheet,
    slot 1 = CA sheet. Occasionally the company ships one combined two-page PDF —
    that's a single slot. The parser reads every pilot band in whatever file it gets,
    so both shapes work without parser changes.
  - `packet.pdf` — single slot.
- New DB table `shared_documents`: PK `(year, month, kind, slot)`; columns
  `original_name`, `uploaded_at`, `uploaded_by` (user_id, informational), `size_bytes`.
  Brand-new table → `create_all` creates it on deploy; **no ALTER migration needed.**

### Resolution: personal upload always wins

`services.documents_for_user(user_id, year, month)` gains one fallback step per kind:

1. personal upload (existing `user_documents`) — wins if present;
2. else shared documents for that month;
3. else the month is unavailable.

- FA may now resolve to **multiple paths** (the shared slots). `_pipeline` parses each
  and merges the `dict[pilot_code, PilotMonthSchedule]` results in slot order (later
  slot wins a duplicate code; duplicates aren't expected between FO/CA sheets). The
  per-file parse cache (`(path, mtime, size)` keying) already handles multiple files.
- A personal FA replaces the shared set entirely (it's one pilot's own copy).
- Feed remains **per-user only** — never shared.
- `available_months(user_id)` (dashboard + PWA pre-warm) becomes the union of personal
  and shared months. A month counts as available when an FA (personal or shared) AND a
  packet (personal or shared) exist.

### Admin

- New env var `ADMIN_EMAILS` (comma-separated, case-insensitive match on the account
  email). Helper `is_admin(user)`. Add to the config matrix in SYSTEM_CONTEXT §14 and
  to the box's `deploy/.env.prod` (`ADMIN_EMAILS=dennfish@gmail.com`).
- Nav shows an **Admin** link only for admins.
- Routes (admin-gated; non-admins get 404, not 403 — don't advertise the surface):
  - `GET /admin/documents` — year/month picker (defaults to current month), list of
    uploaded shared docs (original filename, upload date, size, parsed pilot count),
    upload forms, per-file delete buttons.
  - `POST /admin/documents/upload` — kind + year/month + file. **Parse-on-upload:**
    the PDF is parsed immediately; an unreadable file or an FA with zero pilot bands
    is rejected with the error shown. On success the page reports what was found
    ("24 pilot codes: AAA, BBB, …" / packet trip count) so a bad sheet is caught at
    upload time, not when a pilot complains.
  - `POST /admin/documents/delete` — remove a shared file (DB row + file on disk).

### Document viewing (pilot-facing)

- `GET /documents/view/{year}/{month}/{kind}` (+ `/{slot}` for FA) — auth-gated
  `FileResponse` streaming the PDF, resolved with the same personal-first-then-shared
  rule as the pipeline, so a pilot always views exactly what their numbers came from.
- The Documents page shows a **View** link next to each available document (FA slots,
  packet). Browsers (including iPhone Safari) render PDFs natively — no viewer to build.
- Network-only: these routes are **never pre-cached by the PWA service worker**
  (packets are multi-megabyte).

## 2. Onboarding step 2: feed link instead of files

Step 2 ("Documents") is replaced by **"Connect your live schedule (optional)"**:

- One text field: iCal feed URL. Saves to the existing `pilot_profiles.feed_url` and
  sets `feed_auto_update = True`. No new storage. The year/month inputs and all three
  file inputs are removed.
- Validation: must be http(s); on save the app **fetches it once immediately** (same
  code path as the hourly updater, writing current + next month) so the pilot's first
  dashboard already has live data. A fetch failure re-renders the form with the error;
  the pilot can fix the link or skip.
- Copy (both points, per author):
  - the feed link keeps day-to-day flights current automatically (reroutes, drops,
    cancellations, pickups);
  - the published Final Award / Trip Pairing documents are monthly publications loaded
    by the site — they do not update through the feed;
  - where to find it in BlueOne: export → calendar feed → **copy the link** (don't
    download the file).
- "Skip for now" remains; Settings keeps the same field. The `.ics` **file** upload
  survives on the Documents page as a fallback only.

### Pilot-code assist (step 1)

Backed by a shared FA parse: the current month's if uploaded, otherwise the most
recent earlier month that has one (pilot codes are stable month to month):

- **Instant confirmation:** the code the pilot enters is checked against the FA and
  answered in place — "✓ TRN — matches DENFISH on the August 2026 Final Award" or a
  **non-blocking warning** ("not found on the current Final Award — double-check the
  code printed on your award sheet"). Never a hard block: their sheet may not be
  uploaded yet, or they're a new hire.
- **"Find my code":** enter last name → matching code(s) from the current FA. This is
  the rescue path for a pilot who has never noticed the 3-letter code.
- Implementation: one small JSON endpoint (e.g. `GET /onboarding/code-lookup`)
  accepting `code=` or `last_name=`, called from a few lines of vanilla JS (no HTMX —
  it was removed). Non-JS fallback: the POST handler runs the same check and re-renders
  with the confirmation/warning.
- Privacy note (accepted by author): the endpoint exposes last-name→code pairs to
  authenticated, email-verified users mid-signup. The Final Award is distributed to
  every pilot at the company, so this data is already company-public; the endpoint
  returns **only** code, last name, and position — nothing else from the sheet.
- If no shared FA exists at all, the assist quietly disappears (plain field, current
  behavior).

## 3. Delete account

- **Settings → Danger Zone** (bottom of page): "Delete account" link →
  `GET /account/delete` confirmation page: requires current password + typing `DELETE`.
- `POST /account/delete` verifies both, then calls one new service function
  `delete_account(user_id)`:
  1. deletes all user-keyed rows **explicitly, in one transaction** — the ten tables:
     `pilot_profiles`, `day_overrides`, `email_verifications`, `password_resets`,
     `user_documents`, `user_assignment_versions`, `user_version_legs`,
     `feed_reassignment_decisions`, `feed_drop_decisions`, then `users`.
     (Explicit deletes, not FK cascade — four of these tables have no ORM cascade
     relationship, and SQLite FK enforcement is not guaranteed on.)
  2. removes the whole `{data_dir}/users/{user_id}/` tree from disk;
  3. ends the session; lands on a simple "your account and data have been deleted"
     page (unauthenticated).
- Guards: the default/dev account (`is_default`) refuses deletion. Admins may delete
  themselves (admin status is env-based; recreating the account restores it).
- Future (when subscribers exist): grace-period variant = deactivate immediately, a
  scheduled purge calls the **same** `delete_account()` later. No rework required now.

## 4. Friendly empty states (no raw JSON)

The five data routes that currently re-raise pipeline `ValueError` as
`HTTPException(404)` → raw JSON (`/calendar`, `/pay`, `/compare`, `/discrepancies`,
`/day/{date}` — `app/main.py`) instead render a shared HTML empty-state template in
the normal site layout (404 status, HTML body). Two flavors keyed off the error:

- **Month not loaded:** "The August 2026 schedule documents haven't been published to
  the site yet — check back soon, or upload your own copies via Documents." (With
  central docs the wording no longer commands every pilot to upload.)
- **Pilot code not found in FA:** "We couldn't find pilot code XYZ on the August 2026
  Final Award — check your pilot code in Settings." (Currently a raw
  `Pilot XYZ not found in …` 404. This is the silent-failure mode the code assist in
  §2 also targets.)

Month navigation (prev/next links) stays present on these pages so a pilot can reach
months that do exist.

## Error handling summary

- Admin upload of an unparseable PDF → rejected at upload with the parse error.
- Feed URL that isn't http(s) / doesn't fetch / isn't a VCALENDAR → onboarding/settings
  form error; pilot can skip.
- Duplicate pilot code across FA slots → later slot wins (deterministic; not expected).
- Delete with wrong password or confirmation text → form error, nothing deleted.
- Data pages with missing docs / unmatched code → HTML empty states (§4).

## Testing

- `SharedDocumentsStore` round-trip (save/get/list/delete; multi-slot FA).
- Resolution precedence: personal beats shared per kind; FA multi-file merge produces
  the union of pilot codes; `available_months` union.
- Admin gate: non-admin and anonymous get 404 on all `/admin/*` routes; admin (via
  `ADMIN_EMAILS`) gets pages; parse-on-upload rejects a garbage PDF.
- Document view route: streams the resolved file; 404 when nothing resolves;
  personal-first.
- Onboarding step 2: URL saves `feed_url` + `feed_auto_update`; immediate fetch runs
  (mocked); bad URL re-renders with error; skip works.
- Code lookup: by code (hit + miss) and by last name; disappears with no shared FA.
- `delete_account`: seed a user with rows in **every** user-keyed table plus files on
  disk; delete; assert every table has zero rows for that user, the directory is gone,
  other users' data untouched; default user refuses.
- Empty states: the five routes return HTML (not JSON) for both flavors.

## Deploy notes

1. Merge to main via PR (normal workflow), pull + rebuild on the box.
2. Add `ADMIN_EMAILS=dennfish@gmail.com` to `deploy/.env.prod` (env change → container
   restart required; not in the repo, same as `FEED_UPDATER_ENABLED`).
3. `create_all` creates `shared_documents` automatically on first boot.
4. Author uploads the August FA (FO + CA) and Trip Pairing Packet via
   `/admin/documents` — the two PDFs currently sitting untracked in the repo's `docs/`
   folder are these; they should be uploaded through the app, not committed to git.
5. Re-test the outside pilot's signup path end-to-end from a phone.
