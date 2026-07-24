# Reason Tags + Calendar Color Coding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the full reason-code list as status tags on calendar cells and the day-page Assignment card (fixing the invisible July 2/3 SICK), and add priority-ordered cell colors: green for effective premium multiplier > 1.0×, yellow for protected-absence reasons.

**Architecture:** All logic lives in `services.py` (a shared `_REASON_TAGS` map + `_status_duty_class()` helper) feeding the existing `duty_label`/`duty_class` channels that both `calendar.html` and `day.html` already render — no template changes. Two new CSS tints. Spec: `docs/superpowers/specs/2026-07-24-reason-tags-and-calendar-colors-design.md`.

**Tech Stack:** Python 3.11, pytest, plain CSS.

## Global Constraints

- Branch `feat/reason-tags-calendar-colors`; PR → merge → deploy authorized end-to-end this run (author approval in-session 2026-07-24); push notifications at milestones.
- Verify with pytest's OWN exit code; full suite via `run_in_background`; single files foreground with `-o addopts=""`.
- Tag priority: DROPPED > CANCELLED > reason tag > FLT/duty label (CALLOUT beats reason on day cells). Color priority: premium green > absence yellow > dropped/cancelled white > existing tints.
- Absence set: SICK, PTO, JURY, BEREAVEMENT, MOVING, FAR, MILITARY, FMLA. TRAINING keeps `training` tint; UNPAID_LOA renders `off`; LESSER_TRADE / UNPROTECTED_UNAVAIL get tags but keep base color.
- Green = `premium_multiplier(category, custom_multiplier) > 1` (CUSTOM included; OPEN_TIME_BID_PERIOD 1.0× excluded).

---

### Task 1: Shared helpers + calendar (`_build_cell`) + CSS

**Files:**
- Modify: `src/nac_pay/app/services.py` — add `_REASON_TAGS` + `_ABSENCE_REASON_VALUES` + `_status_duty_class()` near `_REASON_LABELS` (~line 1091); rework `_build_cell` trip branch (~2870) and day branch (~2895)
- Modify: `src/nac_pay/app/static/styles.css` — two tints after `.duty-bg--void` (~line 433); overlay suppression after `.day-cell--user-reassigned::before` (~line 452)
- Test: `tests/app/test_calendar.py`

**Interfaces:**
- Produces: `_REASON_TAGS: dict[str, str]` (keyed by ReasonCode string value; FLOWN/OFF/VOLUNTARY_DROP absent), `_status_duty_class(reason_value: str, multiplier: Decimal | None, base_class: str) -> str`. Task 2 consumes both.

- [ ] **Step 1: Failing app tests** (module-level default-user `client` pattern from `tests/app/test_day_edit.py`; each test posts its own override — per-test DB reset keeps them hermetic). June FA facts (pilot DFI): Jun 2 trip `722/750`, Jun 5 trip `722/750`, Jun 6 trip `722/754`, Jun 12 trip `768`.

```python
def test_calendar_shows_reason_tag_and_absence_color():
    client.post("/day/2026-06-02", data={
        "reason_code": "SICK", "premium_category": "NONE",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    assert ">SICK<" in body
    assert "duty-bg--absence" in body


def test_calendar_premium_day_is_green():
    client.post("/day/2026-06-05", data={
        "reason_code": "FLOWN", "premium_category": "OVERTIME",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    body = client.get("/calendar?ym=2026-6").text
    assert "duty-bg--premium" in body


def test_calendar_premium_beats_absence_color_but_keeps_reason_tag():
    client.post("/day/2026-06-06", data={
        "reason_code": "SICK", "premium_category": "JUNIOR_ASSIGNMENT_1ST",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    body = client.get("/calendar?ym=2026-6").text
    assert "duty-bg--premium" in body
    assert ">SICK<" in body


def test_calendar_bid_period_open_time_not_green():
    client.post("/day/2026-06-02", data={
        "reason_code": "FLOWN", "premium_category": "OPEN_TIME_BID_PERIOD",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    body = client.get("/calendar?ym=2026-6").text
    assert "duty-bg--premium" not in body


def test_calendar_custom_multiplier_above_one_is_green():
    client.post("/day/2026-06-02", data={
        "reason_code": "FLOWN", "premium_category": "CUSTOM",
        "entry_mode": "SIMPLE", "custom_multiplier": "2.0"}, follow_redirects=False)
    body = client.get("/calendar?ym=2026-6").text
    assert "duty-bg--premium" in body


def test_calendar_flown_day_unchanged():
    body = client.get("/calendar?ym=2026-6").text
    assert ">FLT<" in body
    assert "duty-bg--absence" not in body
    assert "duty-bg--premium" not in body
```

- [ ] **Step 2: Run, expect the first five to FAIL** — `pytest tests/app/test_calendar.py -o addopts="" -q -k "reason_tag or green or beats or bid_period or custom_mult or flown_day"` (missing CSS class strings / SICK tag).

- [ ] **Step 3: Implement helpers** in `services.py` near `_REASON_LABELS`:

```python
# Short status tags for the calendar cell + day Assignment card. FLOWN and
# OFF keep the FLT / duty-type label; VOLUNTARY_DROP renders as DROPPED via
# its own branch. Keyed by ReasonCode string value (StrEnum-compatible).
_REASON_TAGS: dict[str, str] = {
    "PTO": "PTO", "SICK": "SICK", "JURY": "JURY",
    "BEREAVEMENT": "BEREAVEMENT", "TRAINING": "TRAINING",
    "MOVING": "MOVING", "FAR": "FAR", "MILITARY": "MILITARY",
    "FMLA": "FMLA", "UNPAID_LOA": "UNPAID LOA",
    "LESSER_TRADE": "LESSER TRADE", "UNPROTECTED_UNAVAIL": "UNPROT UNAVAIL",
}

# Protected-absence family → yellow cell tint (author's choice 2026-07-24:
# PTO included, TRAINING keeps its own violet tint, UNPAID_LOA reads as OFF).
_ABSENCE_REASON_VALUES = frozenset({
    "SICK", "PTO", "JURY", "BEREAVEMENT", "MOVING", "FAR", "MILITARY", "FMLA",
})


def _status_duty_class(
    reason_value: str, multiplier: Decimal | None, base_class: str,
) -> str:
    """Cell/card tint with the author's priority: premium green (> 1.0×)
    beats absence yellow beats the base duty tint. TRAINING keeps its own
    tint; UNPAID_LOA reads as a day off."""
    if multiplier is not None and multiplier > 1:
        return "premium"
    if reason_value in _ABSENCE_REASON_VALUES:
        return "absence"
    if reason_value == "TRAINING":
        return "training"
    if reason_value == "UNPAID_LOA":
        return "off"
    return base_class
```

- [ ] **Step 4: Rework `_build_cell`.** Trip branch — replace the label/class expressions:

```python
        cancelled = trip.cancelled_pay_protected
        reason_value = trip.reason_code.value
        if cancelled:
            cell_label, cell_class = "CANCELLED", "off"
        else:
            cell_label = _REASON_TAGS.get(reason_value, "FLT")
            cell_class = _status_duty_class(reason_value, mult, "flt")
```
and use `duty_label=cell_label, duty_class=cell_class` in the CalendarCell. Day branch — after `mult` is computed:

```python
        reason_value = day.reason_code.value
        if not is_callout:
            tag = _REASON_TAGS.get(reason_value)
            if tag is not None:
                display_label = tag
            display_class = _status_duty_class(reason_value, mult, display_class)
        elif mult is not None and mult > 1:
            display_class = "premium"
```

- [ ] **Step 5: CSS.** After `.duty-bg--void`:

```css
.duty-bg--premium    { background: #d5f2d9; }   /* green — pays above 1.0× */
.duty-bg--absence    { background: #ffedb0; }   /* yellow — protected absence */
```
After `.day-cell--user-reassigned::before` rule:

```css
/* On premium/absence-tinted cells keep the indigo rail but drop the
   translucent overlay so the fill reads as one color (priority order). */
.day-cell--user-reassigned.duty-bg--premium::before,
.day-cell--user-reassigned.duty-bg--absence::before {
  background: transparent;
}
```

- [ ] **Step 6: Run** `pytest tests/app/test_calendar.py -o addopts="" -q` — all PASS.
- [ ] **Step 7: Commit** `feat: reason tags + premium/absence colors on calendar cells`.

---

### Task 2: Day-page Assignment card (`load_day`)

**Files:**
- Modify: `src/nac_pay/app/services.py` — after the CANCELLED override block (~line 1878: `if trip is not None and trip.cancelled_pay_protected and not is_dropped:`)
- Test: `tests/app/test_day_detail.py`

**Interfaces:** consumes `_REASON_TAGS` / `_status_duty_class` from Task 1; `reason_value` and `premium_multiplier` are already in scope at the insertion point.

- [ ] **Step 1: Failing tests** (same default-user client pattern as that file):

```python
def test_day_assignment_card_shows_reason_tag_and_color():
    client.post("/day/2026-06-02", data={
        "reason_code": "SICK", "premium_category": "NONE",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    body = client.get("/day/2026-06-02").text
    assert ">SICK</span>" in body or ">SICK<" in body
    assert "duty-bg--absence" in body


def test_day_assignment_card_premium_green_keeps_flt_tag():
    client.post("/day/2026-06-02", data={
        "reason_code": "FLOWN", "premium_category": "OVERTIME",
        "entry_mode": "SIMPLE", "custom_multiplier": ""}, follow_redirects=False)
    body = client.get("/day/2026-06-02").text
    assert "duty-bg--premium" in body
    assert ">FLT<" in body
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/app/test_day_detail.py -o addopts="" -q -k "reason_tag or premium_green"`.
- [ ] **Step 3: Implement.** Extend the cancelled block to an if/elif:

```python
    if trip is not None and trip.cancelled_pay_protected and not is_dropped:
        duty_label = "CANCELLED"
        duty_class = "off"
    elif not is_dropped and kind != "off" and duty_label != "CALLOUT":
        tag = _REASON_TAGS.get(reason_value)
        if tag is not None:
            duty_label = tag
        duty_class = _status_duty_class(reason_value, premium_multiplier, duty_class)
```

- [ ] **Step 4: Run** `pytest tests/app/test_day_detail.py tests/app/test_day_edit.py tests/app/test_calendar.py tests/app/test_reassign.py tests/app/test_drops.py -o addopts="" -q` — all PASS (drops/cancelled regressions covered by existing tests).
- [ ] **Step 5: Commit** `feat: reason tag + premium/absence color on day Assignment card`.

---

### Task 3: Full verify → PR → merge → deploy → prod check → docs

- [ ] **Step 1:** Full suite background: `.venv/bin/pytest -q > <scratch>/pytest_full2.out 2>&1; echo PYTEST_RC=$?` then grep FAILED|ERROR (expect RC=0, 0 matches).
- [ ] **Step 2:** Commit spec + plan docs; push; `gh pr create`; `gh pr merge --squash --delete-branch`; verify `git ls-remote origin main` matches local.
- [ ] **Step 3:** Deploy (`git pull --ff-only` + `docker compose up -d --build` on the box); `curl https://pch-ledger.com/api/health`.
- [ ] **Step 4:** Read-only prod probe: `load_calendar(2026, 7, user)` cells for Jul 2/3 → `duty_label == "SICK"`, `duty_class == "absence"`; Jul 23 (pickup, premium if set) sanity; Jul 24 still CANCELLED.
- [ ] **Step 5:** Changelog entry in `docs/SYSTEM_CONTEXT.md` via docs PR; update memory; push notification with the result.
