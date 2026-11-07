"""Explicit session calendars: never infer a trading day from a missing date."""
from datetime import date


def checked_calendar(values):
    if values is None or isinstance(values, (str, bytes, dict)):
        raise ValueError("An explicit nonempty session calendar is required")
    sessions = list(values)
    if not sessions:
        raise ValueError("An explicit nonempty session calendar is required")
    for value in sessions:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("Calendar dates must use canonical YYYY-MM-DD")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise ValueError("Session calendar must be strictly ordered, without duplicates")
    return sessions


def extended_calendar(retained, extension=None):
    """Append an explicitly confirmed future list anchored at the last retained day.

    The caller supplies market-calendar facts; this does not certify their source.
    The known prefix cannot be replaced, shortened or silently skipped.
    """
    sessions = checked_calendar(retained)
    if extension is None:
        return sessions
    extra = checked_calendar(extension)
    if extra[0] != sessions[-1]:
        raise ValueError("Calendar extension must start at the last retained session")
    return sessions + extra[1:]


def require_next_session(calendar, reference_date, trade_date):
    checked_calendar([reference_date, trade_date])
    if reference_date not in calendar or trade_date not in calendar:
        raise ValueError("Date not in explicit session calendar; supply a confirmed calendar extension")
    if calendar.index(trade_date) != calendar.index(reference_date) + 1:
        raise ValueError("Skipped market session")
