"""GitHub access: token discovery, REST with retries, and batched GraphQL lookups."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import __version__
from .model import GONE, Account, Sighting, parse_time

API = "https://api.github.com"
USER_AGENT = f"starsift/{__version__} (+https://github.com/intikhab49/starsift)"
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
RETRY = {500, 502, 503, 504}
# Dropped connections and truncated bodies surface as any of these, often mid-read.
NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)

# Counting PRs and issues doubles the cost of a lookup, so activity is read from cheaper counts.
PROFILE = (
    "login createdAt followers { totalCount } following { totalCount } "
    "repositories(ownerAffiliations: OWNER, isFork: false) { totalCount } gists { totalCount }"
)
RECENT_STARS = (
    "starredRepositories(first: 6, orderBy: {field: STARRED_AT, direction: DESC}) "
    "{ nodes { nameWithOwner } }"
)
WORKERS = 6


class StarsiftError(Exception):
    """A failure the user can act on. The CLI prints it without a traceback."""


def fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None,
               timeout: float = 60, tries: int = 3):
    """Fetch a JSON document (POST when data is given), retrying transient failures."""
    for attempt in range(tries):
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in RETRY or attempt + 1 == tries:
                raise
        except NETWORK_ERRORS:
            if attempt + 1 == tries:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def find_token() -> str:
    """Use GITHUB_TOKEN or GH_TOKEN, else ask the GitHub CLI."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    gh = shutil.which("gh")
    if gh:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise StarsiftError(
        "needs a GitHub token: set GITHUB_TOKEN or run `gh auth login`. Any token works; it needs no scopes."
    )


def parse_repo(text: str) -> str:
    """Accept owner/name or a github.com URL and return owner/name."""
    value = text.strip()
    for prefix in ("https://", "http://", "www.", "github.com/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    parts = [part for part in value.split("/") if part][:2]
    candidate = "/".join(parts).removesuffix(".git")
    if not REPO.fullmatch(candidate):
        raise StarsiftError(f"not a GitHub repo: {text!r} (expected owner/name or a github.com URL)")
    return candidate


class GitHub:
    """Authenticated REST and GraphQL calls."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def rest(self, path: str):
        for attempt in range(2):
            try:
                return fetch_json(API + path, headers=self._headers)
            except urllib.error.HTTPError as error:
                if attempt == 0 and _wait_for_secondary_limit(error):
                    continue
                raise _explain(error) from None
            except NETWORK_ERRORS as error:
                raise StarsiftError(f"could not reach GitHub: {getattr(error, 'reason', error)}") from None
        raise AssertionError("unreachable")

    def graphql(self, query: str, variables: dict) -> dict:
        """Run a query. GitHub giving up on a heavy query comes back as an error, not an exception."""
        body = json.dumps({"query": query, "variables": variables}).encode()
        for attempt in range(2):
            try:
                return fetch_json(API + "/graphql", data=body, headers=self._headers, timeout=90, tries=1)
            except urllib.error.HTTPError as error:
                if error.code in RETRY:
                    return {"errors": [{"message": f"Resource limits: HTTP {error.code}"}]}
                if attempt == 0 and _wait_for_secondary_limit(error):
                    continue
                raise _explain(error) from None
            except NETWORK_ERRORS as error:
                return {"errors": [{"message": f"Resource limits: {error}"}]}
        raise AssertionError("unreachable")


def _wait_for_secondary_limit(error: urllib.error.HTTPError) -> bool:
    """Sleep through a short secondary rate limit; report whether a retry makes sense."""
    retry_after = error.headers.get("retry-after") if error.headers else None
    if error.code in (403, 429) and retry_after and retry_after.isdigit() and int(retry_after) <= 120:
        time.sleep(int(retry_after) + 1)
        return True
    return False


def _explain(error: urllib.error.HTTPError) -> StarsiftError:
    if error.code == 401:
        return StarsiftError("GitHub rejected the token (HTTP 401). Check GITHUB_TOKEN or run `gh auth login`.")
    if error.code in (403, 429) and error.headers.get("x-ratelimit-remaining") == "0":
        reset = error.headers.get("x-ratelimit-reset")
        when = time.strftime("%H:%M UTC", time.gmtime(int(reset))) if reset and reset.isdigit() else "later"
        return StarsiftError(f"GitHub rate limit reached; it resets at {when}.")
    if error.code == 404:
        return StarsiftError("not found on GitHub (private, deleted or misspelled)")
    return StarsiftError(f"GitHub answered HTTP {error.code} for {error.url}")


def repo_info(gh: GitHub, repo: str) -> dict:
    data = gh.rest(f"/repos/{repo}")
    return {
        "name": data["full_name"],
        "stars": data["stargazers_count"],
        "forks": data["forks_count"],
        "created": parse_time(data["created_at"]),
    }


def live_stars(gh: GitHub, repo: str) -> list[Sighting]:
    """Accounts that starred the repo within its public event feed (the last 300 events)."""
    events: list[dict] = []
    for page in (1, 2, 3):
        try:
            batch = gh.rest(f"/repos/{repo}/events?per_page=100&page={page}")
        except StarsiftError:
            if page == 1:
                raise
            break
        events += batch
        if len(batch) < 100:
            break
    return [Sighting(e["actor"]["login"], parse_time(e["created_at"])) for e in events if e.get("type") == "WatchEvent"]


def fork_pages(total: int, wanted: int, points: int = 4) -> tuple[int, list[int]]:
    """Page size and page numbers that spread a fork sample from newest to oldest."""
    if total <= wanted:
        return 100, list(range(1, math.ceil(total / 100) + 1)) or [1]
    per_page = min(100, math.ceil(wanted / points))
    last = math.ceil(total / per_page)
    return per_page, sorted({round(1 + i * (last - 1) / (points - 1)) for i in range(points)})


def fork_owners(gh: GitHub, repo: str, total: int, wanted: int) -> list[Sighting]:
    """Personal accounts that forked the repo, sampled across its whole fork history."""
    if total == 0:
        return []
    per_page, pages = fork_pages(total, wanted)

    def page_of(page: int) -> list[dict]:
        try:
            return gh.rest(f"/repos/{repo}/forks?sort=newest&per_page={per_page}&page={page}")
        except StarsiftError:
            return []

    with ThreadPoolExecutor(max_workers=len(pages)) as pool:
        forks = [fork for batch in pool.map(page_of, pages) for fork in batch]
    return [Sighting(f["owner"]["login"], parse_time(f["created_at"])) for f in forks if f["owner"]["type"] == "User"]


def profiles(gh: GitHub, logins) -> dict[str, Account | str | None]:
    """Each login's profile today: an Account, GONE, or None when GitHub would not answer."""
    return _lookup(gh, logins, PROFILE, _account, batch=50)


def recent_stars(gh: GitHub, logins) -> dict[str, tuple[str, ...] | str | None]:
    """The last ten repos each account starred."""
    return _lookup(gh, logins, RECENT_STARS, _starred, batch=20)


def star_counts(gh: GitHub, repos: list[str]) -> dict[str, int]:
    """Today's star count for each owner/name; repos that no longer exist are left out."""
    valid = [repo for repo in repos if REPO.fullmatch(repo)]
    if not valid:
        return {}
    declared = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(len(valid)))
    selected = " ".join(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ stargazerCount }}" for i in range(len(valid)))
    variables = {}
    for i, repo in enumerate(valid):
        variables[f"o{i}"], variables[f"n{i}"] = repo.split("/")
    data = gh.graphql(f"query({declared}) {{ {selected} }}", variables).get("data") or {}
    return {repo: data[f"r{i}"]["stargazerCount"] for i, repo in enumerate(valid) if data.get(f"r{i}")}


def _account(node: dict) -> Account:
    return Account(
        login=node["login"],
        created=parse_time(node["createdAt"]),
        followers=node["followers"]["totalCount"],
        following=node["following"]["totalCount"],
        repos=node["repositories"]["totalCount"],
        gists=node["gists"]["totalCount"],
    )


def _starred(node: dict) -> tuple[str, ...]:
    return tuple(repo["nameWithOwner"] for repo in node["starredRepositories"]["nodes"] if repo)


def _lookup(gh: GitHub, logins, fields: str, parse: Callable, batch: int, workers: int = WORKERS) -> dict:
    unique = sorted({login for login in logins if LOGIN.fullmatch(login)})
    chunks = [unique[i : i + batch] for i in range(0, len(unique), batch)]
    found: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(lambda chunk: _resolve(gh, chunk, fields, parse), chunks):
            found.update(part)
    return found


def _resolve(gh: GitHub, logins: list[str], fields: str, parse: Callable) -> dict:
    """Look up a batch. GitHub refuses heavy batches outright, so halve until each half fits."""
    declared = ", ".join(f"$l{i}: String!" for i in range(len(logins)))
    selected = " ".join(f"u{i}: user(login: $l{i}) {{ {fields} }}" for i in range(len(logins)))
    reply = gh.graphql(f"query({declared}) {{ {selected} }}", {f"l{i}": login for i, login in enumerate(logins)})
    errors = reply.get("errors") or []
    if any(error.get("type") != "NOT_FOUND" for error in errors):
        if len(logins) == 1:
            return {logins[0]: None}
        half = len(logins) // 2
        return {**_resolve(gh, logins[:half], fields, parse), **_resolve(gh, logins[half:], fields, parse)}
    data = reply.get("data") or {}
    return {login: parse(data[f"u{i}"]) if data.get(f"u{i}") else GONE for i, login in enumerate(logins)}
