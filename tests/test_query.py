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
