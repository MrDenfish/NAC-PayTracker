"""Tests for the shared domicile (Anchorage) timezone helpers."""

from datetime import date, datetime, timezone

from nac_pay.timeutil import DOMICILE_TZ, local_date


def test_local_date_evening_anc_is_next_day_utc():
    # 18:00 AKDT Jul 24 = 02:00 UTC Jul 25 — civil date must be Jul 24.
    dt = datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)
    assert local_date(dt) == date(2026, 7, 24)


def test_local_date_winter_offset_is_utc_minus_9():
    # AKST (no DST): 08:59 UTC Jan 2 = 23:59 AKST Jan 1.
    dt = datetime(2026, 1, 2, 8, 59, tzinfo=timezone.utc)
    assert local_date(dt) == date(2026, 1, 1)


def test_domicile_tz_key():
    assert str(DOMICILE_TZ) == "America/Anchorage"
