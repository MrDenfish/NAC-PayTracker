"""Hourly feed updater — fetch validation, per-user/month gating, sweep."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from nac_pay.app import feed_updater as fu
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
    PersistedPilotProfile,
    PilotProfileStore,
    UserDocumentsStore,
    feed_auto_update_profiles,
    get_data_dir,
)

ICAL = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
JUNE22 = date(2026, 6, 22)


# ── Helpers ─────────────────────────────────────────────────────────


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_client(content: bytes = ICAL) -> httpx.Client:
    return _client(lambda req: httpx.Response(200, content=content))


def _make_user(uid: str, feed_url: str = "https://feed.example/x.ics", auto: bool = True) -> None:
    prof = PilotProfile(
        pilot_id="DFI", name="Pilot", position=Position.FO,
        hourly_rate=Decimal("100"),
    )
    PilotProfileStore(get_data_dir(), uid).save(
        PersistedPilotProfile(profile=prof, feed_url=feed_url, feed_auto_update=auto)
    )


def _set_up_month(uid: str, year: int, month: int) -> UserDocumentsStore:
    """Give a user the FA + Packet a month needs to be 'set up'."""
    store = UserDocumentsStore(get_data_dir(), uid)
    store.save(year, month, DocumentKind.FINAL_AWARD, "fa.pdf", b"x")
    store.save(year, month, DocumentKind.TRIP_PACKET, "packet.pdf", b"x")
    return store


# ── fetch_ical ──────────────────────────────────────────────────────


def test_fetch_ical_returns_valid_feed():
    with _ok_client() as c:
        assert fu.fetch_ical("https://feed.example/x.ics", client=c) == ICAL


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "x.ics", ""])
def test_fetch_ical_rejects_non_http_scheme(url):
    with pytest.raises(fu.FeedFetchError):
        fu.fetch_ical(url, client=_ok_client())


def test_fetch_ical_rejects_non_ical_body():
    with _ok_client(b"<html>not a calendar</html>") as c:
        with pytest.raises(fu.FeedFetchError):
            fu.fetch_ical("https://feed.example/x.ics", client=c)


def test_fetch_ical_rejects_empty_body():
    with _ok_client(b"") as c:
        with pytest.raises(fu.FeedFetchError):
            fu.fetch_ical("https://feed.example/x.ics", client=c)


def test_fetch_ical_raises_on_http_error():
    with _client(lambda req: httpx.Response(500)) as c:
        with pytest.raises(fu.FeedFetchError):
            fu.fetch_ical("https://feed.example/x.ics", client=c)


def test_fetch_ical_rejects_oversized_feed():
    big = b"BEGIN:VCALENDAR\n" + b"x" * (6 * 1024 * 1024)
    with _ok_client(big) as c:
        with pytest.raises(fu.FeedFetchError):
            fu.fetch_ical("https://feed.example/x.ics", client=c)


# ── target_months ───────────────────────────────────────────────────


def test_target_months_current_and_next():
    assert fu.target_months(JUNE22) == [(2026, 6), (2026, 7)]


def test_target_months_year_rollover():
    assert fu.target_months(date(2026, 12, 9)) == [(2026, 12), (2027, 1)]


# ── update_user_feed ────────────────────────────────────────────────


def test_update_writes_feed_only_to_set_up_months():
    _make_user("alice")
    store = _set_up_month("alice", 2026, 6)  # June set up; July is not

    with _ok_client() as c:
        result = fu.update_user_feed("alice", "https://feed.example/x.ics", today=JUNE22, client=c)

    by_month = {(m.year, m.month): m for m in result.months}
    assert by_month[(2026, 6)].detail == "updated"
    assert by_month[(2026, 7)].detail.startswith("skipped")
    # June got the bytes; July never created an iCal row.
    assert store.get(2026, 6, DocumentKind.ICAL_FEED).path.read_bytes() == ICAL
    assert store.get(2026, 7, DocumentKind.ICAL_FEED) is None
    assert result.changed is True


def test_update_no_set_up_months_makes_no_change():
    _make_user("alice")
    with _ok_client() as c:
        result = fu.update_user_feed("alice", "https://feed.example/x.ics", today=JUNE22, client=c)
    assert all(m.detail.startswith("skipped") for m in result.months)
    assert result.changed is False


def test_update_fetch_failure_writes_nothing():
    _make_user("alice")
    store = _set_up_month("alice", 2026, 6)
    with _client(lambda req: httpx.Response(503)) as c:
        result = fu.update_user_feed("alice", "https://feed.example/x.ics", today=JUNE22, client=c)
    assert all(not m.ok for m in result.months)
    assert store.get(2026, 6, DocumentKind.ICAL_FEED) is None


def test_update_default_user_is_noop():
    result = fu.update_user_feed(DEFAULT_USER_ID, "https://feed.example/x.ics", today=JUNE22)
    assert result.months == ()
    assert result.changed is False


# ── feed_auto_update_profiles + run_once ─────────────────────────────


def test_profiles_lists_only_opted_in_users():
    _make_user("on_with_url", "https://feed.example/a.ics", auto=True)
    _make_user("off", "https://feed.example/b.ics", auto=False)
    _make_user("on_no_url", "", auto=True)
    ids = {uid for uid, _ in feed_auto_update_profiles()}
    assert ids == {"on_with_url"}


def test_run_once_updates_only_opted_in(monkeypatch):
    _make_user("alice", "https://feed.example/a.ics", auto=True)
    _make_user("bob", "https://feed.example/b.ics", auto=False)
    _set_up_month("alice", 2026, 6)
    _set_up_month("bob", 2026, 6)

    calls = {"invalidated": 0}
    monkeypatch.setattr(
        "nac_pay.app.services.invalidate_caches",
        lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1),
    )

    with _ok_client() as c:
        updates = fu.run_once(today=JUNE22, client=c)

    assert [u.user_id for u in updates] == ["alice"]
    assert UserDocumentsStore(get_data_dir(), "alice").get(2026, 6, DocumentKind.ICAL_FEED) is not None
    assert UserDocumentsStore(get_data_dir(), "bob").get(2026, 6, DocumentKind.ICAL_FEED) is None
    assert calls["invalidated"] == 1


def test_run_once_isolates_per_user_failure():
    _make_user("good", "https://good.example/a.ics", auto=True)
    _make_user("bad", "https://bad.example/b.ics", auto=True)
    _set_up_month("good", 2026, 6)
    _set_up_month("bad", 2026, 6)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "bad.example":
            return httpx.Response(500)
        return httpx.Response(200, content=ICAL)

    with _client(handler) as c:
        updates = fu.run_once(today=JUNE22, client=c)

    by_user = {u.user_id: u for u in updates}
    assert by_user["good"].changed is True
    assert by_user["bad"].changed is False
    # The good user still got updated despite the bad user's failure.
    assert UserDocumentsStore(get_data_dir(), "good").get(2026, 6, DocumentKind.ICAL_FEED) is not None


def test_run_once_no_change_skips_cache_invalidation(monkeypatch):
    _make_user("alice", "https://feed.example/a.ics", auto=True)
    # No set-up month → nothing changes.
    calls = {"invalidated": 0}
    monkeypatch.setattr(
        "nac_pay.app.services.invalidate_caches",
        lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1),
    )
    with _ok_client() as c:
        fu.run_once(today=JUNE22, client=c)
    assert calls["invalidated"] == 0


# ── last_feed_fetch + env knobs ─────────────────────────────────────


def test_last_feed_fetch_returns_saved_timestamp():
    _make_user("alice")
    _set_up_month("alice", 2026, 6)
    with _ok_client() as c:
        fu.update_user_feed("alice", "https://feed.example/x.ics", today=JUNE22, client=c)
    stamp = fu.last_feed_fetch("alice", today=JUNE22)
    assert stamp is not None and stamp.startswith("2026")


def test_last_feed_fetch_none_when_never_fetched():
    _make_user("alice")
    assert fu.last_feed_fetch("alice", today=JUNE22) is None
    assert fu.last_feed_fetch(DEFAULT_USER_ID, today=JUNE22) is None


def test_updater_enabled_env(monkeypatch):
    monkeypatch.delenv("FEED_UPDATER_ENABLED", raising=False)
    assert fu.updater_enabled() is False
    monkeypatch.setenv("FEED_UPDATER_ENABLED", "true")
    assert fu.updater_enabled() is True
    monkeypatch.setenv("FEED_UPDATER_ENABLED", "0")
    assert fu.updater_enabled() is False


def test_interval_seconds_env(monkeypatch):
    monkeypatch.delenv("FEED_UPDATE_INTERVAL_SECONDS", raising=False)
    assert fu.interval_seconds() == fu.DEFAULT_INTERVAL_SECONDS
    monkeypatch.setenv("FEED_UPDATE_INTERVAL_SECONDS", "900")
    assert fu.interval_seconds() == 900
    monkeypatch.setenv("FEED_UPDATE_INTERVAL_SECONDS", "garbage")
    assert fu.interval_seconds() == fu.DEFAULT_INTERVAL_SECONDS
    monkeypatch.setenv("FEED_UPDATE_INTERVAL_SECONDS", "-5")
    assert fu.interval_seconds() == fu.DEFAULT_INTERVAL_SECONDS


# ── merge-preserve (frozen legs survive the rolling-window overwrite) ──

from datetime import datetime, timezone  # noqa: E402


def _vevent(uid: str, start: str, end: str) -> bytes:
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        f"SUMMARY:FLT - NC{uid} ANC-OME N1\r\nEND:VEVENT\r\n"
    ).encode()


def _vcal(*vevents: bytes) -> bytes:
    return (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        + b"".join(vevents)
        + b"END:VCALENDAR\r\n"
    )


def test_update_merge_preserves_frozen_dropped_leg():
    """A leg that aged out of the fetched feed but is already completed must
    survive the save — BlueOne's rolling window can't erase flown history."""
    uid = "merge-user"
    _make_user(uid)
    store = _set_up_month(uid, 2026, 6)
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    # Stored feed has both legs (both completed before `now`).
    store.save(
        2026, 6, DocumentKind.ICAL_FEED, "feed.ics",
        _vcal(
            _vevent("720", "20260627T140000Z", "20260627T160000Z"),
            _vevent("721", "20260627T170000Z", "20260627T190000Z"),
        ),
    )
    # Fetch drops 720 (aged out).
    incoming = _vcal(_vevent("721", "20260627T170000Z", "20260627T190000Z"))
    with _client(lambda req: httpx.Response(200, content=incoming)) as c:
        fu.update_user_feed(
            uid, "https://feed.example/x.ics", today=JUNE22, now=now, client=c,
        )

    merged = store.get(2026, 6, DocumentKind.ICAL_FEED).path.read_bytes()
    assert b"UID:720" in merged and b"UID:721" in merged
