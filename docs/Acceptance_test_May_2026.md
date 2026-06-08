# Acceptance test — May 2026

## Inputs
- Master Schedule: MAY 2026 ANC 737 - FO FINAL AWARDS.pdf
- Packet:          MAY_2026_Trip_Pairing_Packet.pdf
- Hourly rate:     $124.59
- **Line value (Final Award):** 65.29 PCH  → floor = max(65.29, 65) = **65.29**
- Actual events (all reassignments and reserve are **straight time = 1.0×**; they roll into Regular pay):
  - May 1  — reassignment: leg added to FLT 766, new PCH 5.00 → paid `max(original, 5.00)` at 1.0×
  - May 4  — reassignment: FLT 724 duty 13.0 hrs → duty rig 6.50 → paid `max(original, 6.50)` at 1.0×
  - May 8  — added reserve RES 1021, straight time 3.82 (1.0×)
  - May 15 — added reserve RES 1021, straight time 3.82 (1.0×)
  - May 31 — added reserve RES 1021, straight time 3.82 (1.0×)
  - one open-time pickup that **qualified** for premium: 3.82 PCH (1.5×)

## Expected output  (terminology per spec §6)
- **Earned (Option 3):** 84.11 PCH  = Regular 80.29 + Open time 3.82
- **Floor (Option 1):** 65.29   ·   **Workdays × DPG (Option 2):** < earned
- **Monthly PCH = max(65.29, Option 2, 84.11) = 84.11**  (earned wins)
- **Guarantee top-up: 0.00**  (earned exceeds the floor — no top-up)

| Category    | PCH   | Rate     | Expected $   |
|-------------|-------|----------|--------------|
| Regular pay | 80.29 | $124.59  | $10,003.33   |
| Open time   | 3.82  | $186.89  | $713.90      |
| Sick        | 0.00  | —        | $0.00        |
| **Total**   | 84.11 |          | **$10,717.23** |

Reconciles to the May pay stub: Regular Pay 80.29 × $124.59 = $10,003.33; Open Time 3.82 × $186.885 (= $124.59 × 1.5) = $713.90.

Expected discrepancies vs. company stub: **none (clean month).**

## Notes
- **Category mapping:** "Regular pay" aggregates the scheduled line (65.29) + the three reserve straight-time days + the two reassignments — everything at 1.0× — summing to 80.29. "Open time" is only the single qualifying 1.5× pickup (3.82).
- "Base monthly PCH = 65.29" from the earlier draft was the **line value**, not the §6 monthly PCH. The "15.0 top-up" was **additional straight-time PCH earned above the line** (65.29 + 15.0 = 80.29 regular), which is *not* a guarantee top-up in the spec's sense. Both are folded into the corrected figures above.
- **Reassignment greater-of (3.E.1.b)** is better verified with a small focused unit test: feed the engine the original 766/724 PCH from the May packet plus the new values and assert it picks the max. The month rollup above is the acceptance target; it does not by itself prove the per-reassignment logic.
