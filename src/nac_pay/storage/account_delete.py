"""Account deletion — immediate hard delete of a user's every trace.

One function so the future grace-period flow (deactivate now, purge on a
schedule once there are subscribers) can call the exact same removal.
Deletes are explicit per table rather than relying on FK cascade: four
tables have no ORM cascade relationship and SQLite FK enforcement is off
by default."""

from __future__ import annotations

import logging
import shutil

from sqlalchemy import delete as sa_delete

from .users import DEFAULT_USER_ID, user_dir

logger = logging.getLogger("nac_pay.storage")


def delete_account(user_id: str) -> None:
    if user_id == DEFAULT_USER_ID:
        raise ValueError("The default (dev) account cannot be deleted.")
    from . import get_data_dir
    from .db import session_scope
    from . import db_models as m

    ordered = [
        m.UserVersionLegRow,
        m.UserAssignmentVersionRow,
        m.FeedReassignmentDecisionRow,
        m.FeedDropDecisionRow,
        m.UserDocumentRow,
        m.DayOverrideRow,
        m.EmailVerificationRow,
        m.PasswordResetRow,
        m.PilotProfileRow,
        m.UserRow,
    ]
    with session_scope() as sess:
        for model in ordered:
            sess.execute(sa_delete(model).where(model.user_id == user_id))

    # DB removal above is authoritative — the account is gone either way.
    # A disk-tree failure (permissions, a locked file, etc.) shouldn't
    # silently vanish; log it so it surfaces for manual cleanup instead
    # of being swallowed. A missing directory (a user who never uploaded
    # anything) is normal, not a failure — skip it rather than logging a
    # false-positive warning on every doc-less account deletion.
    doc_dir = user_dir(get_data_dir(), user_id)
    if doc_dir.exists():
        try:
            shutil.rmtree(doc_dir, ignore_errors=False)
        except OSError as exc:
            logger.warning(
                "account %s: document tree removal failed: %s", user_id, exc,
            )
