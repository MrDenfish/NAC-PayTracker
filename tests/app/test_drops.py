"""Company-approved drops: /day/<date>/drop route + engine integration.

A drop is the inverse of a reassignment — it forfeits a scheduled
assignment (0 PCH credited, the workday removed, the floor reduced 1:1 by
the lost PCH per §3.D). Modeled as a ``VersionType.DROP`` that stamps the
matched Trip/Day with ``ReasonCode.VOLUNTARY_DROP`` so the existing
lower.py FLOOR_DROP path does the work. Reversible via a CORRECTION.
"""

from __future__ import annotations

from decimal import Decimal

from nac_pay.app.services import invalidate_caches
from nac_pay.storage import UserAssignmentVersionStore, VersionType, active_versions
from fastapi.testclient import TestClient
from nac_pay.app.main import app

# Reuse the Phase-G end-to-end harness (signup → verify → ACTIVE → upload
# bundled June 2026 docs) and the June-PCH probe.
from tests.app.test_reassign import _bootstrap_user_with_june, _june_total_pch

D = Decimal


# ── Route validation ─────────────────────────────────────────────────


def test_drop_requires_company_approval(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "drop-approve@x.test")
    r = client.post(
        "/day/2026-06-02/drop",
        data={"assignment_id": "722/750"},  # no company_approved
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "approval%20is%20required" in r.headers["location"]
    # Nothing was stored.
    assert UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02") == []


def test_drop_success_stores_zero_pch_drop(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "drop-ok@x.test")
    r = client.post(
        "/day/2026-06-02/drop",
        data={"company_approved": "1", "assignment_id": "722/750",
              "notes": "CS approved 09:14"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/day/2026-06-02?saved=drop"

    versions = UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")
    assert len(versions) == 1
    v = versions[0]
    assert v.version_type is VersionType.DROP
    assert v.pch_value == D("0")
    assert "Company-approved drop" in v.notes


def test_drop_blocks_default_user():
    client = TestClient(app)  # AUTH_REQUIRED unset → default user
    r = client.post(
        "/day/2026-06-02/drop",
        data={"company_approved": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Default%20user" in r.headers["location"]


def test_cannot_drop_an_already_dropped_day(monkeypatch):
    """Once dropped the day credits 0 PCH, so a second drop has nothing to
    forfeit — the route rejects it."""
    client, _ = _bootstrap_user_with_june(monkeypatch, "drop-twice@x.test")
    client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                follow_redirects=False)
    invalidate_caches()
    r = client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                    follow_redirects=False)
    assert "Nothing%20to%20drop" in r.headers["location"]


# ── Engine integration ──────────────────────────────────────────────


def test_drop_removes_pch_from_monthly_total(monkeypatch):
    """June 2's trip (~4.92 published PCH) dropped → earned PCH falls by ~4.92."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "drop-engine@x.test")
    before = _june_total_pch(uid)

    client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                follow_redirects=False)
    after = _june_total_pch(uid)

    diff = after - before
    assert diff < D("-4.0"), f"expected ≈-4.92 PCH, got {diff}"


def test_drop_emits_voluntary_drop_floor_event(monkeypatch):
    """The forfeit shows up as a VOLUNTARY_DROP floor event, and the dropped
    trip credits no chunk."""
    from nac_pay.app.services import _pipeline
    from nac_pay.engine.models import FloorEventKind

    client, uid = _bootstrap_user_with_june(monkeypatch, "drop-floor@x.test")
    client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                follow_redirects=False)
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)

    # Re-lower the updated month to inspect floor events directly.
    from nac_pay.schedule import lower_month
    inp = lower_month(pr.updated_month)
    drop_events = [e for e in inp.floor_events
                   if e.kind is FloorEventKind.VOLUNTARY_DROP]
    assert drop_events, "expected a VOLUNTARY_DROP floor event after dropping"
    assert any(e.delta_pch > D("4.0") for e in drop_events)


def test_dropped_day_and_calendar_render(monkeypatch):
    """The day-detail and calendar templates render a dropped day without
    error and surface the DROPPED state."""
    client, _ = _bootstrap_user_with_june(monkeypatch, "drop-render@x.test")
    client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                follow_redirects=False)
    invalidate_caches()

    day = client.get("/day/2026-06-02")
    assert day.status_code == 200
    assert "dropped (company-approved)" in day.text.lower()
    # The Restore affordance is offered on the dropped day.
    assert "Restore" in day.text

    cal = client.get("/calendar?ym=2026-6")
    assert cal.status_code == 200
    assert "DROPPED" in cal.text
    # Stylesheet is cache-busted with a content-hash version query so a CSS
    # change reaches the browser/edge without a manual purge.
    assert "styles.css?v=" in cal.text
    # The dropped assignment id is de-emphasized (not bold) to match the
    # DROPPED/OFF duty-label styling.
    assert "aid--dropped" in cal.text


def test_restore_reverts_the_drop(monkeypatch):
    """A CORRECTION superseding the drop restores the original PCH."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "drop-restore@x.test")
    before = _june_total_pch(uid)

    client.post("/day/2026-06-02/drop", data={"company_approved": "1"},
                follow_redirects=False)
    dropped = _june_total_pch(uid)
    assert dropped < before

    # The drop is seq=1; supersede it with a correction back to the original.
    versions = UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")
    drop_seq = versions[0].seq
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": str(drop_seq),
              "entry_mode": "SIMPLE", "assignment_id": "722/750",
              "pch_value": "4.92", "reason_code": "FLOWN",
              "premium_category": "NONE"},
        follow_redirects=False,
    )
    restored = _june_total_pch(uid)

    # The drop is now superseded; earned PCH is back to ~before.
    assert abs(restored - before) < D("0.10"), \
        f"restore should return to {before}, got {restored}"
    # Audit trail intact: both rows survive; the drop is superseded.
    versions = UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")
    assert len(versions) == 2
    _active, superseded = active_versions(versions)
    assert drop_seq in superseded
