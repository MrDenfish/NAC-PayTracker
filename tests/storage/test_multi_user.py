"""Multi-tenant storage isolation tests.

Each user's profile + overrides live in ``{data_dir}/users/{user_id}/``.
Two users editing independently must never see each other's data.
"""

from __future__ import annotations

from decimal import Decimal

from nac_pay.engine import compute_pay
from nac_pay.schedule import (
    PilotProfile,
    Position,
    apply_overrides_to_month,
    lower_month,
)
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DayOverride,
    DayOverrideStore,
    PersistedPilotProfile,
    PilotProfileStore,
    User,
    UserStore,
    default_user,
    get_data_dir,
    user_dir,
)


D = Decimal


# ── User registry ─────────────────────────────────────────────────────


def test_default_user_resolves_without_registry_file():
    """The default user is always available even before the registry exists."""
    store = UserStore(get_data_dir())
    users = store.list_users()
    assert any(u.is_default and u.user_id == DEFAULT_USER_ID for u in users)


def test_user_registry_upsert_and_get():
    store = UserStore(get_data_dir())
    new = User(user_id="abc123", email="a@b.com", created_at="2026-06-11T00:00:00Z")
    store.upsert(new)
    fetched = store.get("abc123")
    assert fetched is not None
    assert fetched.email == "a@b.com"
    assert fetched.is_default is False


# ── Path namespacing ─────────────────────────────────────────────────


def test_user_dir_paths_are_isolated():
    base = get_data_dir()
    a = user_dir(base, "alice")
    b = user_dir(base, "bob")
    assert a != b
    assert a.name == "alice"
    assert b.name == "bob"
    assert a.parent.name == "users"


def test_profile_store_writes_user_scoped_row():
    """Phase 2: profiles live in the pilot_profiles table keyed by user_id."""
    store = PilotProfileStore(get_data_dir(), user_id="alice")
    persisted = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI", name="Alice", position=Position.FO,
            hourly_rate=D("100.00"),
        ),
    )
    store.save(persisted)

    # Round-trip via a fresh store proves the row was persisted under "alice".
    fresh = PilotProfileStore(get_data_dir(), user_id="alice")
    fallback = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="zzz", name="not-alice", position=Position.CPT,
            hourly_rate=D("1"),
        ),
    )
    loaded = fresh.load(fallback)
    assert loaded.profile.name == "Alice"
    # Bob doesn't see Alice's row.
    bob_view = PilotProfileStore(get_data_dir(), user_id="bob").load(fallback)
    assert bob_view == fallback


# ── Isolation ─────────────────────────────────────────────────────────


def test_two_users_have_independent_profiles():
    """Alice and Bob each set their own profile; neither should see the
    other's data."""
    default = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI", name="x", position=Position.FO,
            hourly_rate=D("100"),
        ),
    )

    alice = PilotProfileStore(get_data_dir(), user_id="alice")
    bob = PilotProfileStore(get_data_dir(), user_id="bob")

    alice.save(PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="ALC", name="Alice Pilot", position=Position.CPT,
            hourly_rate=D("150.00"),
        ),
    ))
    bob.save(PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="BOB", name="Bob Pilot", position=Position.FO,
            hourly_rate=D("130.00"),
        ),
    ))

    assert alice.load(default).profile.name == "Alice Pilot"
    assert bob.load(default).profile.name == "Bob Pilot"
    # Neither sees the other's rate
    assert alice.load(default).profile.hourly_rate == D("150.00")
    assert bob.load(default).profile.hourly_rate == D("130.00")


def test_two_users_have_independent_overrides():
    alice = DayOverrideStore(get_data_dir(), user_id="alice")
    bob = DayOverrideStore(get_data_dir(), user_id="bob")
    alice.save_one(DayOverride(date_iso="2026-06-12", reason_code="SICK"))
    bob.save_one(DayOverride(date_iso="2026-06-12", reason_code="PTO"))

    a = alice.load_all()
    b = bob.load_all()
    assert a["2026-06-12"].reason_code == "SICK"
    assert b["2026-06-12"].reason_code == "PTO"


def test_default_user_unaffected_by_other_user_edits():
    """The bundled DFI default-user behavior must stay rock solid even
    after other users write data — this is the back-compat guarantee."""
    default_store = PilotProfileStore(get_data_dir())  # no user_id → default
    other = PilotProfileStore(get_data_dir(), user_id="charlie")
    other.save(PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="CHR", name="Charlie", position=Position.CPT,
            hourly_rate=D("999.99"),
        ),
    ))
    fallback = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI", name="Dennis FISHER",
            position=Position.FO, hourly_rate=D("124.59"),
        ),
    )
    # Default user has no saved profile → falls back to caller's default.
    assert default_store.load(fallback) == fallback


def test_pipeline_runs_per_user_with_distinct_overrides():
    """End-to-end: each user gets their own engine result through the
    same _pipeline cache, namespaced by user_id."""
    from nac_pay.app.services import _pipeline, invalidate_caches
    from nac_pay.schedule import ReasonCode

    invalidate_caches()
    # Default user gets normal June baseline ($8,195.53 baseline pay).
    default_pr = _pipeline(2026, 6, DEFAULT_USER_ID)
    assert default_pr.engine_result.total_pay == D("8195.53")

    # Synthetic user "alice": save an SICK override on June 12 — the
    # FLT 768 chunk moves into the Sick category. Engine total stays
    # the same (still 1.0× rate) but the chunk *kind* differs, proving
    # her pipeline result is distinct from default's.
    alice_overrides = DayOverrideStore(get_data_dir(), user_id="alice")
    alice_overrides.save_one(
        DayOverride(date_iso="2026-06-12", reason_code=ReasonCode.SICK.value)
    )
    # Alice has no profile saved → falls back to default profile
    # (pilot_code "DFI" so the FA lookup still works).
    invalidate_caches()
    alice_pr = _pipeline(2026, 6, "alice")
    # Same engine total (sick still pays 1.0×) but different Month state.
    sick_chunks = [
        c for c in alice_pr.engine_result.per_chunk
        if c.kind.value == "SICK"
    ]
    default_sick_chunks = [
        c for c in default_pr.engine_result.per_chunk
        if c.kind.value == "SICK"
    ]
    assert len(sick_chunks) == 1
    assert sick_chunks[0].raw_pch == D("4.17")
    assert len(default_sick_chunks) == 0
