"""ORM models for the persistence layer.

Three tables map to the same three concepts as Phase 1's JSON stores:

- ``users``: SaaS account identity (placeholder until auth lands).
- ``pilot_profiles``: 1:1 with users — name, position, hourly rate, banks,
  feed URL, etc. Numeric columns use SQLAlchemy ``Numeric`` so Decimal
  precision survives both SQLite and Postgres.
- ``day_overrides``: many-per-user, composite PK ``(user_id, date_iso)``.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Auth fields (nullable for back-compat with the bundled default user).
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    email_verified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    onboarding_completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Subscription state (Phase B). subscription_status is the SaaS access
    # gate; trial_ends_at is the computed-expiry anchor for TRIALING.
    # NONE / TRIALING / TRIAL_EXPIRED / ACTIVE / PAST_DUE / CANCELED.
    subscription_status: Mapped[str] = mapped_column(
        String(24), default="NONE", nullable=False,
    )
    trial_ends_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_period_end: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    profile: Mapped["PilotProfileRow | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan",
    )
    overrides: Mapped[list["DayOverrideRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    email_verifications: Mapped[list["EmailVerificationRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    password_resets: Mapped[list["PasswordResetRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    documents: Mapped[list["UserDocumentRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )


class PilotProfileRow(Base):
    __tablename__ = "pilot_profiles"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    pilot_id: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    fleet: Mapped[str] = mapped_column(String(8), default="737", nullable=False)
    # Numeric(9, 4) covers up to $99,999.9999 — plenty of headroom for hourly rate.
    hourly_rate: Mapped[float] = mapped_column(Numeric(9, 4), nullable=False)
    sick_bank_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pto_bank_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    feed_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    feed_auto_update: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[UserRow] = relationship(back_populates="profile")


class DayOverrideRow(Base):
    __tablename__ = "day_overrides"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    date_iso: Mapped[str] = mapped_column(String(10), primary_key=True)

    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    premium_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Multipliers go up to 2.5× per spec — Numeric(4, 2) handles 99.99 max.
    custom_multiplier: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    entry_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)

    user: Mapped[UserRow] = relationship(back_populates="overrides")


class EmailVerificationRow(Base):
    """Single-use, 24h-expiry tokens for the signup → activate flow."""

    __tablename__ = "email_verifications"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    used_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    user: Mapped[UserRow] = relationship(back_populates="email_verifications")


class PasswordResetRow(Base):
    """Single-use, 1h-expiry tokens for /forgot → email → /reset/{token}."""

    __tablename__ = "password_resets"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    used_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    user: Mapped[UserRow] = relationship(back_populates="password_resets")


class UserDocumentRow(Base):
    """Metadata for a user-uploaded document. Bytes live on disk at a
    deterministic path under ``{data_dir}/users/{user_id}/docs/{year}-{month:02}/``.

    Composite PK ``(user_id, year, month, kind, slot)`` lets PAY_STUB
    accumulate multiple files per month (semi-monthly stubs) while
    FA/Packet/iCal stay at the canonical slot=0 (re-upload replaces)."""

    __tablename__ = "user_documents"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[str] = mapped_column(String(40), nullable=False)

    user: Mapped[UserRow] = relationship(back_populates="documents")


class UserAssignmentVersionRow(Base):
    """Pilot-recorded reassignment / correction for a calendar day.

    Append-only on save and never edited; the sole removal path is an
    explicit pilot ``delete`` (clears a typo/duplicate, cascading to its
    corrections). Composite PK ``(user_id, date_iso, seq)`` with seq =
    ``max(existing)+1`` per (user, date); deleting the current top seq frees
    that number for reuse, which is safe because cascade removes any
    correction that referenced it.

    `version_type=REASSIGNMENT` stacks on top of the trip's published
    value; `version_type=CORRECTION` references a prior seq via
    `correction_of` and marks it superseded. The engine considers only
    non-superseded versions in the §3.E.1.b max-PCH comparison, but the
    full history is preserved for audit. Phase G."""

    __tablename__ = "user_assignment_versions"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    date_iso: Mapped[str] = mapped_column(String(10), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)

    version_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """REASSIGNMENT or CORRECTION."""

    correction_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """For CORRECTION rows, the prior seq this supersedes."""

    assignment_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    entry_mode: Mapped[str] = mapped_column(String(16), default="SIMPLE", nullable=False)
    """SIMPLE = pilot entered pch_value directly. DETAILED = pch_value
    was computed from times via §3.E."""

    pch_value: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    """The effective PCH for this version. Populated in both modes — in
    DETAILED it's the recompute result so the engine path stays uniform."""

    # DETAILED inputs (nullable when SIMPLE). Stored verbatim so the
    # form can re-render them if the pilot opens a correction.
    block_hours: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    duty_hours: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    tafb_hours: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    deadhead_pch: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    workdays: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reason_code: Mapped[str] = mapped_column(String(32), default="FLOWN", nullable=False)
    premium_category: Mapped[str] = mapped_column(String(32), default="NONE", nullable=False)
    notes: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class UserVersionLegRow(Base):
    """Per-leg actual times the pilot entered for a DETAILED version (the
    reassign "Legs" table). Stored for display/audit — the day view merges
    these (source = Manual) with the iCal legs (source = iCal). The version's
    block/duty/TAFB aggregates (computed from these) drive pay; these rows are
    the human-readable backing. Deleted with their version (cascade).

    New table — created automatically by ``Base.metadata.create_all`` on first
    engine use, so no migration is needed on existing databases."""

    __tablename__ = "user_version_legs"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    date_iso: Mapped[str] = mapped_column(String(10), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    """Leg order within the version (0-based)."""

    flight: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    out_local: Mapped[str] = mapped_column(String(5), default="", nullable=False)
    in_local: Mapped[str] = mapped_column(String(5), default="", nullable=False)


class FeedReassignmentDecisionRow(Base):
    """Pilot confirm/reject decision on a feed-detected reassignment.

    A company mid-month reroute shows up in the iCal feed as a trip whose
    routing isn't in the Trip Pairing Packet, landing on a day that already
    carries an FA-scheduled trip (see ``schedule.apply_actuals``). The
    pipeline auto-applies it as a §3.E.1.b reassignment (new flight active,
    pay ``max(original, recomputed)``) but marks it PROPOSED until the pilot
    acts. This table records only that decision — the reassignment itself is
    re-derived from the feed every pipeline run, so no row here = PROPOSED.

    ``signature`` is the new flight sequence (e.g. ``"730/730/731"``); a fresh
    company reroute to a different sequence is a new signature = a new
    proposal the pilot must act on again. Composite PK
    ``(user_id, date_iso, signature)``.

    New table — created by ``Base.metadata.create_all`` on first engine use,
    so no migration is needed on existing databases."""

    __tablename__ = "feed_reassignment_decisions"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    date_iso: Mapped[str] = mapped_column(String(10), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    """CONFIRMED or REJECTED. Absence of a row means PROPOSED."""

    decided_at: Mapped[str] = mapped_column(String(40), nullable=False)
