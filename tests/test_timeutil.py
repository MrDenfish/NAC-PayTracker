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


def test_duty_hours_between_same_day():
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    # Aug 8 2026: show 04:41, release 18:15 = 13:34 = 13.5667h
    got = duty_hours_between("04:41", "18:15")
    assert got is not None
    assert abs(got - Decimal("13.5667")) < Decimal("0.001")


def test_duty_hours_between_crosses_midnight():
    """Duty off at or before duty on means the next day, never negative."""
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    # 04:41 show, 01:30 release next morning = 20:49 = 20.8167h
    got = duty_hours_between("04:41", "01:30")
    assert got is not None
    assert abs(got - Decimal("20.8167")) < Decimal("0.001")


def test_duty_hours_between_equal_clocks_is_a_full_day():
    from decimal import Decimal

    from nac_pay.timeutil import duty_hours_between

    assert duty_hours_between("04:41", "04:41") == Decimal("24")


def test_duty_hours_between_rejects_garbage():
    from nac_pay.timeutil import duty_hours_between

    assert duty_hours_between("", "18:15") is None
    assert duty_hours_between("04:41", "") is None
    assert duty_hours_between("25:00", "18:15") is None
    assert duty_hours_between("04:61", "18:15") is None
    assert duty_hours_between("0441", "18:15") is None
