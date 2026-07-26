"""delete_account removes every trace of a user — DB rows and disk files."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nac_pay.storage import delete_account, get_data_dir
from nac_pay.storage.db import session_scope
from nac_pay.storage import db_models as m
from nac_pay.storage.users import user_dir

ALL_USER_TABLES = [
    m.PilotProfileRow, m.DayOverrideRow, m.EmailVerificationRow,
    m.PasswordResetRow, m.UserDocumentRow, m.UserAssignmentVersionRow,
    m.UserVersionLegRow, m.FeedReassignmentDecisionRow, m.FeedDropDecisionRow,
]


def _seed(uid: str):
    with session_scope() as sess:
        sess.add(m.UserRow(user_id=uid, email=f"{uid}@x.com"))
        sess.add(m.PilotProfileRow(user_id=uid, pilot_id="AAA", name="A",
                                   position="FO", hourly_rate=100))
        sess.add(m.DayOverrideRow(user_id=uid, date_iso="2026-07-01"))
        sess.add(m.EmailVerificationRow(token=f"t-{uid}", user_id=uid,
                                        expires_at="2027-01-01T00:00:00"))
        sess.add(m.PasswordResetRow(token=f"p-{uid}", user_id=uid,
                                    expires_at="2027-01-01T00:00:00"))
        sess.add(m.UserDocumentRow(user_id=uid, year=2026, month=7,
                                   kind="FINAL_AWARD", slot=0,
                                   original_filename="fa.pdf",
                                   uploaded_at="2026-07-01T00:00:00"))
        sess.add(m.UserAssignmentVersionRow(
            user_id=uid, date_iso="2026-07-01", seq=1,
            version_type="REASSIGNMENT", pch_value=1,
            created_at="2026-07-01T00:00:00"))
        sess.add(m.UserVersionLegRow(user_id=uid, date_iso="2026-07-01",
                                     seq=1, idx=0))
        sess.add(m.FeedReassignmentDecisionRow(
            user_id=uid, date_iso="2026-07-01", signature="1/2",
            status="CONFIRMED", decided_at="2026-07-01T00:00:00"))
        sess.add(m.FeedDropDecisionRow(user_id=uid, date_iso="2026-07-02",
                                       status="REJECTED",
                                       decided_at="2026-07-01T00:00:00"))
    d = user_dir(get_data_dir(), uid) / "docs" / "2026-07"
    d.mkdir(parents=True, exist_ok=True)
    (d / "final_award.pdf").write_bytes(b"%PDF")


def _count(model, uid):
    with session_scope() as sess:
        return sess.execute(
            select(func.count()).select_from(model).where(model.user_id == uid)
        ).scalar_one()


def test_delete_account_removes_all_rows_and_files_and_spares_neighbors():
    _seed("u_gone"); _seed("u_stays")
    delete_account("u_gone")
    for model in ALL_USER_TABLES + [m.UserRow]:
        assert _count(model, "u_gone") == 0, model.__tablename__
        assert _count(model, "u_stays") == 1, model.__tablename__
    assert not user_dir(get_data_dir(), "u_gone").exists()
    assert user_dir(get_data_dir(), "u_stays").exists()


def test_delete_account_refuses_default_user():
    with pytest.raises(ValueError):
        delete_account("default")


def test_delete_account_logs_warning_when_disk_removal_fails(monkeypatch, caplog):
    """DB removal is authoritative — a disk-tree failure (permissions, a
    locked file, ...) must not raise or block the delete; it's logged
    instead so it surfaces for manual cleanup."""
    _seed("u_disk_fail")

    def _boom(path, ignore_errors=False):
        raise OSError("disk gremlin")

    monkeypatch.setattr("nac_pay.storage.account_delete.shutil.rmtree", _boom)

    with caplog.at_level("WARNING", logger="nac_pay.storage"):
        delete_account("u_disk_fail")   # must not raise

    assert _count(m.UserRow, "u_disk_fail") == 0
    assert any(
        "document tree removal failed" in r.message for r in caplog.records
    )
