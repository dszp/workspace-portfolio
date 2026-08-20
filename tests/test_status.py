# tests/test_status.py
from datetime import datetime, timedelta
from pathlib import Path
import pytest
from scripts.config import load_config, ProjectOverride
from scripts.status import derive

NOW = datetime.fromisoformat("2026-08-18T19:00:00-04:00")
CFG = load_config(Path("config/projects.toml"))


def rec(*, days_quiet=0, commits_90d=0, commits_90d_anchored=None, dirty=0,
        untracked=0, unpushed=0, ahead=(), open_items=0, roadmap_items=0,
        handoff=False, half_plan=False, plans=(), is_repo=True, files_90d=0):
    when = (NOW - timedelta(days=days_quiet)).isoformat()
    # Defaulting the anchored count to commits_90d keeps every existing test
    # (which only ever set the one "now"-anchored field) exercising the same
    # intensity it always did; only a test that cares about the anchor moving
    # away from "now" needs to pass commits_90d_anchored explicitly.
    anchored = commits_90d if commits_90d_anchored is None else commits_90d_anchored
    return {
        "is_repo": is_repo,
        "git": None if not is_repo else {
            "last_commit_at": when, "commits_90d": commits_90d,
            "commits_90d_anchored": anchored,
            "dirty_files": dirty, "untracked_files": untracked,
            "unpushed": unpushed, "branches_ahead": list(ahead), "stashes": 0,
        },
        "fs": {"newest_mtime": when, "files_touched_90d": files_90d},
        "docs": {"open_items": open_items, "roadmap_items": roadmap_items,
                 "has_handoff": handoff,
                 "half_checked_plan": half_plan, "plans": list(plans)},
        "activity": {"remember_last_day": None, "claude_last_session_at": None},
    }


def test_recent_clean_repo_is_active():
    assert derive(rec(days_quiet=2), CFG, None, NOW)["status"] == "active"


def test_dirty_tree_is_mid_flight_even_when_recent():
    assert derive(rec(days_quiet=1, dirty=3), CFG, None, NOW)["status"] == "mid-flight"


def test_dirty_tree_is_mid_flight_even_when_ancient():
    assert derive(rec(days_quiet=300, dirty=3), CFG, None, NOW)["status"] == "mid-flight"


def test_quiet_with_open_items_is_stalled():
    assert derive(rec(days_quiet=45, open_items=4), CFG, None, NOW)["status"] == "stalled"


def test_quiet_with_nothing_open_is_dormant():
    assert derive(rec(days_quiet=200), CFG, None, NOW)["status"] == "dormant"


def test_roadmap_only_project_stays_dormant_and_attention_ignores_its_size():
    # A quiet project whose only "open" work is a roadmap must not read as
    # stalled, and a bigger roadmap must not move its attention score at all —
    # both directions checked against the same no-roadmap baseline.
    baseline = derive(rec(days_quiet=45, open_items=0, roadmap_items=0), CFG, None, NOW)
    small_roadmap = derive(rec(days_quiet=45, open_items=0, roadmap_items=1), CFG, None, NOW)
    huge_roadmap = derive(rec(days_quiet=45, open_items=0, roadmap_items=500), CFG, None, NOW)
    assert baseline["status"] == "dormant"
    assert small_roadmap["status"] == "dormant"
    assert huge_roadmap["status"] == "dormant"
    assert baseline["attention"] == small_roadmap["attention"] == huge_roadmap["attention"]
    assert huge_roadmap["roadmap_items"] == 500


def test_roadmap_items_is_surfaced_in_derived_but_never_summed_into_open_items():
    out = derive(rec(open_items=3, roadmap_items=9), CFG, None, NOW)
    assert out["open_items"] == 3
    assert out["roadmap_items"] == 9


@pytest.mark.parametrize("days_quiet", [1, 40, 400])
def test_done_is_reachable_at_any_age_and_does_not_decay(days_quiet):
    r = rec(days_quiet=days_quiet, plans=[{"checked": 5, "unchecked": 0}])
    assert derive(r, CFG, None, NOW)["status"] == "done"


def test_done_is_reachable_whether_or_not_roadmap_entries_are_present():
    # A roadmap is aspiration, not obligation: its presence — at any size — must
    # not block `done`. Both directions in one test: no roadmap, and a sizeable
    # one, both landing on the same status.
    clean = rec(days_quiet=1, plans=[{"checked": 5, "unchecked": 0}], roadmap_items=0)
    with_roadmap = rec(days_quiet=1, plans=[{"checked": 5, "unchecked": 0}], roadmap_items=40)
    assert derive(clean, CFG, None, NOW)["status"] == "done"
    assert derive(with_roadmap, CFG, None, NOW)["status"] == "done"


def test_done_requires_a_plan_to_exist():
    # Clean and empty is not evidence of completion.
    assert derive(rec(days_quiet=1), CFG, None, NOW)["status"] == "active"


def test_handoff_blocks_done_and_forces_mid_flight():
    r = rec(days_quiet=5, handoff=True, plans=[{"checked": 5, "unchecked": 0}])
    assert derive(r, CFG, None, NOW)["status"] == "mid-flight"


def test_config_override_wins_over_every_derivation():
    r = rec(days_quiet=1, dirty=9)
    out = derive(r, CFG, ProjectOverride(status="archived"), NOW)
    assert out["status"] == "archived"
    assert out["status_source"] == "config"


def test_boundary_day_of_active_window():
    assert derive(rec(days_quiet=21, open_items=1), CFG, None, NOW)["status"] == "active"
    assert derive(rec(days_quiet=22, open_items=1), CFG, None, NOW)["status"] == "stalled"


def test_hot_then_dropped_outranks_always_slow_at_equal_quiet():
    hot = derive(rec(days_quiet=60, commits_90d=60, open_items=2), CFG, None, NOW)
    slow = derive(rec(days_quiet=60, commits_90d=2, open_items=2), CFG, None, NOW)
    assert hot["attention"] > slow["attention"]


def test_hot_then_dropped_outranks_always_slow_even_forgotten_for_months():
    # days_quiet=60 (the test above) sits inside the live band: quiet_days-7 <
    # stall_ramp_days(60), so the OLD bug (intensity from commits_90d anchored
    # at "now") cannot be seen there — a repo quiet 60 days still has some of
    # its last 90 days of commits inside the "now"-anchored window. At
    # days_quiet=200, that window is entirely empty for BOTH arms regardless of
    # how hot either one used to be, so the old code scored them identically
    # (both stall=0). commits_90d is left at 0 for both here — matching what a
    # real "now"-anchored count would legitimately report 200 days out — and
    # only commits_90d_anchored (anchored at the repo's own last commit)
    # distinguishes "hot, then abandoned" from "always slow".
    hot = derive(
        rec(days_quiet=200, commits_90d=0, commits_90d_anchored=60, open_items=2),
        CFG, None, NOW,
    )
    slow = derive(
        rec(days_quiet=200, commits_90d=0, commits_90d_anchored=2, open_items=2),
        CFG, None, NOW,
    )
    assert hot["attention"] > slow["attention"]


def test_non_repo_project_still_accrues_stall_attention():
    r = rec(days_quiet=60, is_repo=False, files_90d=20, open_items=1)
    assert derive(r, CFG, None, NOW)["attention"] > 20


def test_attention_is_clamped_to_100_and_reasons_are_reported():
    r = rec(days_quiet=90, commits_90d=200, dirty=5, untracked=2, unpushed=9,
            ahead=("feat",), open_items=99, handoff=True, half_plan=True)
    out = derive(r, CFG, None, NOW)
    assert out["attention"] == 100
    assert any("uncommitted" in reason for reason in out["attention_reasons"])


def test_stall_is_zero_through_the_grace_period_then_grows():
    hot = dict(commits_90d=60)
    fresh = derive(rec(days_quiet=0, **hot), CFG, None, NOW)
    at_grace_edge = derive(rec(days_quiet=7, **hot), CFG, None, NOW)
    past_grace = derive(rec(days_quiet=40, **hot), CFG, None, NOW)
    # Zero on both sides of the grace boundary distinguishes "grace period" from
    # "no ramp at all"; a non-zero value past it is the only assertion that fails
    # when the stall term is deleted outright, which is what the old single-point
    # test could not detect.
    assert fresh["attention"] == 0
    assert at_grace_edge["attention"] == 0
    assert past_grace["attention"] > 0


def test_stall_ramp_thresholds_come_from_config():
    import dataclasses
    faster = dataclasses.replace(
        CFG, weights=dataclasses.replace(CFG.weights, stall_grace_days=0, stall_ramp_days=1)
    )
    # No grace and a one-day ramp: a project quiet for two days is already at full
    # stall. Under the default 7/60 the same record scores 0, so this fails if the
    # config values are ignored in favour of the old literals.
    out = derive(rec(days_quiet=2, commits_90d=60), faster, None, NOW)
    assert out["attention"] == CFG.weights.stall_max
    assert derive(rec(days_quiet=2, commits_90d=60), CFG, None, NOW)["attention"] == 0
