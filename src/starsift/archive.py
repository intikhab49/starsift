"""Public archives: the GH Archive mirror on ClickHouse's playground, and OSS Insight's star history.

Both are best-effort. When either is down, starsift carries on without it.
"""

from __future__ import annotations

import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .github import NETWORK_ERRORS, fetch_json
from .model import Sighting, parse_time

CLICKHOUSE = "https://play.clickhouse.com/"
HISTORY = "https://api.ossinsight.io/v1/repos/{repo}/stargazers/history/?per=day"
EVENTS = {"star": "WatchEvent", "fork": "ForkEvent"}
PER_HOUR_CAP = 100


def query(sql: str, **params: str) -> list[list]:
    """Run read-only SQL on ClickHouse's public playground. Values travel as typed parameters."""
    arguments = {"user": "play", "default_format": "JSONCompact"}
    arguments.update({f"param_{key}": value for key, value in params.items()})
    url = f"{CLICKHOUSE}?{urllib.parse.urlencode(arguments)}"
    return fetch_json(url, data=sql.encode(), timeout=90, tries=2)["data"]


def sample(repo: str, size: int) -> tuple[int, list[Sighting]]:
    """How many star events the archive holds for the repo, and a random sample of them."""
    where = "event_type = 'WatchEvent' AND repo_name = {repo:String}"
    total = int(query(f"SELECT count() FROM github_events WHERE {where}", repo=repo)[0][0])
    rows = query(
        f"SELECT actor_login, toString(created_at, 'UTC') FROM github_events WHERE {where} "
        f"ORDER BY rand() LIMIT {int(size)}",
        repo=repo,
    )
    return total, [Sighting(login, parse_time(at)) for login, at in rows]


def matched(sightings: list[Sighting], repo: str, kind: str) -> list[Sighting]:
    """Pair each sighting with a random account that did the same to another repo in the same hour."""
    need = Counter(_hour(s.at) for s in sightings)
    if not need:
        return []
    nearby = {hour + timedelta(hours=step) for hour in need for step in (-1, 0, 1)}
    hours = ", ".join(f"'{hour:%Y-%m-%d %H}:00:00'" for hour in sorted(nearby))
    per_hour = min(max(need.values()) + 2, PER_HOUR_CAP)
    # A random sample per hour, drawn while scanning. Sorting every event in those hours
    # by rand() runs the public server out of memory on samples spread over years.
    rows = query(
        f"SELECT groupArraySample({per_hour})((actor_login, toString(created_at, 'UTC'))) "
        f"FROM github_events WHERE event_type = '{EVENTS[kind]}' "
        f"AND toStartOfHour(created_at, 'UTC') IN ({hours}) AND repo_name != {{repo:String}} "
        "GROUP BY toStartOfHour(created_at, 'UTC')",
        repo=repo,
    )
    pool: dict[datetime, list[Sighting]] = defaultdict(list)
    for (events,) in rows:
        for login, at in events:
            moment = parse_time(at)
            pool[_hour(moment)].append(Sighting(login, moment))
    used = {s.login for s in sightings}  # the control must not reuse the accounts it is compared with
    picked = []
    for sighting in sightings:
        hour = _hour(sighting.at)
        for slot in (hour, hour - timedelta(hours=1), hour + timedelta(hours=1)):
            match = _take(pool[slot], used)
            if match:
                picked.append(match)
                break
    return picked


def history(repo: str) -> list[tuple[date, int]] | None:
    """Cumulative stars per day from OSS Insight, or None when it is unavailable."""
    try:
        rows = fetch_json(HISTORY.format(repo=repo), timeout=30, tries=2)["data"]["rows"]
        return [(date.fromisoformat(row["date"]), int(float(row["stargazers"]))) for row in rows]
    except (*NETWORK_ERRORS, KeyError, TypeError):
        return None


def _take(candidates: list[Sighting], used: set[str]) -> Sighting | None:
    while candidates:
        candidate = candidates.pop()
        if candidate.login not in used:
            used.add(candidate.login)
            return candidate
    return None


def _hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)
