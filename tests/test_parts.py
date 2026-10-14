import json
from datetime import date, datetime, timezone

import pytest

from starsift import archive, cli, github, judge
from starsift.github import StarsiftError, fork_pages, parse_repo
from starsift.judge import Rate
from starsift.model import GONE, Sighting, parse_time


@pytest.mark.parametrize("text", [
    "morluto/rea",
    "https://github.com/morluto/rea",
    "github.com/morluto/rea/",
    "https://www.github.com/morluto/rea.git",
    "https://github.com/morluto/rea/tree/main/src",
])
def test_parse_repo_accepts_common_forms(text):
    assert parse_repo(text) == "morluto/rea"


@pytest.mark.parametrize("text", ["morluto", "https://gitlab.com/a/b", "a b/c", ""])
def test_parse_repo_rejects_other_input(text):
    with pytest.raises(StarsiftError):
        parse_repo(text)


def test_parse_time_reads_github_and_clickhouse_formats():
    assert parse_time("2026-10-13T23:37:44Z") == datetime(2026, 10, 13, 23, 37, 44, tzinfo=timezone.utc)
    assert parse_time("2024-11-05 10:25:08") == datetime(2024, 11, 5, 10, 25, 8, tzinfo=timezone.utc)


def test_fork_pages_spread_from_newest_to_oldest():
    assert fork_pages(50_000, 200) == (50, [1, 334, 667, 1000])
    assert fork_pages(150, 200) == (100, [1, 2])
    assert fork_pages(0, 200) == (100, [1])


class FakeGitHub:
    """Answers user lookups and refuses any batch above `limit`, the way GitHub does."""

    def __init__(self, known, limit):
        self.known, self.limit, self.calls = known, limit, 0

    def graphql(self, query, variables):
        self.calls += 1
        if len(variables) > self.limit:
            return {"errors": [{"message": "Resource limits for this query exceeded."}]}
        data, errors = {}, []
        for key, login in variables.items():
            alias = "u" + key[1:]
            if login in self.known:
                data[alias] = {
                    "login": login,
                    "createdAt": "2020-01-01T00:00:00Z",
                    "followers": {"totalCount": 1},
                    "following": {"totalCount": 3},
                    "repositories": {"totalCount": 2},
                    "gists": {"totalCount": 4},
                }
            else:
                data[alias] = None
                errors.append({"type": "NOT_FOUND", "path": [alias], "message": "Could not resolve"})
        return {"data": data, "errors": errors} if errors else {"data": data}


def test_lookup_halves_heavy_batches_and_marks_missing_accounts_gone():
    gh = FakeGitHub({"alice", "bob", "carol"}, limit=2)
    found = github.profiles(gh, ["alice", "bob", "carol", "dave", "bad login!"])
    assert found["alice"].repos == 2 and found["carol"].gists == 4 and found["bob"].following == 3
    assert found["dave"] is GONE
    assert "bad login!" not in found
    assert gh.calls == 3


def test_matched_control_uses_other_accounts_from_the_same_hour(monkeypatch):
    rows = [  # one row per hour: that hour's random sample of (login, time)
        [[["x1", "2024-11-05 10:12:00"], ["t1", "2024-11-05 10:40:00"], ["x2", "2024-11-05 10:50:00"]]],
        [[["y1", "2024-11-05 13:05:00"]]],
    ]
    seen = {}

    def fake_query(sql, **params):
        seen.update(sql=sql, params=params)
        return rows

    monkeypatch.setattr(archive, "query", fake_query)
    sightings = [
        Sighting("t1", parse_time("2024-11-05 10:30:00")),
        Sighting("t2", parse_time("2024-11-05 10:31:00")),
        Sighting("t3", parse_time("2024-11-05 12:59:00")),
    ]
    picked = archive.matched(sightings, "acme/target", "star")
    assert sorted(s.login for s in picked) == ["x1", "x2", "y1"]
    assert "'WatchEvent'" in seen["sql"] and seen["params"] == {"repo": "acme/target"}
    assert archive.matched([], "acme/target", "fork") == []


def test_report_renders_as_text_and_json():
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    target = [Sighting(f"t{i}", parse_time("2024-11-05 10:00:00")) for i in range(40)]
    control = [Sighting(f"c{i}", parse_time("2024-11-05 10:00:00")) for i in range(40)]
    findings = judge.compare(
        "archive",
        {"young": Rate(2, 40), "hollow": Rate(30, 38), "batch": Rate(0, 38), "gone": Rate(0, 40)},
        {"young": Rate(3, 40), "hollow": Rate(4, 37), "batch": Rate(0, 37), "gone": Rate(1, 41)},
    )
    report = cli.Report(
        repo="acme/target",
        stars=1234,
        forks=5,
        checked=now,
        control="random accounts that starred or forked other repos in the same hours",
        samples={"archive": (target, control)},
        findings=findings,
        verdict=judge.decide(findings, [judge.Shared("farm/client", 12, 40, 99)]),
        shared_checked={"history": 40},
        history=[(date(2026, 10, 1), 10), (date(2026, 10, 13), 1234)],
        archive_events=1200,
    )
    text = cli.render(report, fancy=False)
    assert "Verdict: farm signature." in text
    assert "79%!" in text and "farm/client (99 stars)" in text
    data = json.loads(json.dumps(cli.to_json(report)))
    assert data["verdict"] == "farm" and data["shared_stars"]["history"][0]["repo"] == "farm/client"
    assert data["samples"]["archive"]["accounts"] == 40


def test_readers_split_between_recent_and_history():
    now = parse_time("2026-10-14 12:00:00")
    live = [Sighting(f"l{i}", now) for i in range(94)]
    old = [Sighting(f"a{i}", now) for i in range(200)] + [Sighting("l0", now)]
    picked = cli._readers({"live": live, "archive": old})
    assert len(picked["recent"]) == 50 and len(picked["history"]) == 50 and "l0" not in picked["history"]
    only_old = cli._readers({"archive": old})
    assert "recent" not in only_old and len(only_old["history"]) == 100


def test_a_crash_exits_2_not_1(monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "audit", explode)
    assert cli.main(["owner/repo"]) == 2
    assert "unexpected error" in capsys.readouterr().err
