"""Shared types: an account seen at a moment, and what GitHub says about that account today."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Sighting:
    """An account starring or forking a repo at a known moment (UTC)."""

    login: str
    at: datetime


@dataclass(frozen=True)
class Account:
    """An account's public profile as GitHub reports it today."""

    login: str
    created: datetime
    followers: int
    following: int
    repos: int  # repositories it owns that are not forks
    gists: int


GONE = "gone"
"""Lookup result for a login GitHub no longer resolves: deleted, suspended or renamed."""


def parse_time(value: str) -> datetime:
    """Parse GitHub's ISO 8601 or ClickHouse's 'YYYY-MM-DD HH:MM:SS' as an aware UTC datetime."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
