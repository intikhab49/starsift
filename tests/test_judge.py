from datetime import date, datetime, timedelta, timezone

from starsift import judge
from starsift.model import GONE, Account, Sighting

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def crowd(prefix, n, *, young=0, hollow=0, batch=0, gone=0):
    """n accounts seen at NOW: deleted, new, old-but-empty, old same-day signups, then ordinary ones."""
    kinds = ["gone"] * gone + ["young"] * young + ["hollow"] * hollow + ["batch"] * batch
    kinds += ["normal"] * (n - len(kinds))
    sightings, lookup = [], {}
    for i, kind in enumerate(kinds):
        login = f"{prefix}{i}"
        sightings.append(Sighting(login, NOW))
        if kind == "gone":
            lookup[login] = GONE
        elif kind == "young":
            lookup[login] = Account(login, NOW - timedelta(days=1 + i % 20), 0, 0, 0, 0)
        elif kind == "hollow":
            lookup[login] = Account(login, NOW - timedelta(days=4000 + 7 * i), 0, 0, 0, 0)
        elif kind == "batch":
            lookup[login] = Account(login, datetime(2022, 3, 14, 9, tzinfo=timezone.utc), 2, 1, 0, 0)
        else:
            lookup[login] = Account(login, NOW - timedelta(days=60 + 11 * i), 4, 3, 2, 1)
    return sightings, lookup


def findings_for(sample, target, control, n=200):
    target_sightings, target_lookup = crowd(f"{sample}t", n, **target)
    control_sightings, control_lookup = crowd(f"{sample}c", n, **control)
    lookup = {**target_lookup, **control_lookup}
    return judge.compare(sample, judge.measure(target_sightings, lookup), judge.measure(control_sightings, lookup))


def verdict_for(target, control, n=200, shared=()):
    return judge.decide(findings_for("archive", target, control, n), list(shared))


def test_measure_counts_each_signal():
    sightings, lookup = crowd("a", 100, young=10, hollow=20, batch=5, gone=4)
    rates = judge.measure(sightings, lookup)
    assert (rates["gone"].hits, rates["gone"].base) == (4, 100)
    assert (rates["young"].hits, rates["young"].base) == (10, 96)
    assert (rates["hollow"].hits, rates["hollow"].base) == (20, 86)
    assert (rates["batch"].hits, rates["batch"].base) == (5, 86)


def test_unanswered_logins_are_left_out():
    sightings, lookup = crowd("a", 50)
    lookup["a0"] = None
    del lookup["a1"]
    assert judge.measure(sightings, lookup)["gone"].base == 48


def test_login_registered_after_the_star_counts_as_gone():
    star = Sighting("reused", NOW - timedelta(days=400))
    rates = judge.measure([star], {"reused": Account("reused", NOW - timedelta(days=10), 1, 1, 1, 1)})
    assert rates["gone"].hits == 1 and rates["young"].base == 0


def test_p_greater_is_small_only_for_real_gaps():
    assert judge.p_greater(judge.Rate(10, 100), judge.Rate(10, 100)) == 0.5
    assert judge.p_greater(judge.Rate(60, 200), judge.Rate(20, 200)) < 1e-6
    assert judge.p_greater(judge.Rate(5, 100), judge.Rate(20, 100)) > 0.99
    assert judge.p_greater(judge.Rate(0, 0), judge.Rate(3, 10)) == 1.0


def test_clean_when_target_matches_control():
    verdict = verdict_for(dict(young=12, hollow=20, gone=4), dict(young=14, hollow=18, gone=5))
    assert verdict.level == "clean" and not verdict.flagged


def test_farm_when_two_different_signals_rise():
    verdict = verdict_for(dict(young=10, hollow=90, gone=50), dict(young=12, hollow=18, gone=6))
    assert verdict.level == "farm"
    assert {f.signal.key for f in verdict.flagged} == {"hollow", "gone"}


def test_newcomer_crowd_is_mixed_not_farm():
    # A repo that goes viral outside GitHub draws real people who sign up just to star it.
    verdict = verdict_for(dict(young=150), dict(young=15))
    assert verdict.level == "mixed"
    assert [f.signal.key for f in verdict.flagged] == ["young"]


def test_mass_deletion_decides_alone():
    # 98% of one flagged repo's stargazers had been deleted, against 16% of the control.
    assert verdict_for(dict(gone=196), dict(gone=32)).level == "farm"
    assert verdict_for(dict(gone=40), dict(gone=12)).level == "mixed"


def test_signature_only_among_recent_stargazers_is_recent_not_farm():
    # Farms star big repos as cover: the latest stargazers carry the signature, the history does not.
    findings = findings_for("archive", dict(young=8), dict(young=10))
    findings += findings_for("live", dict(young=90, gone=40), dict(young=10, gone=4))
    shared = [judge.Shared("someone/tiny-tool", 18, 92, 204)]
    verdict = judge.decide(findings, shared_recent=shared)
    assert verdict.level == "recent"
    assert {f.sample for f in verdict.flagged} == {"live"}


def test_signature_in_history_is_farm_even_when_recent_is_clean():
    findings = findings_for("archive", dict(hollow=90, gone=50), dict(hollow=18, gone=6))
    findings += findings_for("live", dict(), dict())
    assert judge.decide(findings).level == "farm"


def test_small_samples_give_no_verdict():
    assert verdict_for(dict(hollow=20), dict(), n=20).level == "unknown"


def test_shared_stars_plus_one_signal_is_farm():
    shared = [judge.Shared("someone/tiny-tool", 40, 120, 310)]
    assert verdict_for(dict(hollow=90), dict(hollow=18), shared=shared).level == "farm"
    assert verdict_for(dict(), dict(), shared=shared).level == "mixed"


def test_shared_candidates_skip_target_owner_and_rare_overlaps():
    recent = {f"u{i}": ("acme/target", "acme/sibling", "farm/client") for i in range(30)}
    recent.update({f"v{i}": ("big/popular",) for i in range(3)})
    recent["x"] = GONE
    assert judge.shared_candidates(recent, "acme/target") == [("farm/client", 30)]


def test_shared_candidates_need_enough_accounts():
    assert judge.shared_candidates({f"u{i}": ("farm/client",) for i in range(10)}, "acme/target") == []


def test_shared_small_drops_popular_and_vanished_repos():
    candidates = [("farm/client", 30), ("big/popular", 25), ("vanished/repo", 20)]
    sizes = {"farm/client": 120, "big/popular": 250_000}
    assert judge.shared_small(candidates, sizes, 33) == [judge.Shared("farm/client", 30, 33, 120)]


def test_daily_gains_and_busiest_week():
    history = [(date(2026, 10, 1), 10), (date(2026, 10, 3), 40), (date(2026, 10, 9), 100), (date(2026, 10, 10), 1100)]
    assert judge.daily_gains(history, date(2026, 10, 10), 3) == [0, 60, 1000]
    start, share = judge.busiest_week(history)
    assert start == date(2026, 10, 4)
    assert abs(share - 1060 / 1100) < 1e-9
