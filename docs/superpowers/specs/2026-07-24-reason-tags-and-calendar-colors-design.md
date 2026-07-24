# Reason Tags + Calendar Color Coding — Design

**Date:** 2026-07-24 · **Approved by:** author (in-session) · **Motivating bug:** July 2/3 marked SICK via the day page; the tag never appeared on the calendar or day Assignment card.

## Problem

The calendar cell and day-page Assignment card show exactly three status tags — FLT, DROPPED, CANCELLED — decided without ever reading `reason_code` (except `VOLUNTARY_DROP`). A pilot-selected reason (SICK, PTO, JURY…) changes pay math but is invisible outside the day page's dropdown. Verified in prod: the July 2/3 `DayOverride(reason_code='SICK')` rows saved and flowed into `Trip.reason_code` correctly; the display simply has no branch for them.

Color coding today: `duty_class` tints (light blue `flt`, etc.), an indigo left rail + translucent overlay for reassigned days, white for dropped/cancelled. Premium shows only as a small violet text label.

## Design

### 1. Reason tags (calendar + day page, shared mapping)

- New mapping in `services.py` near `_REASON_LABELS`: `_REASON_TAGS: dict[ReasonCode, str]` — short uppercase tags: SICK, PTO, JURY, BEREAVEMENT, TRAINING, MOVING, FAR, MILITARY, FMLA, UNPAID LOA, LESSER TRADE, UNPROT UNAVAIL. `FLOWN` and `OFF` are absent (keep FLT / OFF).
- Tag priority in both views: **DROPPED > CANCELLED > reason tag > FLT** (calendar `_build_cell` trip branch; day `load_day` after its dropped/cancelled overrides).
- Day cells without trips (duty-type days like RSV) also get the reason tag when their `Day.reason_code` is in the mapping (e.g., SICK on a reserve day).

### 2. Cell colors

Two new tints in `styles.css`, applied via the existing `duty_class` channel (so the day-page `aid-large duty-bg--…` inherits automatically):

- `.duty-bg--premium` — green, when the day's **effective premium multiplier > 1.0** (`labels.premium_multiplier(category, custom_multiplier)`): OPEN_TIME_MID_MONTH/OVERTIME/LANDING/NRFO 1.5×, JA 2.0×/2.5×, HOSTILE 2.0×, CUSTOM > 1.0. OPEN_TIME_BID_PERIOD (1.0×) does not qualify.
- `.duty-bg--absence` — yellow, when reason ∈ {SICK, PTO, JURY, BEREAVEMENT, MOVING, FAR, MILITARY, FMLA}.
- TRAINING keeps `duty-bg--training` (violet). UNPAID_LOA renders like OFF (white). DROPPED/CANCELLED keep the white `off` class (tag + struck id carry the state).
- **Priority per cell:** premium green > absence yellow > dropped/cancelled white > existing duty tints.
- Reassigned days: keep the indigo 3px left rail and all badges (↻N, ⚠, ↔) on green/yellow cells, but suppress the translucent indigo overlay there so the fill reads as one color (CSS override keyed on `.day-cell--user-reassigned.duty-bg--premium/--absence::before`).

### 3. Explicitly out of scope

- The dropped-signal divergence (calendar keys DROPPED off `reason_code is VOLUNTARY_DROP`; day page keys off a DROP user-version). Works today; to be discussed separately.
- Leg-scoped premium display (LANDING colors the whole day — acceptable; it's a premium day).
- Legend/on-boarding copy changes beyond what the calendar page already shows.

### 4. Testing

- App-level: override SICK on a trip day → calendar cell shows SICK tag + `duty-bg--absence`; day page Assignment card same. Premium override → green + tag unchanged (FLT). Premium+absence → green wins. Dropped+sick → DROPPED tag. FLOWN day unchanged (FLT, light blue). CUSTOM 2.0× → green; OPEN_TIME_BID_PERIOD → not green.
- Prod verification after deploy: July 2/3 show SICK tag + yellow.
