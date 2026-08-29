"""
Schedule Utilities
Cron expression validation, next-run calculation, presets, and
conversion of cron expressions to systemd OnCalendar format.

Used by the scheduled Syncoid replication feature (issue #194).
Schedules are stored canonically as 5-field cron expressions and
interpreted in the system local timezone.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from croniter import croniter


# Preset label -> cron expression
SCHEDULE_PRESETS: List[Dict[str, str]] = [
    {"label": "Every hour", "expression": "0 * * * *"},
    {"label": "Every 4 hours", "expression": "0 */4 * * *"},
    {"label": "Every 6 hours", "expression": "0 */6 * * *"},
    {"label": "Every 12 hours", "expression": "0 */12 * * *"},
    {"label": "Daily at 2:00 AM", "expression": "0 2 * * *"},
    {"label": "Daily at midnight", "expression": "0 0 * * *"},
    {"label": "Weekly (Sunday 2:00 AM)", "expression": "0 2 * * 0"},
    {"label": "Monthly (1st at 2:00 AM)", "expression": "0 2 1 * *"},
]

# Cron day-of-week number to systemd day name.
# Cron allows 0-7 where both 0 and 7 mean Sunday.
_DOW_NAMES = {
    "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
    "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
}


def validate_cron_expression(expression: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a 5-field cron expression.

    Returns:
        Tuple of (is_valid, error_message). error_message is None when valid.
    """
    if not expression or not expression.strip():
        return False, "Schedule is required"

    fields = expression.split()
    if len(fields) != 5:
        return False, "Schedule must be a 5-field cron expression (minute hour day month weekday)"

    if not croniter.is_valid(expression):
        return False, f"Invalid cron expression: {expression}"

    # systemd OnCalendar combines day-of-month and day-of-week with AND,
    # while cron combines them with OR. Reject expressions that restrict
    # both fields so behavior is identical on Linux and BSD.
    day_of_month, day_of_week = fields[2], fields[4]
    if day_of_month != "*" and day_of_week != "*":
        return False, (
            "Restricting both day-of-month and day-of-week in the same "
            "schedule is not supported. Use one or the other."
        )

    return True, None


def calculate_next_run(expression: str, base_time: Optional[datetime] = None) -> Optional[str]:
    """
    Calculate the next run time for a cron expression in local time.

    Returns:
        ISO 8601 string of the next run, or None if the expression is invalid.
    """
    try:
        base = base_time or datetime.now()
        itr = croniter(expression, base)
        return itr.get_next(datetime).isoformat()
    except Exception:
        return None


def preview_next_runs(expression: str, count: int = 5) -> List[str]:
    """Return the next several run times as human-readable local strings."""
    try:
        itr = croniter(expression, datetime.now())
        return [
            itr.get_next(datetime).strftime("%Y-%m-%d %H:%M")
            for _ in range(count)
        ]
    except Exception:
        return []


def _cron_field_to_oncalendar(field: str, is_hour_or_minute: bool = False) -> str:
    """
    Convert a single cron field value to systemd OnCalendar syntax.

    Handles wildcards, steps (*/n), lists (a,b,c), and ranges (a-b).
    systemd expresses "every n" as "start/n" instead of "*/n".
    """
    if field == "*":
        return "*"
    if field.startswith("*/"):
        step = field[2:]
        # systemd repetition syntax: first/step
        return f"0/{step}" if is_hour_or_minute else f"1/{step}"
    # Lists and ranges pass through; systemd accepts a,b,c and a..b
    if "-" in field and "," not in field:
        start, end = field.split("-", 1)
        return f"{start}..{end}"
    parts = []
    for part in field.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            parts.append(f"{start}..{end}")
        else:
            parts.append(part)
    return ",".join(parts)


def _cron_dow_to_oncalendar(field: str) -> Optional[str]:
    """
    Convert a cron day-of-week field to systemd day names.

    Returns None for a wildcard (no day-of-week restriction).
    """
    if field == "*":
        return None
    names = []
    for part in field.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            start_name = _DOW_NAMES.get(start)
            end_name = _DOW_NAMES.get(end)
            if not start_name or not end_name:
                return None
            names.append(f"{start_name}..{end_name}")
        else:
            name = _DOW_NAMES.get(part)
            if not name:
                return None
            names.append(name)
    return ",".join(names)


def cron_to_oncalendar(expression: str) -> Optional[str]:
    """
    Convert a validated 5-field cron expression to a systemd OnCalendar value.

    Format produced: [DOW ] *-MM-DD HH:MM:00

    Returns:
        OnCalendar string, or None if the expression cannot be converted.
    """
    is_valid, _ = validate_cron_expression(expression)
    if not is_valid:
        return None

    minute, hour, dom, month, dow = expression.split()

    dow_part = _cron_dow_to_oncalendar(dow)
    month_part = _cron_field_to_oncalendar(month)
    dom_part = _cron_field_to_oncalendar(dom)
    hour_part = _cron_field_to_oncalendar(hour, is_hour_or_minute=True)
    minute_part = _cron_field_to_oncalendar(minute, is_hour_or_minute=True)

    calendar = f"*-{month_part}-{dom_part} {hour_part}:{minute_part}:00"
    if dow_part:
        calendar = f"{dow_part} {calendar}"
    return calendar


def get_schedule_presets() -> List[Dict[str, str]]:
    """Return the list of schedule presets for the UI."""
    return SCHEDULE_PRESETS


def describe_schedule(expression: str) -> str:
    """Return the preset label for an expression, or the raw expression."""
    for preset in SCHEDULE_PRESETS:
        if preset["expression"] == expression:
            return preset["label"]
    return expression
