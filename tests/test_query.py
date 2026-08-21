from datetime import datetime

from scripts.query import compact, envelope, select

NOW = datetime.fromisoformat("2026-08-21T12:00:00-04:00")


def proj(name, *, slug=None, category="Cat", status="active", attention=10,
         open_items=0, roadmap=0, last="2026-08-18T12:00:00-04:00",
         redacted=False, plans=(), dirty=0, untracked=0, unpushed=0,
         ahead=(), stashes=0, handoff=False, half_plan=False, path=None,
         reasons=()):
    """One facts.json record, trimmed to the fields the projection reads.

    `plans` is a sequence of (checked, unchecked) pairs.
    """
    return {
        "slug": slug or f"-slug-{name}",
        "name": name,
        "category": category,
        "category_display": category,
        "redacted": redacted,
        "display_path": path if path is not None else f"~/workspace/{category}/{name}",
        "error": [],
        "git": {"dirty_files": dirty, "untracked_files": untracked,
                "unpushed": unpushed, "branches_ahead": list(ahead),
                "stashes": stashes},
        "docs": {"plans": [{"checked": c, "unchecked": u} for c, u in plans],
                 "specs": [], "has_handoff": handoff,
                 "half_checked_plan": half_plan},
        "derived": {"status": status, "attention": attention,
                    "open_items": open_items, "roadmap_items": roadmap,
                    "last_worked": last, "attention_reasons": list(reasons)},
    }


def facts(*projects):
    return {"schema_version": 1, "scanned_at": "2026-08-21T05:30:00-04:00",
            "projects": list(projects), "errors": []}


def test_compact_carries_every_documented_field():
    c = compact(proj("alpha"), NOW)
    assert set(c) == {
        "slug", "name", "category", "status", "attention", "last_worked",
        "age", "open", "roadmap", "plans", "dirty", "unpushed", "ahead",
        "stashes", "handoff", "half_plan", "redacted", "path", "reasons",
    }


def test_plans_aggregate_sums_across_files():
    # Two plan files, 3+1 checked of 3+1+5+9 total boxes. The denominator is
    # checked+unchecked, not len(plans) and not unchecked alone -- an
    # implementation summing only `unchecked` would report "4/14" here.
    c = compact(proj("p", plans=((3, 5), (1, 9))), NOW)
    assert c["plans"] == "4/18"


def test_plans_aggregate_is_zero_over_zero_with_no_plans():
    assert compact(proj("p"), NOW)["plans"] == "0/0"


def test_dirty_counts_tracked_and_untracked_together():
    # A project with only untracked files is still dirty. Summing just
    # dirty_files would report 0 and hide it.
    assert compact(proj("p", dirty=2, untracked=3), NOW)["dirty"] == 5


def test_age_matches_the_shared_formatter():
    from scripts.render_terminal import relative_age
    p = proj("p", last="2026-08-18T12:00:00-04:00")
    assert compact(p, NOW)["age"] == relative_age(p["derived"]["last_worked"], NOW)
    assert compact(p, NOW)["age"] == "3d"


def test_redacted_record_keeps_its_slug_and_loses_its_name_and_path():
    # scan.py has already nulled these; the projection must not resurrect
    # them from anywhere, and must not print "None" in their place.
    p = proj("x", slug="redacted-abc123", redacted=True)
    p["name"] = None
    p["display_path"] = None
    c = compact(p, NOW)
    assert c["slug"] == "redacted-abc123"
    assert c["name"] is None
    assert c["path"] is None
    assert c["redacted"] is True


def test_default_sort_is_attention_descending():
    # Names are chosen so alphabetical order is the OPPOSITE of attention
    # order, so an implementation that sorted by name would fail rather than
    # coincidentally agree.
    rows = select([proj("alow", attention=5), proj("zhigh", attention=90)])
    assert [r["name"] for r in rows] == ["zhigh", "alow"]


def test_attention_ties_break_on_last_worked_descending():
    # Names are chosen so the slug tiebreak (a-older before z-newer) is the
    # OPPOSITE of the expected recency order. Without them this test passes
    # against a sort that ignores last_worked entirely, and the mutation
    # check in Step 5 would certify a broken implementation.
    rows = select([
        proj("a-older", attention=50, last="2026-01-01T00:00:00-04:00"),
        proj("z-newer", attention=50, last="2026-08-20T00:00:00-04:00"),
    ])
    assert [r["name"] for r in rows] == ["z-newer", "a-older"]


def test_sorting_a_redacted_record_against_a_named_one_does_not_raise():
    # A redacted record's `name` is None. Sorting on the raw field would
    # raise TypeError comparing None to str the moment two records tie --
    # the same hazard render_terminal.py already guards.
    p = proj("x", slug="redacted-abc123", attention=50)
    p["name"] = None
    rows = select([p, proj("named", attention=50)])
    assert len(rows) == 2


def test_recent_sort_orders_by_last_worked():
    rows = select([proj("aolder", last="2026-01-01T00:00:00-04:00", attention=99),
                   proj("znewer", last="2026-08-20T00:00:00-04:00", attention=1)],
                  sort="recent")
    assert [r["name"] for r in rows] == ["znewer", "aolder"]


def test_name_sort_is_alphabetical_and_case_insensitive():
    rows = select([proj("Zebra", attention=99), proj("apple", attention=1)],
                  sort="name")
    assert [r["name"] for r in rows] == ["apple", "Zebra"]


def test_status_and_category_filters_narrow_the_result():
    ps = [proj("keep", category="X"), proj("drop", category="Y", status="stalled")]
    assert [r["name"] for r in select(ps, category="X")] == ["keep"]
    assert [r["name"] for r in select(ps, status="stalled")] == ["drop"]


def test_dormant_and_archived_appear_by_default():
    # Unlike bare `psum`, which hides them: hiding rows is a display
    # decision and this verb is not a display.
    ps = [proj("awake"), proj("sleepy", status="dormant"),
          proj("shelved", status="archived")]
    assert len(select(ps)) == 3


def test_limit_applies_after_sorting():
    # Limiting before the sort would return the two lowest-attention rows.
    ps = [proj("low", attention=1), proj("mid", attention=50), proj("high", attention=99)]
    rows = select(ps, limit=2)
    assert [r["name"] for r in rows] == ["high", "mid"]


def test_envelope_carries_freshness_and_count():
    f = facts(proj("a"), proj("b"))
    env = envelope(f, [compact(p, NOW) for p in f["projects"]], NOW)
    assert env["scanned_at"] == "2026-08-21T05:30:00-04:00"
    assert env["scan_age"] == "6h"
    assert env["count"] == 2
    assert len(env["projects"]) == 2


import pytest

from pathlib import Path
from scripts.descriptions import REDACTED_PLACEHOLDER, Description
from scripts.query import ResolveError, detail, resolve

ROOT = Path("/w")  # never a symlink, so project_key's resolve() cannot diverge


def test_resolve_prefers_exact_slug():
    ps = [proj("alpha", slug="s-alpha"), proj("beta", slug="s-beta")]
    assert resolve(ps, "s-beta")["name"] == "beta"


def test_resolve_accepts_an_exact_name():
    ps = [proj("alpha", slug="s-alpha"), proj("beta", slug="s-beta")]
    assert resolve(ps, "beta")["slug"] == "s-beta"


def test_resolve_accepts_a_unique_case_insensitive_substring():
    ps = [proj("workspace-portfolio"), proj("something-else")]
    assert resolve(ps, "PORTFOL")["name"] == "workspace-portfolio"


def test_an_exact_name_wins_over_a_substring_of_another():
    # "api" is an exact name AND a substring of "api-gateway". Exactness must
    # win, or naming a project precisely would be an ambiguity error.
    ps = [proj("api"), proj("api-gateway")]
    assert resolve(ps, "api")["name"] == "api"


def test_resolve_raises_on_an_ambiguous_substring_and_names_the_candidates():
    ps = [proj("api-gateway"), proj("api-worker")]
    with pytest.raises(ResolveError) as exc:
        resolve(ps, "api-")
    assert "api-gateway" in str(exc.value)
    assert "api-worker" in str(exc.value)


def test_resolve_raises_on_no_match_and_names_the_argument():
    with pytest.raises(ResolveError) as exc:
        resolve([proj("alpha")], "nope")
    assert "nope" in str(exc.value)


def test_resolve_matches_a_redacted_record_by_slug_without_raising():
    # `name` is None; a substring scan that assumed str would raise
    # AttributeError before ever reaching the slug.
    p = proj("x", slug="redacted-abc123", redacted=True)
    p["name"] = None
    assert resolve([p, proj("alpha")], "abc123")["slug"] == "redacted-abc123"


def test_detail_returns_the_record_verbatim_plus_description_and_age():
    p = proj("alpha", path="~/workspace/Cat/alpha")
    descs = {"Cat/alpha": Description(text="A thing.", source="ai")}
    p["path"] = "/w/Cat/alpha"
    out = detail(p, descs, ROOT, NOW)
    assert out["description"] == "A thing."
    assert out["age"] == "3d"
    # Verbatim: every key of the input record survives untouched, so a
    # consumer can read the plan/spec checkbox counts that only live here.
    for key, value in p.items():
        assert out[key] == value


def test_detail_description_is_null_when_absent():
    p = proj("alpha")
    p["path"] = "/w/Cat/alpha"
    assert detail(p, {}, ROOT, NOW)["description"] is None
    assert detail(p, None, ROOT, NOW)["description"] is None


def test_detail_of_a_redacted_project_yields_only_the_placeholder():
    # The whole point of redaction: no model ever saw this project, so the
    # only description that can exist is the fixed placeholder. Any other
    # prose here would mean generated content escaped the redaction.
    p = proj("x", slug="redacted-abc123", redacted=True)
    p["name"] = None
    p["path"] = None
    descs = {"redacted-abc123": Description(text=REDACTED_PLACEHOLDER, source="redacted")}
    out = detail(p, descs, ROOT, NOW)
    assert out["description"] == REDACTED_PLACEHOLDER
    assert out["name"] is None
