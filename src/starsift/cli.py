"""Command line: `starsift OWNER/REPO`."""

from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__, archive, judge
from .github import (
    GitHub,
    StarsiftError,
    find_token,
    fork_owners,
    live_stars,
    parse_repo,
    profiles,
    recent_stars,
    repo_info,
    star_counts,
)
from .model import Sighting

DEFAULT_CONTROL = "ollama/ollama"
SHARED_ACCOUNTS = 100
TITLES = {"live": "live stargazers", "archive": "archived stars", "forks": "fork owners"}
VERDICTS = {
    "farm": "farm signature",
    "recent": "recent farm activity",
    "mixed": "mixed signals",
    "clean": "no farm signature",
    "unknown": "not enough data",
}
PHRASES = {
    "young": "of accounts were under 30 days old at the time",
    "hollow": "of older accounts have no followers, follow nobody and own no repos or gists",
    "batch": "of older accounts were created in same-day batches",
    "gone": "of accounts have been deleted or suspended since",
}
SPARKS = "▁▂▃▄▅▆▇█"
ASCII_SPARKS = "_.-:=+*#"
ARCHIVE_ERRORS = (OSError, ValueError, KeyError, IndexError)


@dataclass
class Report:
    repo: str
    stars: int
    forks: int
    checked: datetime
    control: str
    samples: dict[str, tuple[list[Sighting], list[Sighting]]]
    findings: list[judge.Finding]
    verdict: judge.Verdict
    shared_checked: dict[str, int]  # per era ("history", "recent"): stargazers whose latest stars were read
    history: list | None = None
    archive_events: int | None = None
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def audit(repo: str, *, size: int, use_archive: bool, control_repo: str | None, say) -> Report:
    gh = GitHub(find_token())
    info = repo_info(gh, repo)
    name = info["name"]
    say(f"{name}: {_count(info['stars'], 'star')}, {_count(info['forks'], 'fork')}")
    notes: list[str] = []
    archived: list[Sighting] = []
    archive_events = None
    with ThreadPoolExecutor(max_workers=4) as pool:
        history_job = pool.submit(archive.history, name)
        live_job = pool.submit(live_stars, gh, name)
        forks_job = pool.submit(fork_owners, gh, name, info["forks"], size)
        archive_job = pool.submit(archive.sample, name, size) if use_archive else None
        live = _distinct(live_job.result())[:size]
        say(f"live feed: {len(live)} recent stargazers")
        forks = _distinct(forks_job.result())
        say(f"forks: {len(forks)} owners sampled")
        if archive_job:
            try:
                archive_events, archived = archive_job.result()
                archived = _distinct(archived)
                say(f"archive: {archive_events:,} star events on record, {len(archived)} sampled")
            except ARCHIVE_ERRORS as error:
                notes.append(f"The GH Archive mirror was unavailable ({_short(error)}).")
                say(notes[-1])
        history = history_job.result()

    groups: dict[str, list[Sighting]] = {}
    skipped: list[str] = []
    for key, target in (("live", live), ("archive", archived), ("forks", forks)):
        if len(target) >= judge.MIN_ACCOUNTS:
            groups[key] = target
        elif key != "archive" or archive_events is not None:
            skipped.append(f"{TITLES[key]} ({f'only {len(target)}' if target else 'none'} found)")

    controls: dict[str, list[Sighting]] = {}
    if groups and archive_events is not None:
        try:
            for key, kind in (("archive", "star"), ("live", "star"), ("forks", "fork")):
                if key in groups and (key == "archive" or not control_repo):
                    controls[key] = archive.matched(groups[key], name, kind)
        except ARCHIVE_ERRORS as error:
            controls.clear()
            notes.append(f"Could not draw a control group from the archive ({_short(error)}).")
            say(notes[-1])
    sources = []
    if controls:
        sources.append("random accounts that starred or forked other repos in the same hours")
    if any(key in groups and key not in controls for key in ("live", "forks")):
        other = repo_info(gh, parse_repo(control_repo or DEFAULT_CONTROL))
        if "live" in groups:
            controls["live"] = _distinct(live_stars(gh, other["name"]))[:size]
        if "forks" in groups:
            controls["forks"] = _distinct(fork_owners(gh, other["name"], other["forks"], size))
        sources.insert(0, f"recent stargazers and fork owners of {other['name']}")
    control = "; archived stars against ".join(sources) if sources else "none (no sample was large enough)"

    everyone = {s.login for group in [*groups.values(), *controls.values()] for s in group}
    readers = _readers(groups)
    to_read = [login for logins in readers.values() for login in logins]
    say(f"looking up {len(everyone):,} accounts" + (f" and the latest stars of {len(to_read)}" if to_read else ""))
    with ThreadPoolExecutor(max_workers=2) as pool:
        recent_job = pool.submit(recent_stars, gh, to_read) if to_read else None
        lookup = profiles(gh, everyone)
        recents = recent_job.result() if recent_job else {}

    samples: dict[str, tuple[list[Sighting], list[Sighting]]] = {}
    findings: list[judge.Finding] = []
    for key, target in groups.items():
        paired = controls.get(key, [])
        samples[key] = (target, paired)
        skip = ("gone",) if key == "forks" else ()  # a deleted account's forks vanish with it
        findings += judge.compare(key, judge.measure(target, lookup), judge.measure(paired, lookup), skip)

    checked: dict[str, int] = {}
    candidates: dict[str, list[tuple[str, int]]] = {}
    for era, logins in readers.items():
        lists = {login: recents.get(login) for login in logins}
        checked[era] = sum(isinstance(starred, tuple) for starred in lists.values())
        candidates[era] = judge.shared_candidates(lists, name)
    wanted = list(dict.fromkeys(repo for found in candidates.values() for repo, _ in found[:20]))
    sizes = star_counts(gh, wanted) if wanted else {}
    shared = {era: judge.shared_small(found, sizes, checked[era]) for era, found in candidates.items()}

    return Report(
        repo=name,
        stars=info["stars"],
        forks=info["forks"],
        checked=datetime.now(timezone.utc),
        control=control,
        samples=samples,
        findings=findings,
        verdict=judge.decide(findings, shared.get("history", []), shared.get("recent", [])),
        shared_checked=checked,
        history=history,
        archive_events=archive_events,
        skipped=skipped,
        notes=notes,
    )


def _readers(groups: dict[str, list[Sighting]]) -> dict[str, list[str]]:
    """Stargazers whose latest stars get read: up to half recent ones, the rest from the repo's history."""
    live = list(dict.fromkeys(s.login for s in groups.get("live", [])))
    seen = set(live)
    old = [login for login in dict.fromkeys(s.login for s in groups.get("archive", [])) if login not in seen]
    recent = live[: max(SHARED_ACCOUNTS // 2, SHARED_ACCOUNTS - len(old))]
    earlier = old[: SHARED_ACCOUNTS - len(recent)]
    picked = {"recent": recent, "history": earlier}
    return {era: logins for era, logins in picked.items() if len(logins) >= judge.SHARED_MIN_CHECKED}


def _distinct(sightings: list[Sighting]) -> list[Sighting]:
    """Keep each account's first sighting only."""
    seen: set[str] = set()
    kept = []
    for sighting in sightings:
        if sighting.login not in seen:
            seen.add(sighting.login)
            kept.append(sighting)
    return kept


def _short(error: Exception) -> str:
    return str(getattr(error, "reason", None) or error)[:80]


def render(report: Report, fancy: bool = True) -> str:
    lines = [f"starsift {__version__}  {report.repo}  {_count(report.stars, 'star')}  {_count(report.forks, 'fork')}"]
    if report.history:
        gains = judge.daily_gains(report.history, report.history[-1][0], 30)  # today is not over yet
        line = f"stars per day, last 30 days  {_spark(gains, SPARKS if fancy else ASCII_SPARKS)}"
        week = judge.busiest_week(report.history)
        if week:
            line += f"  busiest week (from {_day(week[0], report.checked.year)}) brought {week[1]:.0%} of all stars"
        lines.append(line)
    lines.append("")
    if report.samples:
        lines += _table(report)
        lines.append("")
    lines += _shared_lines(report)
    lines.append("")
    lines += _verdict_lines(report)
    lines.append("")
    lines.append(f"Control: {report.control}.")
    if report.skipped:
        lines.append(f"Skipped: {', '.join(report.skipped)}; a sample needs {judge.MIN_ACCOUNTS} accounts.")
    lines += report.notes
    lines.append("Limits: only the sampled windows are checked, and GitHub hides activity from accounts it has flagged.")
    return "\n".join(lines)


def _table(report: Report) -> list[str]:
    keys = list(report.samples)
    by_cell = {(f.sample, f.signal.key): f for f in report.findings}
    out = [" " * 36 + "".join(f"{TITLES[k]:>18}   " for k in keys)]
    out.append(" " * 36 + "".join(f"{'repo':>7} {'control':>10}   " for _ in keys))
    out.append(f"{'accounts checked':36}" + "".join(
        f"{len(report.samples[k][0]):>7} {len(report.samples[k][1]):>10}   " for k in keys))
    for signal in judge.SIGNALS:
        row = f"{signal.label:36}"
        for key in keys:
            finding = by_cell.get((key, signal.key))
            if finding is None:
                row += f"{'-':>7} {'-':>10}   "
                continue
            mark = "!" if finding.flagged else " "
            control = f"{finding.control.share:.0%}" if finding.control.base else "-"
            row += f"{finding.target.share:>7.0%}{mark}{control:>10}   "
        out.append(row)
    out.append(f"{'sampled from':36}" + "".join(
        f"{_span(report.samples[k][0], report.checked):>18}   " for k in keys))
    return [line.rstrip() for line in out]


ERAS = {"history": "stargazers from the repo's history", "recent": "recent stargazers"}


def _shared_lines(report: Report) -> list[str]:
    lines = []
    for era, label in ERAS.items():
        checked = report.shared_checked.get(era, 0)
        if checked < judge.SHARED_MIN_CHECKED:
            continue
        shared = report.verdict.shared_history if era == "history" else report.verdict.shared_recent
        if not shared:
            lines.append(f"Shared stars, {checked} {label}: none; their latest stars do not cluster on a small repo.")
            continue
        lines.append(f"Shared stars, {checked} {label}: small repos many of them starred recently")
        lines += [f"  {s.accounts / s.of:>4.0%}  {s.repo} ({s.stars:,} stars)" for s in shared]
    return lines or ["Shared stars: not checked (too few stargazers' latest stars could be read)."]


def _verdict_lines(report: Report) -> list[str]:
    verdict = report.verdict
    lines = [f"Verdict: {VERDICTS[verdict.level]}."]
    if verdict.level == "clean":
        lines.append("  The accounts behind these stars look like ordinary GitHub users next to the control group.")
    elif verdict.level == "unknown":
        lines.append("  Every sample has fewer than 30 accounts with a control to compare against.")
    elif verdict.level == "recent":
        lines.append("  The repo's earlier stars look normal, but some of its latest stargazers look like farm accounts.")
    for finding in verdict.flagged:
        lines.append(
            f"  {TITLES[finding.sample]}: {finding.target.share:.0%} {PHRASES[finding.signal.key]}, "
            f"against {finding.control.share:.0%} of the control (p {_p(finding.p)})."
        )
    for era, shared in (("history", verdict.shared_history), ("recent", verdict.shared_recent)):
        if shared:
            shares = [s.accounts / s.of for s in shared]
            spread = f"{min(shares):.0%}" if len(shares) == 1 else f"{min(shares):.0%} to {max(shares):.0%}"
            count = "1 small repo is" if len(shares) == 1 else f"{len(shares)} small repos are each"
            lines.append(f"  {count} starred by {spread} of the same {ERAS[era]}, the pattern a star farm leaves.")
    if verdict.level == "recent":
        lines.append("  Farms also star popular repos to make their accounts look real, so this alone does not mean")
        lines.append("  the repo bought stars." + (" The small repos above are likelier customers." if verdict.shared_recent else ""))
    if verdict.level == "mixed":
        lines.append("  One signal on its own is not proof; read the table before drawing conclusions.")
    return lines


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _p(value: float) -> str:
    return "< 0.001" if value < 0.001 else f"= {value:.3f}"


def _span(sightings: list[Sighting], now: datetime) -> str:
    first = min(s.at for s in sightings)
    last = max(s.at for s in sightings)
    if (now - first).total_seconds() < 2 * 3600:
        return f"last {max(1, math.ceil((now - first).total_seconds() / 60))} min"
    if first.year == last.year == now.year:
        return f"{_day(first)} - {_day(last)}"
    months = (f"{first:%b %Y}", f"{last:%b %Y}")
    return months[0] if months[0] == months[1] else f"{months[0]} - {months[1]}"


def _day(moment, year: int | None = None) -> str:
    """'Oct 5', with the year added when it differs from `year`."""
    text = f"{moment:%b} {moment.day}"
    return text if year in (None, moment.year) else f"{text}, {moment.year}"


def _spark(values: list[int], ramp: str) -> str:
    top = max(values, default=0)
    if top <= 0:
        return ramp[0] * len(values)
    return "".join(ramp[min(len(ramp) - 1, round(value / top * (len(ramp) - 1)))] for value in values)


def to_json(report: Report) -> dict:
    week = judge.busiest_week(report.history) if report.history else None
    return {
        "repo": report.repo,
        "stars": report.stars,
        "forks": report.forks,
        "checked_at": report.checked.isoformat(timespec="seconds"),
        "verdict": report.verdict.level,
        "control": report.control,
        "archive_star_events": report.archive_events,
        "busiest_week": {"start": week[0].isoformat(), "share": round(week[1], 4)} if week else None,
        "samples": {
            key: {
                "accounts": len(target),
                "control_accounts": len(paired),
                "from": min(s.at for s in target).isoformat(timespec="seconds"),
                "to": max(s.at for s in target).isoformat(timespec="seconds"),
            }
            for key, (target, paired) in report.samples.items()
        },
        "signals": [
            {
                "sample": f.sample,
                "signal": f.signal.key,
                "repo": {"hits": f.target.hits, "of": f.target.base},
                "control": {"hits": f.control.hits, "of": f.control.base},
                "p_value": round(f.p, 6),
                "flagged": f.flagged,
            }
            for f in report.findings
        ],
        "shared_stars": {
            era: [{"repo": s.repo, "accounts": s.accounts, "of": s.of, "stars": s.stars} for s in shared]
            for era, shared in (("history", report.verdict.shared_history), ("recent", report.verdict.shared_recent))
        },
        "shared_checked": report.shared_checked,
        "skipped": report.skipped,
        "notes": report.notes,
    }


def _fancy(stream) -> bool:
    try:
        SPARKS.encode(stream.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="starsift",
        description="Check whether the accounts behind a GitHub repo's stars look real.",
        epilog="Exit status: 1 for a farm signature in the repo's history, 2 for an error, otherwise 0.",
    )
    parser.add_argument("repo", help="owner/name or a github.com URL")
    parser.add_argument("--sample", type=int, default=200, metavar="N", help="accounts per sample, 30-1000 (default 200)")
    parser.add_argument("--control", metavar="OWNER/NAME",
                        help="compare live stargazers and fork owners with this repo's instead of random accounts")
    parser.add_argument("--no-archive", action="store_true", help="skip the public GH Archive mirror")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--version", action="version", version=f"starsift {__version__}")
    args = parser.parse_args(argv)
    if not 30 <= args.sample <= 1000:
        parser.error("--sample must be between 30 and 1000")

    def say(message: str) -> None:
        print(f"  {message}", file=sys.stderr, flush=True)

    repo = None
    try:
        repo = parse_repo(args.repo)
        report = audit(repo, size=args.sample, use_archive=not args.no_archive, control_repo=args.control, say=say)
    except StarsiftError as error:
        print(f"starsift: {repo + ': ' if repo else ''}{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:  # never let a crash exit with 1, which means "farm signature"
        traceback.print_exc()
        print("starsift: unexpected error (details above)", file=sys.stderr)
        return 2
    print(json.dumps(to_json(report), indent=2) if args.json else render(report, _fancy(sys.stdout)))
    return 1 if report.verdict.level == "farm" else 0
