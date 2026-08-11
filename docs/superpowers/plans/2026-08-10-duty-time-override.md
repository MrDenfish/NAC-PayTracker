# Pilot-Overridable Duty On / Duty Off (PR A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the pilot override a day's Duty On and Duty Off clocks from the amend form, and have that override drive both the displayed duty window and the credited duty-rig PCH — in both directions.

**Architecture:** Two nullable `VARCHAR(5)` columns on `user_assignment_versions` store bare local `"HH:MM"` clocks; `duty_hours` is derived from them. A new `VersionType.DUTY_CORRECTION` marks such a version, and `_pipeline` collects those into a `duty_overrides: dict[date_iso, Decimal]` passed into `apply_actuals_to_month`, where it *substitutes* the duty input to the §3.E recompute rather than competing as another max() candidate. The day view gains an override tier above the PR #76 packet-show anchor.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, SQLAlchemy 2.0, pytest (`-n auto`, `--timeout=60`), Decimal throughout.

**Spec:** `docs/superpowers/specs/2026-08-10-duty-time-override-design.md`

## Global Constraints

- **New columns must be NULLABLE** and registered in `db._ADDED_COLUMNS`; `create_all` never ALTERs an existing table. Reads must handle NULL — existing rows have `duty_hours` and no clocks and must resolve exactly as they do today.
- **Never widen the engine's dependencies.** `nac_pay.engine` and `nac_pay.schedule` must not import `nac_pay.storage` at module level for this work; `_pipeline` resolves the override dict and passes it in as a parameter.
- **All money/time values are `Decimal`**, never float. Clocks are bare local `"HH:MM"` strings, matching `VersionLeg.out_local` and `TripPairing.sched_duty_on`.
- **Midnight rule (one definition, used everywhere):** `duty_off_local` is on the same local date as `duty_on_local` unless it is `<=` it, in which case it is the next day.
- **§3.E guarantee is structural:** `Trip.effective_pch = max(published, *versions)` stays untouched. A duty correction can lower a *recompute*, never take a day below `published`.
- **Test discipline:** capture pytest's own exit code plus a `FAILED|ERROR` grep — never a piped exit code. Run one small file in the foreground; anything larger goes `run_in_background`.
- **Before running any script that touches the DB:** `export NAC_PAY_DATA_DIR=$(mktemp -d)`. The default resolves to the author's live dev DB.
- Branch `feat/duty-time-override` already exists and carries the spec commits. Do not commit `docs/August 2026 Trip Pairing Packet.pdf` or `docs/Troubleshooting/`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/nac_pay/timeutil.py` | All clock/tz conversion. Gains pure clock arithmetic. | Modify |
| `src/nac_pay/storage/db_models.py` | ORM row for `user_assignment_versions`. | Modify (2 columns) |
| `src/nac_pay/storage/db.py` | `_ADDED_COLUMNS` migration registry. | Modify (1 entry) |
| `src/nac_pay/storage/assignment_versions.py` | `VersionType`, `UserAssignmentVersion`, store `save`/read. | Modify |
| `src/nac_pay/schedule/apply_actuals.py` | §3.E recompute from actuals. Duty input becomes overridable. | Modify |
| `src/nac_pay/app/services.py` | `_pipeline` wiring + `_day_duty_window` precedence. | Modify |
| `src/nac_pay/app/main.py` | `POST /day/<date>/reassign` handler. | Modify |
| `src/nac_pay/app/templates/day.html` | Amend-form clock inputs + `recompute()`. | Modify |

---

### Task 1: Clock arithmetic helper

**Files:**
- Modify: `src/nac_pay/timeutil.py` (append after `scheduled_report_utc`)
- Test: `tests/test_timeutil.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `duty_hours_between(duty_on_local: str, duty_off_local: str) -> Decimal | None` — returns duty duration in hours, `None` if either clock is unparsable. Used by Tasks 5, 6, 7.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_timeutil.py`:

```python
def test_duty_hours_between_same_day():
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    # Aug 8 2026: show 04:41, release 18:15 = 13:34 = 13.5667h
    got = duty_hours_between("04:41", "18:15")
    assert got is not None
    assert abs(got - Decimal("13.5667")) < Decimal("0.001")


def test_duty_hours_between_crosses_midnight():
    """Duty off at or before duty on means the next day, never negative."""
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    # 04:41 show, 01:30 release next morning = 20:49 = 20.8167h
    got = duty_hours_between("04:41", "01:30")
    assert got is not None
    assert abs(got - Decimal("20.8167")) < Decimal("0.001")


def test_duty_hours_between_equal_clocks_is_a_full_day():
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    assert duty_hours_between("04:41", "04:41") == Decimal("24")


def test_duty_hours_between_rejects_garbage():
    from nac_pay.timeutil import duty_hours_between

    assert duty_hours_between("", "18:15") is None
    assert duty_hours_between("04:41", "") is None
    assert duty_hours_between("25:00", "18:15") is None
    assert duty_hours_between("04:61", "18:15") is None
    assert duty_hours_between("0441", "18:15") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/test_timeutil.py -q -k duty_hours_between`
Expected: FAIL — `ImportError: cannot import name 'duty_hours_between'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/nac_pay/timeutil.py`:

```python
def _parse_clock(clock_hhmm: str) -> int | None:
    """Minutes past local midnight for a bare ``"HH:MM"``, else None."""
    try:
        hh, mm = (int(part) for part in clock_hhmm.split(":"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def duty_hours_between(
    duty_on_local: str, duty_off_local: str,
) -> Decimal | None:
    """Duty duration in hours between two bare local ``"HH:MM"`` clocks.

    Duty off is on the same local date as duty on unless it is ``<=`` it,
    in which case it is the next day — a trip releasing 01:30 after an
    04:41 show is 20.82h, never negative. Equal clocks mean a full 24h.

    Returns None if either clock is unparsable; callers fall back rather
    than crediting a guess.
    """
    on = _parse_clock(duty_on_local)
    off = _parse_clock(duty_off_local)
    if on is None or off is None:
        return None
    span = off - on
    if span <= 0:
        span += 24 * 60
    return Decimal(span) / Decimal("60")
```

Add `Decimal` to the module imports: change `from datetime import date, datetime, timedelta, timezone` block by adding a new line above it:

```python
from decimal import Decimal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/test_timeutil.py -q`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/timeutil.py tests/test_timeutil.py
git commit -m "feat(timeutil): duty_hours_between for bare local clocks"
```

---

### Task 2: Storage columns for the two clocks

**Files:**
- Modify: `src/nac_pay/storage/db_models.py:199` (after `duty_hours`)
- Modify: `src/nac_pay/storage/db.py:79` (`_ADDED_COLUMNS`)
- Modify: `src/nac_pay/storage/assignment_versions.py` (dataclass, `save`, both readers)
- Test: `tests/storage/test_assignment_versions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `UserAssignmentVersion.duty_on_local: str | None` and `.duty_off_local: str | None`; `UserAssignmentVersionStore.save(..., duty_on_local: str | None = None, duty_off_local: str | None = None)`. Used by Tasks 5, 6, 7.

- [ ] **Step 1: Write the failing test**

Append to `tests/storage/test_assignment_versions.py`:

```python
def test_save_and_read_duty_clocks():
    from decimal import Decimal

    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    store = UserAssignmentVersionStore(user_id="u_test_clocks")
    store.save(
        date_iso="2026-08-08",
        version_type=VersionType.REASSIGNMENT,
        assignment_id="720/1780",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("7.13"),
        duty_hours=Decimal("13.5667"),
        duty_on_local="04:41",
        duty_off_local="18:15",
    )

    got = store.list_for_month(2026, 8)["2026-08-08"][0]
    assert got.duty_on_local == "04:41"
    assert got.duty_off_local == "18:15"


def test_version_without_clocks_reads_as_none():
    """Rows predating the columns must keep resolving — NULL, not crash."""
    from decimal import Decimal

    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    store = UserAssignmentVersionStore(user_id="u_test_noclocks")
    store.save(
        date_iso="2026-08-09",
        version_type=VersionType.REASSIGNMENT,
        assignment_id="768",
        entry_mode=VersionEntryMode.SIMPLE,
        pch_value=Decimal("4.17"),
    )

    got = store.list_for_month(2026, 8)["2026-08-09"][0]
    assert got.duty_on_local is None
    assert got.duty_off_local is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/storage/test_assignment_versions.py -q -k duty_clocks`
Expected: FAIL — `TypeError: save() got an unexpected keyword argument 'duty_on_local'`

- [ ] **Step 3: Write minimal implementation**

In `src/nac_pay/storage/db_models.py`, immediately after the `duty_hours` column in `UserAssignmentVersionRow`:

```python
    # Pilot-overridden duty window, bare local "HH:MM" (nullable: rows
    # predating the columns carry duty_hours only). When present these are
    # the truth and duty_hours is derived from them — see
    # timeutil.duty_hours_between.
    duty_on_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    duty_off_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
```

In `src/nac_pay/storage/db.py`, extend `_ADDED_COLUMNS`:

```python
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "feed_reassignment_decisions": [("pch_value", "VARCHAR(16)")],
    "user_assignment_versions": [
        ("duty_on_local", "VARCHAR(5)"),
        ("duty_off_local", "VARCHAR(5)"),
    ],
}
```

In `src/nac_pay/storage/assignment_versions.py`, add to the `UserAssignmentVersion` dataclass immediately after `workdays: int | None`:

```python
    duty_on_local: str | None
    duty_off_local: str | None
```

Add two keyword parameters to `save`, after `workdays: int | None = None`:

```python
        duty_on_local: str | None = None,
        duty_off_local: str | None = None,
```

Set them on the row inside `save` wherever the other DETAILED inputs are assigned, and map them in **every** place a `UserAssignmentVersion` is constructed from a row (search the file for `workdays=row.workdays` and add alongside each occurrence):

```python
            duty_on_local=row.duty_on_local,
            duty_off_local=row.duty_off_local,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/storage/ -q`
Expected: PASS. If a construction site was missed you will see `TypeError: __init__() missing 2 required positional arguments`.

- [ ] **Step 5: Verify the migration against a simulated old-schema DB**

This is the step that catches the class of bug `_ensure_added_columns` exists for. Create `/tmp/mig_check.py`:

```python
import os, tempfile
os.environ["NAC_PAY_DATA_DIR"] = tempfile.mkdtemp()

from sqlalchemy import inspect, text
from nac_pay.storage.db import get_engine, _ensure_added_columns

eng = get_engine()
# Simulate the pre-migration schema.
with eng.begin() as c:
    c.execute(text("ALTER TABLE user_assignment_versions DROP COLUMN duty_on_local"))
    c.execute(text("ALTER TABLE user_assignment_versions DROP COLUMN duty_off_local"))
cols = {c["name"] for c in inspect(eng).get_columns("user_assignment_versions")}
assert "duty_on_local" not in cols, "setup failed"
print("old schema simulated OK")

_ensure_added_columns(eng)
cols = {c["name"] for c in inspect(eng).get_columns("user_assignment_versions")}
assert {"duty_on_local", "duty_off_local"} <= cols, cols
print("migration added both columns OK")

_ensure_added_columns(eng)   # idempotent
print("second run idempotent OK")
```

Run: `.venv/bin/python /tmp/mig_check.py`
Expected: three `OK` lines, no traceback.

- [ ] **Step 6: Commit**

```bash
git add src/nac_pay/storage/db_models.py src/nac_pay/storage/db.py \
        src/nac_pay/storage/assignment_versions.py \
        tests/storage/test_assignment_versions.py
git commit -m "feat(storage): nullable duty_on_local/duty_off_local on versions"
```

---

### Task 3: `VersionType.DUTY_CORRECTION`

**Files:**
- Modify: `src/nac_pay/storage/assignment_versions.py:29-50` (`VersionType`)
- Test: `tests/storage/test_assignment_versions.py`

**Interfaces:**
- Consumes: Task 2's columns.
- Produces: `VersionType.DUTY_CORRECTION` (value `"DUTY_CORRECTION"`). Used by Tasks 5 and 7.

- [ ] **Step 1: Write the failing test**

```python
def test_duty_correction_version_round_trips():
    """A duty correction is its own type so the history doesn't call a clock
    fix a 'Reassignment'."""
    from decimal import Decimal

    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    store = UserAssignmentVersionStore(user_id="u_test_dutycorr")
    store.save(
        date_iso="2026-08-08",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="720/1780",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("7.13"),
        duty_hours=Decimal("13.5667"),
        duty_on_local="04:41",
        duty_off_local="18:15",
    )

    got = store.list_for_month(2026, 8)["2026-08-08"][0]
    assert got.version_type is VersionType.DUTY_CORRECTION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/storage/test_assignment_versions.py -q -k duty_correction`
Expected: FAIL — `AttributeError: DUTY_CORRECTION`

- [ ] **Step 3: Write minimal implementation**

Add to `VersionType` after `DROP`:

```python
    # The pilot correcting the DUTY WINDOW itself (duty_on_local /
    # duty_off_local). Unlike every other type this is not a competing
    # max() candidate — it is an INPUT: apply_actuals substitutes its
    # duty hours for the feed-derived _actual_duty_hours when recomputing
    # the §3.E components, so a correction works DOWNWARD as well as up.
    # The §3.E guarantee still holds structurally: effective_pch is
    # max(published, recomputed), so this can never take a day below
    # published. See docs/superpowers/specs/2026-08-10-duty-time-override-design.md
    DUTY_CORRECTION = "DUTY_CORRECTION"
```

`version_type` is a `String(16)` column and `"DUTY_CORRECTION"` is 15 characters — it fits with nothing to spare. Do not lengthen the name.

- [ ] **Step 4: Run test to verify it passes**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/storage/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/storage/assignment_versions.py tests/storage/test_assignment_versions.py
git commit -m "feat(storage): add VersionType.DUTY_CORRECTION"
```

---

### Task 4: `apply_actuals` accepts a duty override

This is the task the whole feature turns on. Everything else is plumbing.

**Files:**
- Modify: `src/nac_pay/schedule/apply_actuals.py` (`_actual_duty_hours`, `_recomputed_actual_components`, `_extension_recompute`, `_recomputed_reroute_pch`, `apply_actuals_to_month`)
- Test: `tests/schedule/test_apply_actuals.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (takes plain `Decimal`s).
- Produces: `apply_actuals_to_month(..., duty_overrides: dict[str, Decimal] | None = None)` — keys are `date_iso` of the trip's **first local date**, values are duty hours. Used by Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/schedule/test_apply_actuals.py`:

```python
def test_duty_override_replaces_the_feed_derived_duty():
    from nac_pay.schedule.apply_actuals import _actual_duty_hours

    packet = _trip_pairing("720/721/1780/1781", "6.08")
    packet = replace(packet, sched_duty_on="04:41")
    start = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)
    rt = ReconciledTrip(
        flight_sequence="720/721/1780/1781", legs=(_leg("720", start, end),),
        packet_trip=packet, match_status=MatchStatus.MATCHED,
        first_dt_utc=start, last_dt_utc=end, actual_block_hours=D("7.13"),
    )

    # No override → the packet-anchored window (PR #76): 04:41 → 18:15.
    assert abs(_actual_duty_hours(rt) - D("13.5667")) < D("0.001")
    # Override → exactly what the pilot said, in this case SHORTER.
    got = _actual_duty_hours(rt, {"2026-08-08": D("11.00")})
    assert got == D("11.00")


def test_duty_override_can_lower_a_day_where_duty_rig_was_winning():
    """The silent no-op this feature exists to fix: with max()-only
    semantics a shorter duty was ignored. Published 4.17, feed duty rig
    5.51 wins; pilot corrects duty down to 10.02h → rig 5.01 → credited."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="9.77")

    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))
    no_override, _, _ = apply_actuals_to_month(baseline, reconciliation)
    lowered, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("10.02")},
    )

    assert no_override.trips[0].effective_pch > lowered.trips[0].effective_pch
    assert lowered.trips[0].effective_pch == D("5.01")


def test_duty_override_never_takes_a_day_below_published():
    """§3.E is structural — max(published, recomputed) still holds."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="9.77")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("0.50")},
    )

    assert updated.trips[0].effective_pch == D("4.17")


def test_duty_override_does_not_discard_block_credit():
    """The Aug 8 shape: block 7.13 wins. Shortening duty must NOT cost the
    flight-op credit the pilot never disputed."""
    baseline_trip = Trip(trip_id="720", published_pch=D("6.08"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip("720", actual_block="7.13", packet_pch="6.08",
                       packet_block="6.08", packet_duty="10.73")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("8.00")},
    )

    assert updated.trips[0].effective_pch == D("7.13")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/schedule/test_apply_actuals.py -q -k duty_override`
Expected: FAIL — `TypeError: _actual_duty_hours() takes 1 positional argument but 2 were given` and `apply_actuals_to_month() got an unexpected keyword argument 'duty_overrides'`

- [ ] **Step 3: Write the implementation**

Change `_actual_duty_hours` in `src/nac_pay/schedule/apply_actuals.py`:

```python
def _actual_duty_hours(
    rt: ReconciledTrip,
    duty_overrides: dict[str, Decimal] | None = None,
) -> Decimal:
    """The duty period used for §3.E, report→release.

    A pilot ``DUTY_CORRECTION`` for this trip's first local date replaces
    the computed window outright — it is an INPUT, not a competing max()
    candidate, so a correction works DOWNWARD as well as up. Otherwise:
    scheduled report (see ``_duty_start_utc``) → TRIP_END_PAD after the
    last leg in.
    """
    if duty_overrides:
        override = duty_overrides.get(_local_date(rt.first_dt_utc).isoformat())
        if override is not None:
            return override
    duty_start = _duty_start_utc(rt)
    duty_end = rt.last_dt_utc + timedelta(hours=float(TRIP_END_PAD_HOURS))
    seconds = int((duty_end - duty_start).total_seconds())
    return Decimal(seconds) / Decimal("3600")
```

Thread `duty_overrides` through every caller. Each gains the same optional parameter and forwards it:

- `_recomputed_actual_components(rt, duty_overrides=None)` → `duty_hours=_actual_duty_hours(rt, duty_overrides)`
- `_extension_recompute(...)` → forwards to `_recomputed_actual_components`
- `_recomputed_reroute_pch(rt, original_packet, duty_overrides=None)` → `duty = _actual_duty_hours(rt, duty_overrides)`
- `_apply_duty_extension(...)` → forwards; its event label calls `_actual_duty_hours(rt, duty_overrides)` so the logged text matches what was credited

Add the parameter to the public entry point:

```python
def apply_actuals_to_month(
    baseline: Month,
    reconciliation: ReconciliationResult,
    *,
    duty_extension_tolerance_hours: Decimal = _DUTY_EXTENSION_TOLERANCE_HOURS,
    block_extension_tolerance_hours: Decimal = _BLOCK_EXTENSION_TOLERANCE_HOURS,
    packet: dict | None = None,
    feed_reassignment_decisions: dict[tuple[str, str], str] | None = None,
    feed_reassignment_pch_overrides: dict[tuple[str, str], Decimal] | None = None,
    duty_overrides: dict[str, Decimal] | None = None,
) -> tuple[Month, tuple[AppliedEvent, ...], tuple[FeedReassignment, ...]]:
```

Document it in the docstring alongside `packet`:

```
    ``duty_overrides`` maps a trip's first local date (ISO) to the duty
    hours the pilot recorded in a DUTY_CORRECTION. Where present it
    REPLACES the feed-derived duty in the §3.E recompute, so a correction
    lowers as well as raises. It can never take a day below published —
    Trip.effective_pch is still max(published, recomputed).
```

Forward it at both internal call sites — the reroute branch (`_recomputed_reroute_pch(rt, None)` and `_recomputed_reroute_pch(rt, original_packet)`) and the duty-extension branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/schedule/ -q`
Expected: PASS, all of `tests/schedule/`. Every pre-existing test must still pass — `duty_overrides` defaults to `None`, which is the current behaviour exactly.

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/schedule/apply_actuals.py tests/schedule/test_apply_actuals.py
git commit -m "feat(schedule): duty_overrides replaces feed duty in the 3.E recompute"
```

---

### Task 5: Pipeline wiring

**Files:**
- Modify: `src/nac_pay/app/services.py:524-529` (the `apply_actuals_to_month` call) and the `user_versions` block below it
- Test: `tests/app/test_day_edit.py`

**Interfaces:**
- Consumes: Task 1 `duty_hours_between`, Task 2 clock fields, Task 3 `DUTY_CORRECTION`, Task 4 `duty_overrides`.
- Produces: nothing new — wires existing pieces.

**Ordering constraint:** `user_versions` is currently loaded *after* `apply_actuals_to_month`. The override dict must be built *before* the call, so the store read moves up. Do not move the fold-onto-trips logic, only the read.

- [ ] **Step 1: Write the failing test**

Append to `tests/app/test_day_edit.py`:

```python
def test_duty_correction_flows_into_the_pipeline_recompute():
    """A stored DUTY_CORRECTION changes the day's credited duty rig."""
    from decimal import Decimal

    from nac_pay.app.services import _pipeline, load_day
    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    before = load_day(2026, 6, 12)
    assert before.duty_hours is not None

    UserAssignmentVersionStore(user_id="default").save(
        date_iso="2026-06-12",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="768",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("4.17"),
        duty_hours=Decimal("20.00"),
        duty_on_local="03:00",
        duty_off_local="23:00",
    )
    _pipeline.cache_clear()

    after = load_day(2026, 6, 12)
    assert after.duty_hours == Decimal("20.00")
    assert after.duty_rig_pch == Decimal("10.00")
```

Note: the bundled dev user id is `DEFAULT_USER_ID`; import it from `nac_pay.storage` rather than hardcoding if `"default"` does not match.

- [ ] **Step 2: Run test to verify it fails**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/test_day_edit.py -q -k duty_correction_flows`
Expected: FAIL — `after.duty_hours` is still the feed-derived value, not 20.00.

- [ ] **Step 3: Write the implementation**

In `src/nac_pay/app/services.py`, move the store read above the `apply_actuals_to_month` call and build the dict:

```python
    # Pilot duty corrections are an INPUT to the §3.E recompute, so they
    # must be resolved BEFORE apply_actuals runs (unlike the other user
    # versions, which fold onto trips afterwards). Latest active
    # correction per date wins.
    user_versions = UserAssignmentVersionStore(user_id=user_id).list_for_month(year, month)
    duty_overrides: dict[str, Decimal] = {}
    for date_iso, vlist in user_versions.items():
        active, _superseded = active_versions(vlist)
        for v in sorted(active, key=lambda x: x.seq):
            if v.version_type is not VersionType.DUTY_CORRECTION:
                continue
            hours = None
            if v.duty_on_local and v.duty_off_local:
                hours = duty_hours_between(v.duty_on_local, v.duty_off_local)
            if hours is None:
                hours = v.duty_hours
            if hours is not None:
                duty_overrides[date_iso] = hours
```

Pass it in:

```python
        updated, applied, feed_reassignments = apply_actuals_to_month(
            baseline, reconciliation,
            packet=packet,
            feed_reassignment_decisions=feed_decisions,
            feed_reassignment_pch_overrides=feed_pch_overrides,
            duty_overrides=duty_overrides,
        )
```

Delete the now-duplicate `user_versions = UserAssignmentVersionStore(...)` line further down; the variable is already in scope.

Add imports at the top of `services.py`: `duty_hours_between` from `nac_pay.timeutil`, and `VersionType` / `active_versions` from `nac_pay.storage` if not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/services.py tests/app/test_day_edit.py
git commit -m "feat(services): resolve duty corrections into the pipeline recompute"
```

---

### Task 6: Day-view precedence

**Files:**
- Modify: `src/nac_pay/app/services.py` (`_day_duty_window`, and the `load_day` call site)
- Test: `tests/app/test_day_detail.py`

**Interfaces:**
- Consumes: Task 1, Task 2.
- Produces: `_day_duty_window(first_out_utc, last_in_utc, sched_duty_on, override=None)` where `override` is `tuple[str, str] | None` of `(duty_on_local, duty_off_local)`.

- [ ] **Step 1: Write the failing test**

```python
def test_day_duty_window_prefers_the_pilot_override():
    """Tier 1 beats the packet show time (tier 2) and the actual-out
    fallback (tier 3)."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(first_out, last_in, "04:41", ("05:15", "17:45"))

    assert w.duty_on == "05:15"
    assert w.duty_off == "17:45"
    assert abs(w.duty_hours - Decimal("12.50")) < Decimal("0.001")
    assert w.duty_rig_pch == w.duty_hours / Decimal("2")


def test_day_duty_window_ignores_a_half_filled_override():
    """One clock without the other is not a window — fall back to tier 2."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(first_out, last_in, "04:41", ("05:15", ""))

    assert w.duty_on == "04:41"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/test_day_detail.py -q -k duty_window_prefers`
Expected: FAIL — `_day_duty_window() takes 3 positional arguments but 4 were given`

- [ ] **Step 3: Write the implementation**

Change the signature and add the tier-1 branch at the top of `_day_duty_window`:

```python
def _day_duty_window(
    first_out_utc: datetime_t,
    last_in_utc: datetime_t,
    sched_duty_on: str | None,
    override: tuple[str, str] | None = None,
) -> _DutyWindow:
```

After the existing imports inside the function, before `duty_end` is computed:

```python
    # Tier 1 — the pilot's own clocks. Both or neither: a half-filled
    # override is not a duty window, so fall through rather than guess.
    if override and override[0] and override[1]:
        from nac_pay.timeutil import duty_hours_between
        hours = duty_hours_between(override[0], override[1])
        if hours is not None:
            return _DutyWindow(
                duty_on=override[0],
                duty_off=override[1],
                duty_hours=hours,
                duty_rig_pch=hours / Decimal("2"),
            )
```

Extend the docstring's tier list to name the override as tier 1.

At the `load_day` call site, resolve the winning version's clocks and pass them.

`load_day` has no active-version list in scope at that point — you must load one. Task 5 already put `duty_overrides` on the `PipelineResult` path, but that dict holds *hours*, not clocks, so it can't render `"04:41"`. Add a small resolver above the `if pr.feed is not None:` block:

```python
    # Highest-seq active DUTY_CORRECTION for this date, if any — its clocks
    # outrank the packet show time in the duty window below. Both clocks or
    # neither: a half-filled override is not a window.
    duty_override: tuple[str, str] | None = None
    _day_versions = UserAssignmentVersionStore(
        user_id=user_id,
    ).list_for_date(target.isoformat())
    if _day_versions:
        _active, _ = active_versions(_day_versions)
        for v in sorted(_active, key=lambda x: x.seq, reverse=True):
            if (
                v.version_type is VersionType.DUTY_CORRECTION
                and v.duty_on_local and v.duty_off_local
            ):
                duty_override = (v.duty_on_local, v.duty_off_local)
                break
```

`list_for_date` already exists on the store (used by the day-edit route). If its exact name differs, grep for it rather than adding a new method.

Then pass it:

```python
            window = _day_duty_window(
                date_legs[0].dt_start_utc,
                date_legs[-1].dt_end_utc,
                packet_trip.sched_duty_on if packet_trip is not None else None,
                duty_override,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/test_day_detail.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/services.py tests/app/test_day_detail.py
git commit -m "feat(day): pilot duty clocks outrank the packet show time"
```

---

### Task 7: Amend form + route

**Files:**
- Modify: `src/nac_pay/app/templates/day.html:812-820` (report field), the duty-hours input, and `recompute()` near line 1044
- Modify: `src/nac_pay/app/main.py:400-425` (form params) and the `store.save` block at ~525
- Test: `tests/app/test_reassign.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 3.
- Produces: form fields `duty_on_local` and `duty_off_local`.

- [ ] **Step 1: Write the failing test**

```python
def test_posting_duty_clocks_persists_them_and_derives_duty_hours(client):
    """The clocks are the truth; duty_hours is derived from them, so the
    stored duration can never disagree with the displayed window."""
    from decimal import Decimal

    from nac_pay.storage.assignment_versions import (
        UserAssignmentVersionStore,
        VersionType,
    )

    r = client.post("/day/2026-06-12/reassign", data={
        "version_type": "DUTY_CORRECTION",
        "assignment_id": "768",
        "entry_mode": "DETAILED",
        "block_hours": "4.17",
        "duty_hours": "9.99",          # stale client value — must be ignored
        "tafb_hours": "7.08",
        "workdays": "1",
        "duty_on_local": "04:41",
        "duty_off_local": "18:15",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    v = UserAssignmentVersionStore(user_id="default").list_for_month(2026, 6)["2026-06-12"][-1]
    assert v.version_type is VersionType.DUTY_CORRECTION
    assert v.duty_on_local == "04:41"
    assert v.duty_off_local == "18:15"
    assert abs(v.duty_hours - Decimal("13.5667")) < Decimal("0.001")


def test_posting_without_clocks_keeps_the_submitted_duty_hours(client):
    """Back-compat: the existing DETAILED flow is unchanged."""
    from decimal import Decimal

    from nac_pay.storage.assignment_versions import UserAssignmentVersionStore

    r = client.post("/day/2026-06-12/reassign", data={
        "version_type": "REASSIGNMENT",
        "assignment_id": "768",
        "entry_mode": "DETAILED",
        "block_hours": "4.17",
        "duty_hours": "9.99",
        "tafb_hours": "7.08",
        "workdays": "1",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    v = UserAssignmentVersionStore(user_id="default").list_for_month(2026, 6)["2026-06-12"][-1]
    assert v.duty_on_local is None
    assert abs(v.duty_hours - Decimal("9.99")) < Decimal("0.001")
```

Use whatever client fixture `tests/app/test_reassign.py` already uses; do not introduce a new one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/test_reassign.py -q -k duty_clocks`
Expected: FAIL — `duty_on_local` is not persisted (reads back `None`).

- [ ] **Step 3: Write the implementation**

In `src/nac_pay/app/main.py`, add two form params after `workdays`:

```python
    duty_on_local: str = Form(""),
    duty_off_local: str = Form(""),
```

In the DETAILED branch, derive `duty_dec` from the clocks when both are present, and reject a bad pair rather than silently falling back:

```python
        on_clock = duty_on_local.strip()
        off_clock = duty_off_local.strip()
        if on_clock and off_clock:
            from nac_pay.timeutil import duty_hours_between
            derived = duty_hours_between(on_clock, off_clock)
            if derived is None:
                return _bail("Enter duty on and duty off as HH:MM.")
            duty_dec = derived
        elif on_clock or off_clock:
            return _bail("Enter both duty on and duty off, or neither.")
```

Place this **after** `duty_dec = Decimal(duty_hours)` so the derived value wins, and **before** `recompute_pch_from_times` so `pch_value` reflects it.

Pass the clocks to `store.save`:

```python
        duty_on_local=on_clock or None,
        duty_off_local=off_clock or None,
```

`on_clock` / `off_clock` are only bound in the DETAILED branch — initialise both to `""` alongside `block_dec = duty_dec = ... = None` in the SIMPLE branch so the `save` call is valid in both modes.

In `src/nac_pay/app/templates/day.html`, give the report input a name and add its partner:

```html
            <label class="reassign-report">
              <span class="form-label">Duty on / report (local)</span>
              <input type="time" id="reassign-report" name="duty_on_local"
                     value="{{ data.duty_on or data.sched_duty_on or '' }}">
              <span class="form-hint">
                When duty started — the scheduled check-in, 1:00 before the
                scheduled departure. Not actual block-out: a late push
                lengthens the duty day, it doesn't move its start.
              </span>
            </label>
            <label class="reassign-duty-off">
              <span class="form-label">Duty off (local)</span>
              <input type="time" id="reassign-duty-off" name="duty_off_local"
                     value="{{ data.duty_off or '' }}">
              <span class="form-hint">
                When duty ended — last block-in plus 0:15. Edit either clock
                to correct the duty day; duty hours and duty rig follow.
              </span>
            </label>
```

Make the duty-hours input read-only so the clocks are the only editable representation:

```html
            <label>
              <span class="form-label">Duty hours</span>
              <input type="text" name="duty_hours" inputmode="decimal"
                     value="{{ defaults.duty_hours }}" readonly
                     aria-describedby="duty-hours-hint">
              <span class="form-hint" id="duty-hours-hint">
                Computed from duty on and duty off.
              </span>
            </label>
```

Update `recompute()` so both clocks drive it:

```js
        var reportEl = document.getElementById("reassign-report");
        var dutyOffEl = document.getElementById("reassign-duty-off");
        var reportMin = reportEl ? toMin(reportEl.value) : null;
        var dutyOffMin = dutyOffEl ? toMin(dutyOffEl.value) : null;
        var front = (reportMin !== null) ? reportMin : (firstOut - REPORT_PAD);
        var back = (dutyOffMin !== null) ? dutyOffMin : (lastIn + TRIP_END_PAD);
        var duty = (back - front) / 60;
        if (duty <= 0) { duty += 24; }   // duty off after local midnight
        setOut("block_hours", block / 60);
        setOut("duty_hours", duty);
```

Bind `recompute` to `change` on both clock inputs wherever the leg inputs are already bound.

- [ ] **Step 4: Run tests to verify they pass**

Run: `export NAC_PAY_DATA_DIR=$(mktemp -d) && .venv/bin/python -m pytest tests/app/test_reassign.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/main.py src/nac_pay/app/templates/day.html tests/app/test_reassign.py
git commit -m "feat(day): editable Duty on / Duty off in the amend form"
```

---

### Task 8: Full-suite gate, impact measurement, changelog

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md` (changelog row)

- [ ] **Step 1: Run the full suite**

```bash
export NAC_PAY_DATA_DIR=$(mktemp -d)
.venv/bin/python -m pytest -q > /tmp/full.txt 2>&1; echo "PYTEST_RC=$?"
grep -c "FAILED\|ERROR" /tmp/full.txt
```

Expected: `PYTEST_RC=0` and a FAILED/ERROR count of `0`. Do not proceed on any other result. This run takes several minutes — start it with `run_in_background: true` rather than blocking.

- [ ] **Step 2: Measure the retroactive impact read-only against prod**

No stored `DUTY_CORRECTION` rows exist yet, so credited PCH must be **unchanged for every user and month**. Confirm that rather than assume it. Write this probe locally, then copy it to the box (never copy prod documents back — read-only in-container inspection only):

```python
# READ-ONLY: dump every day's credited PCH so it can be diffed pre/post.
from nac_pay.app import services

EMAILS = {
    "u_511fcb054f5544f691cc48d2": "dennfish",
    "u_407f02f3b5354413bae3872c": "dpakermaker1",
    "u_d784d389ad6142f9bf1878cf": "teddyjensen",
}
for uid, who in EMAILS.items():
    try:
        months = services.available_months(uid)
    except Exception as e:
        print(f"{who}: available_months failed: {e}")
        continue
    for (y, m, _label) in months:
        try:
            pr = services._pipeline(y, m, uid)
        except Exception as e:
            print(f"{who} {y}-{m:02d}: pipeline failed: {e}")
            continue
        for t in pr.updated_month.trips:
            for d in sorted(t.dates):
                print(f"{who} {d} {t.trip_id} {t.effective_pch}")
```

Run it on the CURRENT deployed image first and save the output, then again against the new build, and `diff` the two:

```bash
scp -i ~/.ssh/amis-key.pem probe_pch.py ubuntu@35.80.137.164:/tmp/probe_pch.py
ssh -i ~/.ssh/amis-key.pem ubuntu@35.80.137.164 \
  'docker cp /tmp/probe_pch.py nac-pay:/tmp/probe_pch.py && docker exec nac-pay python /tmp/probe_pch.py' \
  > /tmp/pch_before.txt
# ...after deploying the new build to a scratch container or after merge...
diff /tmp/pch_before.txt /tmp/pch_after.txt
```

Expected: empty diff.

Per the owner: a duty change only moves the credited value when `duty/2` exceeds the other candidates — in practice actual block. When reviewing the diff, the days worth scrutinising are those where the duty-rig candidate is at or near the winning value.

Per the owner: a duty change only moves the credited value when `duty/2` exceeds the other candidates — in practice actual block. When reviewing the probe output, the days worth scrutinising are those where the duty-rig candidate is at or near the winning value.

- [ ] **Step 3: Add the changelog row**

Insert directly below the `|------|--------|` separator in `docs/SYSTEM_CONTEXT.md`, describing: the two nullable columns and their migration entry; `VersionType.DUTY_CORRECTION` as an *input* to the §3.E recompute rather than a max() candidate, and why (the auto duty-extension version also credits block, so suppressing it would discard flight-op credit); the three-tier day-view precedence; the amend-form clocks with `duty_hours` derived read-only; and the measured impact.

- [ ] **Step 4: Commit and open the PR**

```bash
git add docs/SYSTEM_CONTEXT.md
git commit -m "docs(changelog): pilot-overridable duty on/off (PR A)"
git push -u origin feat/duty-time-override
```

Then open the PR. The body must contain, in this order:

1. **What it does** — Duty On and Duty Off become editable in the amend form; the clocks are the stored truth and `duty_hours` is derived from them.
2. **Why `DUTY_CORRECTION` is an input, not a max() candidate** — `Trip.effective_pch` is `max(published, *versions)` and `apply_actuals` appends its own auto duty-extension version, so an override that shortens duty would produce a lower `pch_value` that `max()` ignores. State plainly that the first design (suppressing the auto version) was rejected because that version also credits **block**, and suppressing it would discard flight-op credit the pilot never disputed.
3. **Behaviour table** — shorten/block-wins, shorten/duty-rig-was-winning, never below published, lengthen.
4. **Migration note** — two nullable columns via `_ensure_added_columns`, verified against a simulated old-schema DB (paste the three `OK` lines from Task 2 Step 5).
5. **Measured impact** — the empty diff from Step 2, stated as measured across all three accounts and every month with data, not assumed.
6. **Test evidence** — `PYTEST_RC=0`, FAILED/ERROR count `0`, and the count of new tests.

Close with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Do not merge or deploy without the owner's explicit go-ahead.

---

## Out of scope for this plan

PR B — deviation detection, the `deviation_decisions` table, the day banner, and the calendar marker. Covered by §4 of the spec and planned separately.
