"""India-time helpers. Instants are stored in UTC; everything shown or spoken is IST."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

IST = ZoneInfo(settings.JARVIS_TIMEZONE)


def now_local() -> datetime:
    return timezone.now().astimezone(IST)


def today_local() -> date:
    return now_local().date()


def combine_local(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = combine_local(d, time(0, 0))
    return start, start + timedelta(days=1)


def to_local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(IST) if dt else None


def fmt_time(dt: datetime) -> str:
    local = to_local(dt)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}" if local.minute else f"{hour} {suffix}"


def fmt_day(d: date) -> str:
    today = today_local()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if d == today - timedelta(days=1):
        return "yesterday"
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


_HI_WEEKDAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
_HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def fmt_time_hi(dt: datetime) -> str:
    """For the Hindi voice: '4 बजे', 'साढ़े 4 बजे', '4:15 बजे' (12-hour, as people say it)."""
    local = to_local(dt)
    hour = local.hour % 12 or 12
    if local.minute == 0:
        return f"{hour} बजे"
    if local.minute == 30:
        return f"साढ़े {hour} बजे"
    return f"{hour}:{local.minute:02d} बजे"


def fmt_day_hi(d: date) -> str:
    today = today_local()
    if d == today:
        return "आज"
    if d == today + timedelta(days=1):
        return "कल"
    if d == today + timedelta(days=2):
        return "परसों"
    if d == today - timedelta(days=1):
        return "कल (बीता हुआ)"
    return f"{_HI_WEEKDAYS[d.weekday()]} {d.day} {_HI_MONTHS[d.month - 1]}"


def due_phrase(due_kind: str, due_date: date | None, due_at: datetime | None) -> str:
    if due_kind == "TIMED" and due_at:
        local = to_local(due_at)
        return f"{fmt_day(local.date())} at {fmt_time(local)}"
    if due_kind == "DATE" and due_date:
        return fmt_day(due_date)
    return ""


def due_phrase_hi(due_kind: str, due_date: date | None, due_at: datetime | None) -> str:
    """'कल 4 बजे' / 'आज' — the Hindi twin of due_phrase."""
    if due_kind == "TIMED" and due_at:
        local = to_local(due_at)
        return f"{fmt_day_hi(local.date())} {fmt_time_hi(local)}"
    if due_kind == "DATE" and due_date:
        return fmt_day_hi(due_date)
    return ""


def minutes_phrase_hi(minutes: int) -> str:
    """'30 मिनट' / '2 घंटे' / '1 घंटा'."""
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} घंटे" if hours > 1 else "1 घंटा"
    return f"{minutes} मिनट"
