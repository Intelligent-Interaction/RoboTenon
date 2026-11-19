"""Shared budgets and timestamp normalization for history endpoints."""

from datetime import datetime, timezone


def page_window(limit=100, offset=0, since=None, until=None):
    limit, offset = int(limit), int(offset)
    if not 1 <= limit <= 500 or not 0 <= offset <= 100000:
        raise ValueError(
            "limit must be 1..500 and offset 0..100000; narrow the time window for older history"
        )

    def timestamp(value):
        if value in (None, ""):
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    since, until = timestamp(since), timestamp(until)
    if since and until and since > until:
        raise ValueError("since must not be later than until")
    return limit, offset, since, until
