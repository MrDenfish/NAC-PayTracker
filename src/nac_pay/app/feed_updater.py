"""Hourly iCal feed updater — the feed-updater milestone.

Each opted-in user (Settings → "Auto-update hourly", with a feed URL) has
their BlueOne iCal feed pulled on a recurring schedule and saved as the
month's ``feed.ics``, exactly as if they'd uploaded it. The existing
pipeline reconciles it against the packet on the next page load — this
module only does the *acquisition* half (§10: the program never scrapes a
company system; it fetches the per-user feed URL the pilot pasted).

Design notes:
- **Multi-tenant.** ``run_once`` iterates every user with auto-update on,
  isolating per-user failures so one bad feed never blocks the others.
- **Target months: current + next.** A single BlueOne feed spans multiple
  calendar months, so the same bytes are saved under each target month and
  each month's pipeline filters to its own dates. We only write months the
  user has already *set up* (Final Award + Trip Packet uploaded) so a fetch
  never conjures a phantom, un-computable month into the switcher.
- **No DB schema change.** "Last fetched" is just the iCal document's
  ``uploaded_at`` (set by ``UserDocumentsStore.save``), read back via
  ``last_feed_fetch`` for the Settings UI.
- **Self-contained loop.** A lightweight asyncio task (no APScheduler dep)
  started from the FastAPI lifespan; gated behind ``FEED_UPDATER_ENABLED``
  so tests and dev don't spawn a network loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from nac_pay.parsers import merge_feed_bytes
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
    UserDocumentsStore,
    feed_auto_update_profiles,
    get_data_dir,
)

logger = logging.getLogger("nac_pay.feed_updater")

# A fetched feed must look like iCalendar and stay within a sane size.
_MAX_FEED_BYTES = 5 * 1024 * 1024
_ICAL_MARKER = b"BEGIN:VCALENDAR"
_FETCH_TIMEOUT_SECONDS = 20.0

DEFAULT_INTERVAL_SECONDS = 3600  # hourly


def updater_enabled() -> bool:
    """Whether the background loop should run. Off by default so the test
    suite and local dev don't spawn a network loop on import; prod sets
    ``FEED_UPDATER_ENABLED=true``."""
    return os.environ.get("FEED_UPDATER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def interval_seconds() -> int:
    """Refresh cadence in seconds (default hourly). Overridable via
    ``FEED_UPDATE_INTERVAL_SECONDS`` for ops tuning / tests."""
    raw = os.environ.get("FEED_UPDATE_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def target_months(today: date) -> list[tuple[int, int]]:
    """The current calendar month plus the next one — so a freshly posted
    next-month schedule is picked up automatically."""
    cur = (today.year, today.month)
    nxt = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return [cur, nxt]


@dataclass(frozen=True)
class MonthUpdate:
    year: int
    month: int
    ok: bool
    detail: str  # "updated", "skipped: no Final Award + Packet", or an error


@dataclass(frozen=True)
class UserUpdate:
    user_id: str
    months: tuple[MonthUpdate, ...]

    @property
    def changed(self) -> bool:
        return any(m.ok and m.detail == "updated" for m in self.months)


class FeedFetchError(Exception):
    """Raised when a feed URL can't be fetched or isn't valid iCalendar."""


def fetch_ical(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Fetch and lightly validate an iCal feed. Rejects non-http(s) schemes
    (no file:// / SSRF-by-typo), oversized bodies, and anything that isn't
    iCalendar. Raises ``FeedFetchError`` on any failure."""
    cleaned = (url or "").strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise FeedFetchError(f"feed URL must be http(s): {cleaned!r}")

    own_client = client is None
    client = client or httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        resp = client.get(cleaned)
        resp.raise_for_status()
        data = resp.content
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"fetch failed: {exc}") from exc
    finally:
        if own_client:
            client.close()

    if not data:
        raise FeedFetchError("empty feed body")
    if len(data) > _MAX_FEED_BYTES:
        raise FeedFetchError(f"feed too large ({len(data)} bytes)")
    if _ICAL_MARKER not in data[:4096]:
        raise FeedFetchError("response is not an iCalendar feed (no BEGIN:VCALENDAR)")
    return data


def _month_is_set_up(store: UserDocumentsStore, year: int, month: int) -> bool:
    """True when the user has both a Final Award and a Trip Packet for the
    month — the minimum the pipeline needs to compute pay. We don't write a
    feed into a month that can't be computed (avoids phantom switcher rows)."""
    fa = store.get(year, month, DocumentKind.FINAL_AWARD)
    packet = store.get(year, month, DocumentKind.TRIP_PACKET)
    return fa is not None and packet is not None


def update_user_feed(
    user_id: str,
    feed_url: str,
    *,
    today: date,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> UserUpdate:
    """Fetch one user's feed and save it into each set-up target month.

    The fetch happens once; the bytes are reused across months. Per-month
    skips (month not set up) are recorded but aren't failures. A fetch
    failure marks every target month failed for this user."""
    if user_id == DEFAULT_USER_ID:
        # The dev/default user reads bundled docs — never auto-fetched.
        return UserUpdate(user_id=user_id, months=())

    store = UserDocumentsStore(get_data_dir(), user_id)
    months = target_months(today)
    now = now or datetime.now(timezone.utc)

    try:
        data = fetch_ical(feed_url, client=client)
    except FeedFetchError as exc:
        logger.warning("feed fetch failed for user %s: %s", user_id, exc)
        return UserUpdate(
            user_id=user_id,
            months=tuple(
                MonthUpdate(y, m, ok=False, detail=str(exc)) for (y, m) in months
            ),
        )

    results: list[MonthUpdate] = []
    for (y, m) in months:
        if not _month_is_set_up(store, y, m):
            results.append(
                MonthUpdate(y, m, ok=True, detail="skipped: no Final Award + Packet")
            )
            continue
        try:
            # Merge-preserve: keep frozen (completed) legs the fetch dropped
            # so BlueOne's ~24h window can't erase flown history on overwrite.
            existing = store.get(y, m, DocumentKind.ICAL_FEED)
            existing_bytes = (
                existing.path.read_bytes()
                if existing is not None and existing.exists
                else None
            )
            merged = merge_feed_bytes(existing_bytes, data, now)
            store.save(y, m, DocumentKind.ICAL_FEED, "feed.ics", merged)
            results.append(MonthUpdate(y, m, ok=True, detail="updated"))
        except Exception as exc:  # storage/IO failure — isolate to the month
            logger.warning("feed save failed for user %s %d-%02d: %s", user_id, y, m, exc)
            results.append(MonthUpdate(y, m, ok=False, detail=f"save failed: {exc}"))

    return UserUpdate(user_id=user_id, months=tuple(results))


def run_once(
    *,
    today: date | None = None,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> list[UserUpdate]:
    """One full sweep across every opted-in user. Per-user failures are
    isolated. Clears the pipeline cache once if anything actually changed so
    the next page render reflects the new feed."""
    today = today or date.today()
    updates: list[UserUpdate] = []
    for user_id, feed_url in feed_auto_update_profiles():
        try:
            updates.append(
                update_user_feed(
                    user_id, feed_url, today=today, now=now, client=client
                )
            )
        except Exception as exc:  # never let one user abort the sweep
            logger.exception("unexpected error updating user %s: %s", user_id, exc)

    if any(u.changed for u in updates):
        from .services import invalidate_caches
        invalidate_caches()

    changed = sum(1 for u in updates if u.changed)
    logger.info(
        "feed sweep: %d user(s) checked, %d updated", len(updates), changed,
    )
    return updates


def last_feed_fetch(user_id: str, today: date | None = None) -> str | None:
    """Most recent iCal ``uploaded_at`` (ISO string) across the target
    months, for the Settings "last fetched" display. None when no feed has
    ever been saved. The dev/default user never auto-fetches → None."""
    if user_id == DEFAULT_USER_ID:
        return None
    today = today or date.today()
    store = UserDocumentsStore(get_data_dir(), user_id)
    stamps: list[str] = []
    for (y, m) in target_months(today):
        rec = store.get(y, m, DocumentKind.ICAL_FEED)
        if rec is not None:
            stamps.append(rec.uploaded_at)
    return max(stamps) if stamps else None


async def feed_update_loop(stop: asyncio.Event) -> None:
    """Background task: run a sweep, then wait for the interval (or an early
    stop). Each sweep's blocking DB/HTTP work runs in a worker thread so the
    event loop stays free. Exceptions are swallowed so the loop survives a
    bad tick."""
    interval = interval_seconds()
    logger.info("feed updater started (every %ds)", interval)
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_once)
        except Exception:  # pragma: no cover - defensive; run_once self-isolates
            logger.exception("feed sweep crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("feed updater stopped")
