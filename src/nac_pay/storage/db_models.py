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
