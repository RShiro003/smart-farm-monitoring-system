"""Shared local date/time conditions for history tables and CSV downloads."""
from datetime import datetime, timedelta


def _date(value):
    value = (value or "").strip()
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


def _time(value):
    value = (value or "").strip()
    if not value:
        return None
    format_string = "%H:%M:%S" if value.count(":") == 2 else "%H:%M"
    return datetime.strptime(value, format_string).strftime(format_string)


def datetime_conditions(expression, date=None, time_from=None, time_to=None,
                        date_from=None, date_to=None):
    """Date bounds select days; time bounds select the same clock interval daily.

    HH:MM end bounds include the entire minute; HH:MM:SS includes that second.
    Invalid/reversed bounds match nothing, rather than broadening a download.
    The SQL expression must be a trusted constant, never request input.
    """
    try:
        day, start_day, end_day = _date(date), _date(date_from), _date(date_to)
        start_time, end_time = _time(time_from), _time(time_to)
    except (TypeError, ValueError, AttributeError):
        return ["0 = 1"], []
    if day:
        start_day = end_day = day
    if start_day and end_day and start_day > end_day:
        return ["0 = 1"], []
    if start_time and end_time:
        lower = start_time if len(start_time) == 8 else start_time + ":00"
        upper = end_time if len(end_time) == 8 else end_time + ":59"
        if lower > upper:
            return ["0 = 1"], []

    clauses, params = [], []
    if start_day:
        clauses.append(f"{expression} >= ?")
        params.append(f"{start_day.isoformat()} 00:00:00")
    if end_day:
        if end_day.year == 9999 and end_day.month == 12 and end_day.day == 31:
            clauses.append(f"{expression} <= ?")
            params.append("9999-12-31 23:59:59.999999")
        else:
            clauses.append(f"{expression} < ?")
            params.append(f"{(end_day + timedelta(days=1)).isoformat()} 00:00:00")
    for value, operator in ((start_time, ">="), (end_time, "<=")):
        if value:
            # Compare the recorded local clock, without SQLite timezone conversion.
            clauses.append(f"substr({expression}, 12, {len(value)}) {operator} ?")
            params.append(value)
    return clauses, params
