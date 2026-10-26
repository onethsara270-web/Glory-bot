from datetime import datetime, timezone

from glorybot.config import get_settings


def fmt_dt(dt: datetime | None) -> str:
    """12-hour local (Nepal) time, e.g. '11 Oct 2026, 09:15 PM'."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC datetimes
    return dt.astimezone(get_settings().tz).strftime("%d %b %Y, %I:%M %p")
