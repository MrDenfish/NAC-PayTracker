# Company-Assigned PCH as a §3.E.1.b Candidate + Reassignment-Card Transparency — Design

**Date:** 2026-08-11 · **Approved by:** author ("Yes, that's correct. The company will pay MAX(original pch, reassigned pch, reassigned actual, actual duty rig)") · **Follows:** PR #77 (duty corrections), PR #78 (Times-card duty-correction affordance).

## Domain facts (from the pilot, 2026-08-11)

- When the company reassigns a trip mid-month, it **sends the pilot a notice with the new trip's PCH value**. Almost every manually entered PCH on the reassignment card is that company-assigned value — it is an **authoritative document value**, the reassigned trip's *published* PCH, not a pilot override.
- The company pays **`MAX(original published, company-assigned, recompute-from-actuals)`**, where the recompute itself is `max(flight-op block, duty rig, trip rig, cumulative DPG) + deadhead` — so the pilot's four-candidate framing (original, reassigned, reassigned actual, actual duty rig) is exactly the engine's existing `components_from_times(...).trip_pch` plus the two published values. Worked example, Aug 10 2026: `max(4.50, 5.17, 5.05, 4.60) = 5.17`.
- **Consequence accepted by the author:** if the company ever assigns a value *lower* than the actual-times recompute, the app credits the recompute — it will sometimes pay more than the company's notice says, and that is correct behaviour, not a bug.

## The defects being fixed

1. **Pay (latent):** `apply_actuals` computes `credited = override if override is not None else new_pch` — the company value *replaces* the recompute. Company assigns 4.80, actuals then recompute to 5.05 → credited 4.80 today, 5.05 under the contract. **Measured on live data: zero pay movement** — only two entered values exist (both dennfish, both 5.17, both already the greatest). This changes future days only.
2. **Display (live on Aug 10):** the "Day PCH — how it's credited" card lists Published 4.50 / Flight-op 5.05 / Duty-rig 4.60, marks **no** winner, and asserts Effective 5.17 — a number appearing nowhere in the table. Same pathology the PR #77 review caught in the history badge, un-caught here.
3. **Usability (the author's original report):** the reassignment card asserts a recomputed conclusion without showing its inputs, and the duty-correction capability is not reachable from it.

## Design

### 1. Engine — one line, §3.E.1.b applied uniformly

In `apply_actuals` (the reroute branch and the off-day-pickup branch if it accepts an override):

```
credited = max(override, new_pch) if override is not None else new_pch
```

`effective = max(baseline published, credited)` stays as is, so the full fold is `max(original, company, recompute)`. `FeedReassignment` keeps `override_pch` and `new_pch` separately (it already does), so the display can always show both.

**Invariant preserved:** nothing lowers. The change can only credit more than today, never less. A duty correction still flows into `new_pch` via `duty_overrides` — and under greatest-of it composes correctly with a company value instead of being silenced by it.

### 2. "Day PCH — how it's credited" card — the missing row

On a day with a CONFIRMED reassignment carrying a company value, add the candidate **"Company-assigned (reassignment notice)"**. The existing is-winning marking then works unchanged, and the footer's "Effective PCH = greatest" becomes true. The four candidates the author enumerated all render: Published, Company-assigned, Flight-op (actual block), Duty-rig (actual).

### 3. Reassignment card — show the comparison, not just the conclusion

Replace the single-figure prose ("recomputed 5.05 → 5.17 PCH") with a small comparison consistent with the option-table style: original published, company-assigned (when entered), recomputed from actuals — winner marked. The recompute row notes it is `max` of block and duty-rig so a wrong duty is visible at the point of decision.

Add a **"Correct duty times"** link (`/day/<date>?duty=1#reassign-form` — the PR #78 entry point) next to the recompute row, shown under the same `data.editable` gate PR #78 uses.

### 4. Company PCH field — say what it is

Relabel/hint: "Company-assigned PCH — from the company's reassignment notice. Counts as the reassigned trip's published value; §3.E.1.b pays the greatest of this, your original trip, and the recompute from actual times." This replaces the current silence about precedence, and it replaces the now-false implication that entering a value discards the recompute.

## Out of scope

- Duty on/off inputs on the reassignment card — rejected. Two forms writing one stored field produced three separate silent bugs during PR #77; the link into the single duty-correction form is the whole design.
- Any change to `duty_overrides`, `_fold_candidates`, the `_day_duty_window` tiers, or the DUTY_CORRECTION selection rules.
- REJECTED-status reassignment flows (the Apply-instead form) — unchanged.
- The `pch_value` stored on `feed_reassignment_decisions` — schema untouched; only the crediting fold changes.

## Testing

- **Engine:** company > recompute → company credited (today's behaviour, still right); company < recompute → **recompute credited** (the change — this is the test that fails today); no company value → unchanged; the §3.E floor (never below original published) in all cases. Mutation evidence per the project rule: each test shown failing without the change.
- **Card:** with a company value entered, exactly one winner marked and the company row present; without one, card unchanged (byte-identical rendering — the no-override safety property, same standard as PR #77).
- **Reassignment card:** comparison renders all present candidates; the duty-correction link honours the `editable` gate.
- **Prod verify:** re-run the credited-PCH probe across all three accounts before and after deploy — expected diff: **empty** (measured: both existing overrides are already the greatest). Aug 10 card shows `Company-assigned 5.17 ← credited`.
