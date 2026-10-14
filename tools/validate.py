"""Re-run the README's validation: starsift on random repos from the StarScout data.

Draws 25 repos StarScout suspected of fake stars (half or more of their stars, 150+ stars, still on
GitHub) and 25 from its random sample that it did not suspect (200+ stars), runs starsift on each,
and writes only the verdict counts to docs/validation.json. StarScout's authors ask that their data
not be used to name individual repos publicly, so per-repo results go to the terminal and nowhere else.

    python tools/validate.py        # needs a GitHub token, like starsift; takes about 30 minutes
"""
import base64
import csv
import io
import json
import pathlib
import random
import re
import subprocess
import sys
from collections import Counter
from datetime import date

from starsift.github import GitHub, find_token

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = "/repos/hehao98/StarScout/contents/data/250101"
SEED = 20261014
NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
VERDICTS = ("farm", "recent", "mixed", "clean", "unknown")


def table(gh: GitHub, name: str) -> list[dict]:
    text = base64.b64decode(gh.rest(f"{SNAPSHOT}/{name}")["content"]).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def stars(gh: GitHub, names: list[str]) -> dict[str, int]:
    """Today's star count for each repo that still exists."""
    found = {}
    for i in range(0, len(names), 50):
        chunk = names[i : i + 50]
        declared = ", ".join(f"$o{j}: String!, $n{j}: String!" for j in range(len(chunk)))
        selected = " ".join(f"r{j}: repository(owner: $o{j}, name: $n{j}) {{ stargazerCount }}" for j in range(len(chunk)))
        variables = {}
        for j, repo in enumerate(chunk):
            variables[f"o{j}"], variables[f"n{j}"] = repo.split("/")
        data = gh.graphql(f"query({declared}) {{ {selected} }}", variables).get("data") or {}
        found.update({repo: data[f"r{j}"]["stargazerCount"] for j, repo in enumerate(chunk) if data.get(f"r{j}")})
    return found


def draw(gh: GitHub) -> dict[str, list[str]]:
    share: dict[str, tuple[int, float]] = {}
    for name, column in (("fake_stars_low_activity_repos.csv", "p_stars_low_activity"),
                         ("fake_stars_clustered_repos.csv", "p_stars_clustered")):
        for row in table(gh, name):
            n, p = int(float(row["n_stars"] or 0)), float(row[column] or 0)
            old = share.get(row["repo_name"], (0, 0.0))
            share[row["repo_name"]] = (max(old[0], n), max(old[1], p))
    rng = random.Random(SEED)
    heavy = [repo for repo, (n, p) in share.items() if n >= 150 and p >= 0.5 and NAME.match(repo)]
    suspected = rng.sample(sorted(stars(gh, heavy)), 25)
    pool = [row["repo"] for row in table(gh, "sample_repo_ids.csv")]
    pool = [repo for repo in pool if repo not in share and NAME.match(repo)]
    rng.shuffle(pool)
    counts = stars(gh, pool[:1500])
    unsuspected = [repo for repo in pool[:1500] if counts.get(repo, 0) >= 200][:25]
    return {"suspected": suspected, "unsuspected": unsuspected}


def history_gaps(report: dict) -> dict[str, float]:
    """Each signal's rise over its control in the archived stars, or in the fork owners without them."""
    for sample in ("archive", "forks"):
        rows = [s for s in report["signals"]
                if s["sample"] == sample and s["repo"]["of"] >= 30 and s["control"]["of"] >= 30]
        if rows:
            return {s["signal"]: round(s["repo"]["hits"] / s["repo"]["of"] - s["control"]["hits"] / s["control"]["of"], 3)
                    for s in rows}
    return {}


def main() -> None:
    gh = GitHub(find_token())
    tally = {}
    for label, repos in draw(gh).items():
        verdicts = Counter()
        gaps: dict[str, list[float]] = {}
        for repo in repos:
            run = subprocess.run([sys.executable, "-m", "starsift", repo, "--json"],
                                 capture_output=True, text=True, encoding="utf-8")
            verdict = "error"
            if run.returncode in (0, 1):
                report = json.loads(run.stdout)
                verdict = report["verdict"]
                for signal, gap in history_gaps(report).items():
                    gaps.setdefault(signal, []).append(gap)
            verdicts[verdict] += 1
            print(f"{label:12} {verdict:8} {repo}", flush=True)
        counts = {verdict: verdicts[verdict] for verdict in VERDICTS}
        if verdicts["error"]:
            counts["error"] = verdicts["error"]
        # sorted, so the numbers cannot be matched back to the repos they came from
        tally[label] = {"repos": len(repos), "verdicts": counts, "gaps": {k: sorted(v) for k, v in gaps.items()}}
    result = {
        "checked": date.today().isoformat(),
        "source": "StarScout data snapshot 2025-01-01 (He et al., ICSE 2026), github.com/hehao98/StarScout",
        "seed": SEED,
        "sets": {
            "suspected": {"label": "suspected by the study: half or more of their stars, 150+ stars, still on GitHub",
                          **tally["suspected"]},
            "unsuspected": {"label": "not suspected: the study's random sample, 200+ stars", **tally["unsuspected"]},
        },
    }
    out = ROOT / "docs" / "validation.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
