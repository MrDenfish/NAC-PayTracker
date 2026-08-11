# Company-Assigned PCH as a §3.E.1.b Candidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The company's reassignment-notice PCH becomes one more §3.E.1.b candidate — `credited = max(company, recompute)` instead of company-replaces-recompute — and the two day-page cards show the full comparison with the winner marked.

**Architecture:** One `max()` at each of the two `credited = override if override is not None else new_pch` sites in `apply_actuals.py`. Display: a "Company-assigned" candidate row in `_build_day_detail`'s existing `raw` candidates list; the reassignment card's prose becomes a small comparison table with two navigation links into the existing amend form. No schema change, no new selection rules.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, pytest (`-n auto`, `--timeout=60`), Decimal throughout.

**Spec:** `docs/superpowers/specs/2026-08-11-company-pch-greatest-of-design.md` (approved by owner 2026-08-11). Branch `feat/company-pch-greatest-of` (exists, carries the spec).

## Global Constraints

- **The change can only credit MORE than today, never less.** `credited = max(override, new_pch)` where today it is `override`; `effective = max(published, credited)` unchanged. Any path that could lower a day is a defect.
- **No-override safety property:** on a day with no company value entered, every path — pay AND rendering — must be byte-identical to current `main`. Same standard as PR #77.
- All values `Decimal`, never float. ANC-local dates via `timeutil`, never UTC `.date()`.
- **Mutation evidence is mandatory for every behaviour** (project rule after PR #77): show each test failing when the change is reverted, paste both outputs. Assert on CREDITED values (`effective_pch`, `pr.updated_month`) or on RENDERED HTML — never on hand-built expectations for form/route behaviour.
- Capture pytest's own exit code — never a piped one. `export NAC_PAY_DATA_DIR=$(mktemp -d)` before every run. `.venv/bin/python -m pytest`.
- Do NOT touch: `duty_overrides` / `_build_duty_overrides` / `_resolve_duty_override_key`, `_fold_candidates`, the `_day_duty_window` tiers, the 16h ceiling, `feed_reassignment_decisions` schema, or the REJECTED-status flows.
- The links added to the reassignment card are NAVIGATION into the one amend form (`?duty=1`, `?amend=1`) — never a second write path.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/nac_pay/schedule/apply_actuals.py` | The two `credited =` fold sites (reroute `:463`, off-day pickup `:377`). | Modify |
| `src/nac_pay/app/services.py` | `_build_day_detail` candidates list (`:2527-2572`); needs the day's CONFIRMED `FeedReassignment` in scope. | Modify |
| `src/nac_pay/app/templates/day.html` | Reassignment card comparison + links; Company PCH field hint. | Modify |
| `tests/schedule/test_apply_actuals.py` | Engine tests. | Modify |
| `tests/app/test_day_detail.py` | Candidates-card tests. | Modify |
| `tests/app/test_feed_cancellation.py` or `tests/app/test_reassign.py` | Card rendering tests (whichever already covers the reassignment card — grep `feed_reassignment` in tests/ and follow the existing home). | Modify |
| `docs/SYSTEM_CONTEXT.md` | Changelog row. | Modify |

---

### Task 1: Engine — `credited = max(override, new_pch)`

**Files:**
- Modify: `src/nac_pay/schedule/apply_actuals.py:377` (off-day pickup) and `:463` (reroute)
- Test: `tests/schedule/test_apply_actuals.py`

**Interfaces:**
- Consumes: existing `apply_actuals_to_month(..., feed_reassignment_pch_overrides=...)`.
- Produces: unchanged signatures; only the fold arithmetic changes. `FeedReassignment.override_pch` and `.new_pch` keep their current meanings (entered value / recompute) — Task 2 relies on both being present unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/schedule/test_apply_actuals.py`. Use the file's existing fixture helpers (`_empty_month`, `_trip_pairing`, `_leg`, `ReconciledTrip`, `ReconciliationResult` — all already imported there). The reroute path needs a baseline Trip whose date carries an UNMATCHED reconciled trip; follow the existing reroute tests in the file (grep `REASSIGN_CONFIRMED` for the established shape, including the `feed_reassignment_decisions` / `feed_reassignment_pch_overrides` dict keys `(date_iso, signature)`).

```python
def test_company_pch_below_the_recompute_credits_the_recompute():
    """The company's reassignment-notice value is one more §3.E.1.b
    candidate, not a replacement: company assigns 4.80, actual times
    recompute to 5.05 -> credited 5.05. (Old behaviour: 4.80 — the
    recompute was silently discarded.) Owner contract 2026-08-11:
    pay MAX(original, company-assigned, recompute)."""
    # Build: baseline trip published 4.50 on 2026-06-12; unmatched
    # reconciled trip whose actual block recomputes to 5.05; decision
    # CONFIRMED; override 4.80.
    ...
    assert fr.effective_pch == Decimal("5.05")


def test_company_pch_above_the_recompute_still_credits_the_company_value():
    """Today's behaviour, still correct: company 5.17 > recompute 5.05
    -> credited 5.17. Pins that the max() did not overshoot."""
    ...
    assert fr.effective_pch == Decimal("5.17")


def test_no_company_value_is_byte_identical():
    """No override entered -> credited is exactly the recompute, as today."""
    ...
```

Fill the `...` from the existing reroute-test shape in the file — the plan deliberately does not restate ~40 lines of established fixture code; the implementer copies the nearest existing CONFIRMED-reroute test and changes the override value and assertion. The assertions above are contractual.

Also assert in the first test that the resulting `AssignmentVersion.pch_value` (the row folded onto the trip) is `Decimal("5.05")`, not 4.80 — the version label may still say "company PCH", but the credited number must be the max.

- [ ] **Step 2: Run to verify the first test FAILS** (credited comes back 4.80): `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/schedule/test_apply_actuals.py -q -k company_pch`. Paste the failure.

- [ ] **Step 3: Implement.** At both sites, replace

```python
        credited = override if override is not None else new_pch
```

with

```python
        # §3.E.1.b: the company's reassignment-notice PCH is one more
        # candidate, not a replacement — the pilot keeps the recompute
        # when actual times beat the notice (owner contract 2026-08-11).
        credited = max(override, new_pch) if override is not None else new_pch
```

Check whether the off-day-pickup site (`:377`) can even receive an override (grep how `pch_overrides` keys are written for pickups); if it can, apply the same max there; if it cannot, say so in the report and leave it — do not add dead arithmetic.

- [ ] **Step 4: Run to verify all pass**, then run all of `tests/schedule/` — every pre-existing test must still pass (`duty_overrides`-era tests included).

- [ ] **Step 5: Mutation check** — revert the max, show the first test failing, restore, show green. Paste both.

- [ ] **Step 6: Commit** — `feat(engine): company-assigned PCH folds as a 3.E.1.b candidate`.

---

### Task 2: "Day PCH — how it's credited" card gains the company row

**Files:**
- Modify: `src/nac_pay/app/services.py` `_build_day_detail` (`raw` list, `:2527-2572`) and its call site in `load_day` (thread the day's CONFIRMED `FeedReassignment` in — `pr.feed_reassignments` is already on `PipelineResult`, `services.py:288/721`)
- Test: `tests/app/test_day_detail.py`

**Interfaces:**
- Consumes: Task 1's semantics (`fr.override_pch` present ⇒ it was folded via max).
- Produces: a candidate labelled exactly `"Company-assigned (reassignment notice)"` when the viewed date has a CONFIRMED `FeedReassignment` with `override_pch is not None`.

- [ ] **Step 1: Failing test** — end-to-end through `load_day` against the real June fixture is not possible (no stored override in bundled data), so follow the established pattern for decision-backed tests: write the decision row + override via the same store the route uses (grep `feed_reassignment` in `tests/app/` for the existing helper), `_pipeline.cache_clear()`, then:

```python
def test_candidates_card_includes_the_company_assigned_row():
    """Aug 10 class of defect: with a company PCH entered the card listed
    three candidates, marked NO winner, and asserted a fourth number that
    appeared nowhere. The company value must be a row, and exactly one
    row must be marked winning."""
    ...
    d = load_day(...)
    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" in labels
    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].pch == d.effective_pch


def test_candidates_card_without_a_company_value_is_unchanged():
    """No-override safety: the row is absent and rendering is as before."""
    ...
    assert "Company-assigned (reassignment notice)" not in [c.label for c in d.pch_candidates]
```

- [ ] **Step 2: Verify FAIL** (row absent; today no winner is marked when the override wins).

- [ ] **Step 3: Implement.** In `_build_day_detail`, insert into `raw` — after the published entry, before flight-op — when the day's `FeedReassignment` (CONFIRMED, matching the viewed date) carries `override_pch`:

```python
        if fr_for_day is not None and fr_for_day.override_pch is not None:
            raw.append((
                "Company-assigned (reassignment notice)",
                fr_for_day.override_pch,
            ))
```

`fr_for_day` is resolved in `load_day` (`next((fr for fr in pr.feed_reassignments if fr.date == target and fr.status == REASSIGN_CONFIRMED), None)`) and passed to `_build_day_detail` as one new keyword parameter. Import `REASSIGN_CONFIRMED` from `nac_pay.schedule.apply_actuals` if not already imported.

- [ ] **Step 4: Green; run all of `tests/app/test_day_detail.py`.**
- [ ] **Step 5: Mutation** — remove the `raw.append`, first test fails; restore.
- [ ] **Step 6: Commit** — `feat(day): company-assigned PCH is a visible credited candidate`.

---

### Task 3: Reassignment card — comparison + two links + honest hint

**Files:**
- Modify: `src/nac_pay/app/templates/day.html:81-140` (the `feed_reassignment` card) and the Company PCH input hint in the same card
- Test: wherever the reassignment card is already tested (grep `Company reassignment detected` in `tests/` — follow that file)

**Interfaces:**
- Consumes: `fr.original_pch`, `fr.override_pch`, `fr.new_pch`, `fr.effective_pch` (all existing); `data.editable` (existing); the `?duty=1` / `?amend=1` entry points (existing, PR #78 / pre-existing).
- Produces: template only.

- [ ] **Step 1: Failing tests** — render the day via the client fixture the existing card tests use; assert against the HTML:

```python
def test_reassignment_card_shows_the_comparison_rows(client):
    r = client.get("/day/<date-with-confirmed-override>")
    # All three candidates visible with their values:
    assert "Original" in r.text and "4.50" in r.text
    assert "Company-assigned" in r.text and "5.17" in r.text
    assert "Recomputed from actual times" in r.text and "5.05" in r.text


def test_reassignment_card_links_into_the_amend_form(client):
    r = client.get("/day/<same-date>")
    assert "?duty=1#reassign-form" in r.text
    assert "?amend=1#reassign-form" in r.text
```

Build the fixture state the same way Task 2 does (decision + override via the store).

- [ ] **Step 2: Verify FAIL.**

- [ ] **Step 3: Implement.** Replace the CONFIRMED-branch prose paragraph (the `Paying the greater (§3.E.1.b): ...` block at `day.html:124-135`) with an `option-table` (the same class the credited card uses at `:526`) of the present candidates — Original published, Company-assigned (when `fr.override_pch is not none`), Recomputed from actual times (with a parenthetical `max of block and duty-rig`) — marking the row equal to `fr.effective_pch` with the existing `winning` class. Below it, under `{% if data.editable %}`, two `correct-link` anchors: `⏱ Correct duty times` → `/day/{{ data.date_iso }}?duty=1#reassign-form` and `Amend this trip` → `/day/{{ data.date_iso }}?amend=1#reassign-form`.

Change the Company PCH field's label/hint (both the REJECTED-branch form at `:103` and the CONFIRMED/PROPOSED one — grep `reassign-pch-input` for all instances) to:

```html
<label class="form-label" for="...">Company-assigned PCH (from the reassignment notice)</label>
...
<span class="form-hint">
  The value the company's notice assigns this trip. Counts as the
  reassigned trip's published value — §3.E.1.b pays the greatest of
  this, your original trip, and the recompute from actual times.
</span>
```

- [ ] **Step 4: Green; run the containing test file.**
- [ ] **Step 5: Mutation** — drop the company row from the table, first test fails; restore.
- [ ] **Step 6: Commit** — `feat(day): reassignment card shows the 3.E.1.b comparison and links into amend`.

---

### Task 4: Gate, prod impact, changelog

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md` (changelog row below the `|------|--------|` separator)

- [ ] **Step 1: Full suite** — `run_in_background`, then `PYTEST_RC` + `grep -c "FAILED\|ERROR"`; both must be 0.
- [ ] **Step 2: Prod impact, measured** — re-run the credited-PCH probe (the `probe_pch.py` shape from PR #77: every trip/day `effective_pch` for all three users) against the current image, save; the expected post-deploy diff is **empty** (measured 2026-08-11: only two overrides exist, both already the greatest). Additionally assert the two override days directly: 2026-07-06 and 2026-08-10 both stay 5.17.
- [ ] **Step 3: Changelog row** — cover: the contract (owner 2026-08-11, MAX(original, company-assigned, recompute)); that the old code silently discarded the recompute when a company value was entered; zero measured movement today and why; the two card changes and the two links; the field relabel.
- [ ] **Step 4: Commit; push; PR** — body must include the contract statement, the worked Aug 10 example (`max(4.50, 5.17, 5.05, 4.60) = 5.17`), the measured zero-impact statement, and the mutation-evidence summary. Do not merge or deploy without the owner's explicit go-ahead.

---

## Out of scope for this plan

Duty inputs on the reassignment card (rejected in the spec); REJECTED-flow changes; the multi-day pairing key mismatch (parked, open-items #14).
