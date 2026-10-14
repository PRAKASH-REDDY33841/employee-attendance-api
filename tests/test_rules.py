"""
Tests for core business rules (R1 - R8).
"""
from datetime import datetime, timezone
import pytest
from app.main import (
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    get_attendance_date,
    count_working_days,
    round_half_up_2,
    round_half_up_4,
    truncate_to_seconds_ms,
    ms_to_utc_dt,
)


def test_r1_second_truncation():
    """R1: Instants are truncated to whole seconds before any calculation."""
    ms = 1783312500999
    truncated = truncate_to_seconds_ms(ms)
    assert truncated == 1783312500000

    dt = ms_to_utc_dt(ms)
    assert dt.microsecond == 0


def test_r1_overnight_shift_date():
    """
    R1: Overnight shift (shift_end <= shift_start, e.g. 22:00-06:00).
    A punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    # 2026-07-06 21:55 IST (16:25 UTC) -> belongs to 2026-07-06
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_evening, "22:00", "06:00") == "2026-07-06"

    # 2026-07-07 01:15 IST (2026-07-06 19:45 UTC) -> earlier than 06:00, belongs to 2026-07-06
    dt_early_morning = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_early_morning, "22:00", "06:00") == "2026-07-06"

    # 2026-07-07 07:00 IST (01:30 UTC) -> after 06:00, belongs to 2026-07-07
    dt_morning_after = datetime(2026, 7, 7, 1, 30, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_morning_after, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes_grace_period():
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    Example (shift 09:30):
      09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    # Exactly 09:40:00 IST (04:10:00 UTC) -> 0 minutes
    dt_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_094000, "09:30", "2026-07-06") == 0

    # 09:40:01 IST (04:10:01 UTC) -> 10 minutes
    dt_094001 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_094001, "09:30", "2026-07-06") == 10

    # 10:15:59 IST (04:45:59 UTC) -> 45 minutes
    dt_101559 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_101559, "09:30", "2026-07-06") == 45

    # Early arrival 09:25:00 IST -> 0 minutes
    dt_early = datetime(2026, 7, 6, 3, 55, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "09:30", "2026-07-06") == 0


def test_r3_overtime_minimum_30_minutes():
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    # Shift 18:30 end
    # Punch out at 18:59:59 IST (29 min 59 sec after) -> 0 overtime
    dt_29m = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    assert compute_overtime(dt_29m, "18:30", "2026-07-06", "09:30") == 0

    # Punch out at 19:00:00 IST (exactly 30 min after) -> 30 overtime
    dt_30m = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_30m, "18:30", "2026-07-06", "09:30") == 30

    # Punch out at 19:10:00 IST (40 min after) -> 40 overtime
    dt_40m = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_40m, "18:30", "2026-07-06", "09:30") == 40


def test_r3_overtime_overnight_shift():
    """Overtime on an overnight shift crossing midnight."""
    # Shift 22:00 - 06:00 on 2026-07-06.
    # Punch out at 06:40 IST on 2026-07-07 (01:10 UTC)
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out, "06:00", "2026-07-06", "22:00") == 40


def test_r4_work_hours_half_up_rounding():
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    # Sample 1: 03:58:00Z to 13:05:00Z -> 32820 seconds / 3600 = 9.11666... -> 9.12
    dt_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    dt_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(dt_in, dt_out) == 9.12

    # Half-up test: 2.865 hours (10314 seconds / 3600 = 2.865)
    # Banker's rounding rounds to 2.86 (even), but Half-Up must round to 2.87!
    dt_in2 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    dt_out2 = datetime(2026, 7, 6, 2, 51, 54, tzinfo=timezone.utc)  # 10314 sec
    assert compute_work_hours(dt_in2, dt_out2) == 2.87


def test_r5_half_day_rule():
    """R5: Half day is true when rounded work_hours < 4.50."""
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.50) is False
    assert compute_half_day(None) is False


def test_r7_working_days_calculation():
    """
    R7: Mon-Fri working days, counted from joined_on for mid-month joiners.
    """
    # July 2026 has 31 days. Starts on Wednesday, ends on Friday. Total weekdays = 23.
    assert count_working_days("2026-07", "2026-01-05") == 23

    # Mid-month joiner: joined 2026-07-13 (Monday). Weekdays from July 13 to July 31 = 15.
    assert count_working_days("2026-07", "2026-07-13") == 15

    # Joined in a future month: joined 2026-08-01 -> 0 working days in July 2026
    assert count_working_days("2026-07", "2026-08-01") == 0


def test_r8_rounding():
    """R8: 2 decimals half-up for numbers, 4 decimals half-up for rates."""
    assert round_half_up_2(12.345) == 12.35
    assert round_half_up_2(12.344) == 12.34
    assert round_half_up_4(0.85714) == 0.8571
    assert round_half_up_4(0.85715) == 0.8572
    assert round_half_up_2(None) is None
    assert round_half_up_4(None) is None
