"""Daily AI counters: every Claude call is counted per user, and internet questions have a daily cap."""
from django.conf import settings
from django.db.models import F, Sum

from . import timeutils as tu
from .models import AiUsage


def _int(value) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def record(user, kind: str, response=None) -> None:
    """Count one model call (tokens and web searches come from the response's usage block)."""
    if user is None or not getattr(user, "pk", None):
        return
    usage = getattr(response, "usage", None)
    input_tokens = sum(_int(getattr(usage, name, 0)) for name in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    output_tokens = _int(getattr(usage, "output_tokens", 0))
    searches = _int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0))
    row, _ = AiUsage.objects.get_or_create(user=user, day=tu.today_local(), kind=kind)
    AiUsage.objects.filter(pk=row.pk).update(
        requests=F("requests") + 1,
        input_tokens=F("input_tokens") + input_tokens,
        output_tokens=F("output_tokens") + output_tokens,
        web_searches=F("web_searches") + searches,
    )


def answers_today(user) -> int:
    row = AiUsage.objects.filter(user=user, day=tu.today_local(), kind=AiUsage.Kind.ANSWER).first()
    return row.requests if row else 0


def over_answer_limit(user) -> bool:
    return answers_today(user) >= settings.JARVIS_AI_DAILY_ANSWER_LIMIT


def month_summary(user) -> dict:
    today = tu.today_local()
    rows = AiUsage.objects.filter(user=user, day__year=today.year, day__month=today.month)
    totals = rows.aggregate(requests=Sum("requests"), input_tokens=Sum("input_tokens"),
                            output_tokens=Sum("output_tokens"), web_searches=Sum("web_searches"))
    by_kind = {k: 0 for k, _ in AiUsage.Kind.choices}
    for row in rows.values("kind").annotate(n=Sum("requests")):
        by_kind[row["kind"]] = row["n"] or 0
    return {
        "month": today.strftime("%B %Y"),
        "requests": totals["requests"] or 0,
        "input_tokens": totals["input_tokens"] or 0,
        "output_tokens": totals["output_tokens"] or 0,
        "web_searches": totals["web_searches"] or 0,
        "by_kind": by_kind,
        "questions_today": answers_today(user),
        "daily_question_limit": settings.JARVIS_AI_DAILY_ANSWER_LIMIT,
    }
