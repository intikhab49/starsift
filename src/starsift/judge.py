"""Pure analysis: signal rates per sample, comparison with a control, shared stars, and the verdict."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from .model import GONE, Account, Sighting

YOUNG = timedelta(days=30)
MIN_ACCOUNTS = 30  # below this a sample cannot support a verdict
P_VALUE = 0.01
SHARED_MIN_SHARE = 0.15
SHARED_MIN_ACCOUNTS = 5
SHARED_MIN_CHECKED = 20
SMALL_REPO = 5000  # repos under this many stars have no organic reason to be co-starred en masse


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    min_gap: float  # smallest rise over the control worth flagging
    alone: float | None  # a rise this large is enough for a verdict on its own; None means never


# Young accounts are counted once, under "young". "hollow" and "batch" only look at accounts that
# were at least 30 days old, so a crowd of genuine newcomers cannot trip two signals at once.
# "young" and "hollow" never decide alone: an audience of gamers or first-time users can move
# them. Mass deletions and batch-registered accounts have no organic explanation.
SIGNALS = (
    Signal("young", "account under 30 days old", 0.10, None),
    Signal("hollow", "older account with zero activity", 0.10, None),
    Signal("batch", "older account from a same-day batch", 0.10, 0.30),
    Signal("gone", "account deleted or suspended since", 0.05, 0.30),
)


@dataclass(frozen=True)
class Rate:
    hits: int
    base: int

    @property
    def share(self) -> float:
        return self.hits / self.base if self.base else 0.0


def measure(sightings: list[Sighting], lookup: dict) -> dict[str, Rate]:
    """Count each signal among the accounts behind the sightings."""
    known = []
    for sighting in sightings:
        account = lookup.get(sighting.login)
        if account is None:
            continue  # GitHub would not answer for this login
        if account is not GONE and account.created > sighting.at + timedelta(hours=1):
            account = GONE  # login re-registered after the event: the original account is gone
        known.append((sighting, account))
    alive = [(s, a) for s, a in known if a is not GONE]
    older = [a for s, a in alive if s.at - a.created >= YOUNG]
    days = Counter(a.created.date() for a in older)
    batch_size = max(3, math.ceil(0.02 * len(older)))
    return {
        "young": Rate(len(alive) - len(older), len(alive)),
        "hollow": Rate(sum(_hollow(a) for a in older), len(older)),
        "batch": Rate(sum(days[a.created.date()] >= batch_size for a in older), len(older)),
        "gone": Rate(len(known) - len(alive), len(known)),
    }


def _hollow(account: Account) -> bool:
    """No followers, follows nobody, and has written nothing of its own (forks do not count)."""
    return account.followers == 0 and account.following == 0 and account.repos == 0 and account.gists == 0


def p_greater(a: Rate, b: Rate) -> float:
    """One-sided p-value that a's share exceeds b's (pooled two-proportion z-test)."""
    if not a.base or not b.base:
        return 1.0
    pooled = (a.hits + b.hits) / (a.base + b.base)
    if pooled in (0.0, 1.0):
        return 1.0
    spread = math.sqrt(pooled * (1 - pooled) * (1 / a.base + 1 / b.base))
    return 0.5 * math.erfc((a.share - b.share) / spread / math.sqrt(2))


@dataclass(frozen=True)
class Finding:
    sample: str
    signal: Signal
    target: Rate
    control: Rate

    @property
    def gap(self) -> float:
        return self.target.share - self.control.share

    @property
    def p(self) -> float:
        return p_greater(self.target, self.control)

    @property
    def usable(self) -> bool:
        return self.target.base >= MIN_ACCOUNTS and self.control.base >= MIN_ACCOUNTS

    @property
    def flagged(self) -> bool:
        return self.usable and self.gap >= self.signal.min_gap and self.p < P_VALUE


def compare(sample: str, target: dict[str, Rate], control: dict[str, Rate], skip: tuple[str, ...] = ()) -> list[Finding]:
    return [Finding(sample, s, target[s.key], control[s.key]) for s in SIGNALS if s.key not in skip]


@dataclass(frozen=True)
class Shared:
    """A small repo starred by an unusual share of the same accounts: the mark of a star farm."""

    repo: str
    accounts: int
    of: int
    stars: int


def shared_candidates(recent: dict, repo: str) -> list[tuple[str, int]]:
    """Other repos in at least 15% (and 5) of these accounts' last ten stars, most shared first."""
    lists = [starred for starred in recent.values() if isinstance(starred, tuple)]
    if len(lists) < SHARED_MIN_CHECKED:
        return []
    owner = repo.split("/")[0].lower()
    counts = Counter(
        name for starred in lists for name in set(starred)
        if name.lower() != repo.lower() and name.split("/")[0].lower() != owner
    )
    floor = max(SHARED_MIN_ACCOUNTS, math.ceil(SHARED_MIN_SHARE * len(lists)))
    return [(name, hits) for name, hits in counts.most_common() if hits >= floor]


def shared_small(candidates: list[tuple[str, int]], sizes: dict[str, int], checked: int) -> list[Shared]:
    """Keep the candidates small enough that heavy co-starring has no organic explanation."""
    return [Shared(name, hits, checked, sizes[name]) for name, hits in candidates if sizes.get(name, SMALL_REPO) < SMALL_REPO]


HISTORY = ("archive", "forks")  # samples drawn across the repo's whole life; "live" is the last few hundred events


@dataclass(frozen=True)
class Verdict:
    level: str  # farm, recent, mixed, clean or unknown
    flagged: tuple[Finding, ...]
    shared_history: tuple[Shared, ...] = ()
    shared_recent: tuple[Shared, ...] = ()


def _farm_like(flagged: list[Finding], shared: list[Shared]) -> bool:
    """Two different signals, shared stars plus one signal, or one signal that may decide alone."""
    signals = {f.signal.key for f in flagged}
    decisive = any(f.signal.alone is not None and f.gap >= f.signal.alone for f in flagged)
    return len(signals) >= 2 or decisive or bool(shared and signals)


def decide(findings: list[Finding], shared_history: list[Shared] = (), shared_recent: list[Shared] = ()) -> Verdict:
    """farm when the repo's history carries the signature; recent when only its latest stargazers do.

    Farms star popular repos to make their accounts look real, so farm accounts among a big repo's
    newest stargazers do not mean the repo bought them. That case is kept apart as "recent".
    """
    usable = [f for f in findings if f.usable]
    flagged = tuple(f for f in usable if f.flagged)
    shares = (tuple(shared_history), tuple(shared_recent))
    if not usable:
        return Verdict("unknown", (), *shares)
    if _farm_like([f for f in flagged if f.sample in HISTORY], shared_history):
        level = "farm"
    elif _farm_like([f for f in flagged if f.sample not in HISTORY], shared_recent):
        level = "recent"
    elif flagged or shared_history or shared_recent:
        level = "mixed"
    else:
        level = "clean"
    return Verdict(level, flagged, *shares)


def daily_gains(history: list[tuple[date, int]], end: date, days: int) -> list[int]:
    """Stars gained on each of the `days` days ending at `end`, from a cumulative history."""
    start = end - timedelta(days=days - 1)
    level = next((count for day, count in reversed(history) if day < start), 0)
    totals = dict(history)
    gains = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        if day in totals:
            gains.append(max(0, totals[day] - level))
            level = totals[day]
        else:
            gains.append(0)
    return gains


def busiest_week(history: list[tuple[date, int]]) -> tuple[date, float] | None:
    """First day of the 7-day window that gained the most stars, and that window's share of all stars."""
    if not history or history[-1][1] <= 0:
        return None
    first, last = history[0][0], history[-1][0]
    gains = daily_gains(history, last, (last - first).days + 1)
    total = sum(gains)
    if total <= 0:
        return None
    window = sum(gains[:7])
    best, best_start = window, 0
    for start in range(1, max(1, len(gains) - 6)):
        window += gains[start + 6] - gains[start - 1]
        if window > best:
            best, best_start = window, start
    return first + timedelta(days=best_start), best / total
