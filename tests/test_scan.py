import dataclasses
import json
from datetime import datetime
from pathlib import Path
from scripts import render_index
from scripts.config import ProjectOverride, load_config
from scripts.scan import build_facts, redact_record
from tests.conftest import cfg_for
from tests.fixtures.make_tree import build_tree

NOW = datetime.fromisoformat("2026-08-18T19:00:00-04:00")
EMPTY_HERDR = {"sessions": {}, "panes": []}


def facts_for(tmp_path, **kw):
    build_tree(tmp_path)
    return build_facts(
        cfg_for(tmp_path), NOW,
        remember_root=tmp_path / "_rem", claude_root=tmp_path / "_cl",
        herdr=EMPTY_HERDR, self_path=None, **kw
    )


def facts_for_existing_tree(tmp_path, **kw):
    """Like facts_for, but for callers that already built (and then modified)
    the tree themselves — does not call build_tree again."""
    return build_facts(
        cfg_for(tmp_path), NOW,
        remember_root=tmp_path / "_rem", claude_root=tmp_path / "_cl",
        herdr=EMPTY_HERDR, self_path=None, **kw
    )


def by_name(facts):
    # A redacted record has name=None (see redact_record) — falling back to
    # slug keeps this a total, collision-free map instead of every redacted
    # project colliding under the key None.
    return {(p["name"] or p["slug"]): p for p in facts["projects"]}


def redacted_projects(facts):
    return [p for p in facts["projects"] if p["redacted"]]


def test_document_shape_and_version(tmp_path):
    f = facts_for(tmp_path)
    assert f["schema_version"] == 1
    assert f["scanned_at"] == NOW.isoformat()
    assert isinstance(f["projects"], list) and f["projects"]
    assert isinstance(f["errors"], list)


def test_every_record_carries_a_derived_block_and_a_hash(tmp_path):
    for p in facts_for(tmp_path)["projects"]:
        assert p["derived"]["status"]
        assert p["content_hash"].startswith("sha256:")


def test_category_is_the_first_segment_under_root(tmp_path):
    p = by_name(facts_for(tmp_path))["child-a"]
    assert p["category"] == "Group"


def test_category_display_rename_is_applied(tmp_path):
    build_tree(tmp_path)
    (tmp_path / "Group" / "note.md").write_text("# hi\n")
    cfg = cfg_for(tmp_path)
    cfg.categories["Group"] = "Renamed"   # Config is frozen; the dict inside is not
    f = build_facts(cfg, NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)
    assert by_name(f)["child-a"]["category_display"] == "Renamed"


def test_a_corrupt_repo_records_an_error_and_does_not_abort_the_scan(tmp_path):
    """Covers the SOFT-error path only: collect_git degrades a corrupt HEAD to a
    returned dict with an "error" key rather than raising, so this proves error
    propagation into errors[] plus continued processing of the rest of the tree —
    not that a raised exception is caught. See
    test_a_raising_collector_records_an_error_and_the_scan_continues for that.
    """
    build_tree(tmp_path)
    (tmp_path / "plain-repo" / ".git" / "HEAD").write_text("garbage\n")
    f = build_facts(cfg_for(tmp_path), NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)
    assert len(f["projects"]) > 3          # the scan completed
    assert any(e["stage"] == "git" for e in f["errors"])


def test_a_raising_collector_records_an_error_and_the_scan_continues(tmp_path, monkeypatch):
    build_tree(tmp_path)
    from scripts import scan as scan_mod

    real_collect = scan_mod.collect_git

    def explode(p, excludes, now, exclude_globs=()):
        if p.name == "plain-repo":
            raise RuntimeError("simulated collector failure")
        return real_collect(p, excludes, now, exclude_globs)

    monkeypatch.setattr(scan_mod, "collect_git", explode)
    f = facts_for_existing_tree(tmp_path)
    names = {p["name"] for p in f["projects"]}
    # Continuation: other projects still present. Without the try/except the
    # RuntimeError propagates out of build_facts and this never returns.
    assert "dirty-repo" in names and "outer-repo" in names
    # And the failure is recorded, not silently dropped — a project that vanished
    # from the index is worse than one flagged unreadable.
    assert "plain-repo" in names
    rec = by_name(f)["plain-repo"]
    assert rec["error"] and any("simulated collector failure" in m for m in rec["error"])
    assert any(e["path"] == "plain-repo" and e["stage"] == "git" for e in f["errors"])


def test_a_raising_activity_collector_records_an_error_and_the_scan_continues(
    tmp_path, monkeypatch
):
    build_tree(tmp_path)
    from scripts import scan as scan_mod

    def explode(path, aliases, *, remember_root, claude_root, herdr):
        if path.name == "plain-repo":
            raise RuntimeError("simulated activity failure")
        return activity_mod_real(
            path, aliases,
            remember_root=remember_root, claude_root=claude_root, herdr=herdr,
        )

    activity_mod_real = scan_mod.activity_mod.collect_activity
    monkeypatch.setattr(scan_mod.activity_mod, "collect_activity", explode)
    f = facts_for_existing_tree(tmp_path)
    names = {p["name"] for p in f["projects"]}
    # Continuation: iterdir() (used inside collect_activity) does not swallow
    # PermissionError the way os.walk does, so this stage needs its own guard —
    # without it, the RuntimeError propagates out of build_facts and this test
    # never returns rather than failing an assertion.
    assert "dirty-repo" in names and "outer-repo" in names
    assert "plain-repo" in names
    rec = by_name(f)["plain-repo"]
    assert rec["error"] and any("simulated activity failure" in m for m in rec["error"])
    assert any(e["path"] == "plain-repo" and e["stage"] == "activity" for e in f["errors"])
    # derive() must still run on the fallback sub-record rather than KeyError-ing.
    assert rec["derived"]["status"]


def test_fs_stats_excludes_vendor_trees_from_counts_and_recency(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "noisy-repo"
    vendor = repo / "node_modules" / "pkg"
    vendor.mkdir(parents=True, exist_ok=True)
    for i in range(40):
        (vendor / f"dep{i}.js").write_text("x")
    rec = by_name(facts_for_existing_tree(tmp_path))["noisy-repo"]
    # A dependency install must neither inflate the file count nor be the thing
    # that makes a project look recently worked on.
    assert rec["fs"]["file_count"] <= 5
    assert "node_modules" not in (rec["fs"]["newest_path"] or "")


def test_files_touched_90d_anchors_to_the_projects_own_activity_not_now(tmp_path):
    """IMPORTANT: files_touched_90d used to count files inside a window ending
    at "now", so a project quiet longer than 90 days always reported 0 — even
    one that was genuinely busy before it went quiet. That silences
    status.py's non-repo intensity term (and therefore stall) exactly for the
    doc folders and old repos quiet longest, which is precisely what the
    fallback exists to serve. once-busy-docs (see make_tree.py) holds 20 files
    all backdated to the same day, ~290 days before the suite's fixed NOW —
    "now"-anchored, every one of them falls outside any 90-day window ending
    at scan time.
    """
    rec = by_name(facts_for(tmp_path))["once-busy-docs"]
    assert rec["fs"]["files_touched_90d"] > 0


def test_a_project_quiet_a_long_time_but_once_busy_outranks_one_always_quiet(tmp_path):
    """Both fixture directories (see make_tree.py) share the exact same
    backdated mtime, so _last_worked / quiet_days and therefore the stall
    ramp are identical for both — only their historical busyness (and hence
    intensity) differs. once-busy-docs holds 20 files touched on that one
    day; always-quiet-docs holds 1. Before the anchor fix both silently
    scored files_touched_90d == 0 and were indistinguishable; after it, the
    project that was once busy must score higher attention.
    """
    projects = by_name(facts_for(tmp_path))
    once_busy, always_quiet = projects["once-busy-docs"], projects["always-quiet-docs"]
    assert once_busy["derived"]["attention"] > always_quiet["derived"]["attention"]


def test_self_repo_excludes_its_own_generated_paths(tmp_path):
    build_tree(tmp_path)
    me = tmp_path / "plain-repo"
    (me / "state").mkdir()
    (me / "state" / "TODO.md").write_text("- [ ] generated noise\n")
    f = build_facts(cfg_for(tmp_path), NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=me)
    assert by_name(f)["plain-repo"]["derived"]["open_items"] == 0


def test_redacted_record_keeps_structure_and_drops_every_free_text_field(tmp_path):
    record = {
        "name": "integration-project", "category": "clients", "redacted": True,
        "path": "/home/user/workspace/clients/acme/integration-project",
        "display_path": "~/workspace/clients/acme/integration-project",
        # A stage failure's exception message routinely embeds the absolute
        # path that caused it (CRITICAL 1) — this mirrors the real one the
        # reviewer demonstrated against docs.py's read_text().
        "error": ["docs: [Errno 13] Permission denied: "
                  "'/home/user/workspace/clients/acme/integration-project/TODO.md'"],
        "git": {"last_commit_subject": "fix ACME billing export",
                "head_sha": "abc", "remote": "git@github.com:acme/thing.git",
                "remote_slug": "acme/thing", "branch": "main", "commits_90d": 4,
                # A branch name is developer-chosen free text exactly like a
                # commit subject — this one names the client the same way.
                "dirty_files": 1, "untracked_files": 0, "porcelain_digest": "d",
                "unpushed": 0, "branches_ahead": ["fix/acme-billing-export"],
                "worktrees": ["/x"], "stashes": 0},
        "fs": {"newest_path": "/home/user/workspace/clients/acme/secret-plan.md",
               "newest_mtime": "t", "file_count": 3, "files_touched_90d": 1},
        "docs": {"claude_md": {"path": "CLAUDE.md", "sha": "s", "mtime": "t"},
                 "readme": None, "changelog": None,
                 "backlog_files": [{"path": "ACME-migration/TODO.md", "sha": "b",
                                    "mtime": "t", "open_items": 3, "kind": "todo"}],
                 "plans": [{"path": "docs/superpowers/plans/2026-08-01-acme-cutover.md",
                            "title": "ACME Cutover", "date": "2026-08-01", "sha": "p",
                            "checked": 1, "unchecked": 2, "mtime": "t"}],
                 "specs": [], "open_items": 5, "has_handoff": False,
                 "half_checked_plan": True},
        "activity": {"remember_tail": "Discussed ACME's billing dispute.",
                     "remember_tail_sha": "r", "slugs": ["-x"], "remember_slug": "-x",
                     "remember_last_day": "2026-08-10", "claude_session_count": 4,
                     # A herdr session is named after the project it belongs to
                     # (see psum go in the design spec) — for a client project
                     # that name IS the client name.
                     "claude_last_session_at": "t", "herdr_session": "acme-integration",
                     "herdr_session_running": False, "herdr_open_panes": 0,
                     "herdr_agent_status": None},
        "derived": {"status": "stalled", "attention": 40, "open_items": 5,
                    "last_worked": "t", "last_worked_source": "git",
                    "attention_reasons": ["5 open item(s)"]},
        "content_hash": "sha256:x",
    }
    r = redact_record(record)
    blob = json.dumps(r)
    for leak in ("ACME", "acme", "secret-plan", "billing"):
        assert leak not in blob, f"redacted record leaked {leak!r}"
    # Structure survives: counts, status and recency are all still usable.
    assert r["derived"]["status"] == "stalled"
    assert r["derived"]["open_items"] == 5
    assert r["git"]["dirty_files"] == 1
    assert r["category"] == "clients"
    assert r["docs"]["backlog_files"][0]["open_items"] == 3
    assert r["docs"]["plans"][0]["unchecked"] == 2
    # The folder name is NOT retained: a doc-only project promoted purely by
    # discovery can be named after the client itself (see redact_record).
    # Every surface falls back to the digest slug for display.
    assert r["name"] is None
    assert r["error"] is None
    assert r["git"]["branches_ahead"] is None
    assert r["activity"]["herdr_session"] is None
    # Every path-bearing top-level field is gone; the slug becomes a stable digest
    # so Phase 2 brief filenames do not churn between runs.
    assert r["path"] is None and r["display_path"] is None and r["aliases"] is None
    assert r["slug"].startswith("redacted-") and len(r["slug"]) == 25
    assert redact_record(record)["slug"] == r["slug"]   # deterministic


def test_projects_under_a_redact_prefix_are_redacted_in_the_document(tmp_path):
    build_tree(tmp_path)
    notes = tmp_path / "clients" / "acme" / "notes"
    notes.mkdir(parents=True)
    # TODO*.md matches BACKLOG_PATTERNS — a differently-named file (the
    # original fixture used call.md, which matches no backlog pattern) leaves
    # backlog_files permanently empty and the assertion below vacuous no
    # matter what redact_record does.
    (notes / "TODO.md").write_text("# ACME call\n- [ ] follow up\n")
    f = build_facts(cfg_for(tmp_path), NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)
    [rec] = redacted_projects(f)
    assert rec["redacted"] is True
    assert rec["docs"]["backlog_files"], "fixture must actually produce a backlog entry"
    assert all(b["path"] is None for b in rec["docs"]["backlog_files"])


#: One unique marker per REDACTED_FIELDS/REDACTED_TOP_LEVEL entry. A shared or
#: empty fixture value lets a dropped field hide behind another field's leak —
#: dropping "head_sha" from REDACTED_FIELDS left the older leak-scan green
#: because no fixture value made head_sha's absence observable on its own.
_ALL_MARKERS = {
    "LEAK_PATH", "LEAK_DISPLAY_PATH", "LEAK_ALIASES", "LEAK_ERROR",
    "LEAK_LAST_COMMIT_SUBJECT", "LEAK_REMOTE", "LEAK_REMOTE_SLUG",
    "LEAK_BRANCH", "LEAK_HEAD_SHA", "LEAK_WORKTREES", "LEAK_BRANCHES_AHEAD",
    "LEAK_DEFAULT_BRANCH", "LEAK_GIT_ERROR", "LEAK_FS_NEWEST_PATH",
    "LEAK_REMEMBER_TAIL", "LEAK_REMEMBER_SLUG", "LEAK_SLUGS", "LEAK_HERDR_SESSION",
}


def _redaction_fixture():
    return {
        "name": "x", "category": "clients", "redacted": True,
        "path": "LEAK_PATH",
        "display_path": "LEAK_DISPLAY_PATH",
        "aliases": ["LEAK_ALIASES"],
        "error": ["LEAK_ERROR"],
        "git": {
            "last_commit_subject": "LEAK_LAST_COMMIT_SUBJECT",
            "head_sha": "LEAK_HEAD_SHA",
            "remote": "LEAK_REMOTE",
            "remote_slug": "LEAK_REMOTE_SLUG",
            "branch": "LEAK_BRANCH",
            "default_branch": "LEAK_DEFAULT_BRANCH",
            "last_commit_at": "2026-08-01T12:00:00-04:00",
            "error": "LEAK_GIT_ERROR",
            "commits_90d": 1, "dirty_files": 0, "untracked_files": 0,
            "porcelain_digest": "d", "unpushed": 0,
            "branches_ahead": ["LEAK_BRANCHES_AHEAD"],
            "worktrees": ["LEAK_WORKTREES"], "stashes": 0,
        },
        "fs": {"newest_path": "LEAK_FS_NEWEST_PATH", "newest_mtime": "t",
               "file_count": 1, "files_touched_90d": 1},
        "docs": {"claude_md": None, "readme": None, "changelog": None,
                 "backlog_files": [], "plans": [], "specs": [],
                 "open_items": 0, "has_handoff": False, "half_checked_plan": False},
        "activity": {"remember_tail": "LEAK_REMEMBER_TAIL",
                     "remember_tail_sha": "r", "slugs": ["LEAK_SLUGS"],
                     "remember_slug": "LEAK_REMEMBER_SLUG",
                     "remember_last_day": None, "claude_session_count": 0,
                     "claude_last_session_at": None, "herdr_session": "LEAK_HERDR_SESSION",
                     "herdr_session_running": False, "herdr_open_panes": 0,
                     "herdr_agent_status": None},
        "derived": {"status": "active", "attention": 0, "open_items": 0,
                    "last_worked": None, "last_worked_source": None,
                    "attention_reasons": []},
        "content_hash": "sha256:x",
    }


def test_every_redacted_field_has_its_own_leak_marker():
    record = _redaction_fixture()
    blob = json.dumps(redact_record(record))
    leaked = sorted(m for m in _ALL_MARKERS if m in blob)
    assert leaked == [], f"redaction left these fields intact: {leaked}"


#: Keys whose string values are known-safe after redaction: fixed enums,
#: content-addressed hashes, and timestamps — never developer- or
#: user-authored prose about the project. Deliberately NOT on this list:
#: "name" and "sha" — both are nulled by redact_record, and if a future change
#: stops nulling either one, the leaf-walk test below must fail rather than
#: silently pass because someone added the key here "to be safe".
_SAFE_STRING_KEYS = {
    "category", "category_display",
    "status", "status_source", "last_worked_source", "last_worked",
    "newest_mtime", "remember_last_day", "claude_last_session_at",
    # herdr's own workflow-state enum (working/idle/done), not prose the
    # user wrote about the project.
    "herdr_agent_status",
    "content_hash", "slug",
    "porcelain_digest", "remember_tail_sha",
    "mtime", "date", "kind",
    "attention_reasons",
    # A bare ISO-8601 timestamp — unlike last_commit_subject, it carries no
    # developer- or client-authored text, and recency is the whole point of
    # the tool. Ruling from the residuals review: this one stays.
    "last_commit_at",
}


def _string_leaves(value, key=None):
    """Yield (key, value) for every string leaf in a nested JSON structure."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _string_leaves(v, k)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item, key)
    elif isinstance(value, str):
        yield key, value


def test_every_string_leaf_in_a_hand_written_redacted_fixture_is_on_the_safe_allowlist():
    """IMPORTANT 2: the marker test above can only catch a field that someone
    remembered to both redact AND plant a marker for — it is derived from the
    same list it is checking, so a free-text field nobody ever added to
    REDACTED_FIELDS is invisible to it forever. This test inverts the
    direction: instead of asking "did the fields we know about get redacted",
    it asks "is every string that survived redaction on a list of fields we
    have positively verified are safe". A new free-text field then fails HERE
    — on the allowlist — rather than passing unnoticed.

    Kept alongside test_every_string_leaf_in_a_really_scanned_redacted_record_
    is_on_the_safe_allowlist below rather than replaced by it: this hand-built
    fixture covers `docs` shapes (plan titles, backlog paths) that the other
    test's minimal git-repo tree does not exercise, so the two are
    complementary rather than redundant.
    """
    record = _redaction_fixture()
    r = redact_record(record)
    offenders = sorted(
        f"{k}={v!r}" for k, v in _string_leaves(r) if k not in _SAFE_STRING_KEYS
    )
    assert offenders == [], f"non-allowlisted string field(s) survived redaction: {offenders}"


def test_every_string_leaf_in_a_really_scanned_redacted_record_is_on_the_safe_allowlist(
    tmp_path,
):
    """IMPORTANT 3: the test above only sees keys someone remembered to type
    into _redaction_fixture() — a literal hand-written dict. Applying the same
    leaf-walk to a REAL redacted record (built by an actual build_facts() run,
    not a synthetic payload) surfaced three fields the fixture had never
    included at all: git.default_branch, git.last_commit_at and git.error —
    every real git record carries all three, the fixture simply never typed
    them in. Sourcing the input from a real scan means any field newly added
    to collect_git, collect_docs, collect_activity or the record itself shows
    up here automatically and must be classified — either redacted, or added
    to _SAFE_STRING_KEYS with a reason — instead of silently passing because
    nobody remembered to add it to a hand-maintained dict.
    """
    from tests.fixtures.make_tree import _git, _repo

    build_tree(tmp_path)
    healthy = _repo(tmp_path / "clients" / "acme" / "billing-migration")
    (healthy / "BACKLOG.md").write_text("- [ ] follow up on ACME billing\n")
    plans_dir = healthy / "docs" / "superpowers" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-01-acme-cutover.md").write_text(
        "# ACME Cutover\n- [ ] step one\n- [x] step two\n"
    )
    _git(healthy, "add", "-A")
    _git(healthy, "commit", "-q", "-m", "docs", when="2026-08-01T12:00:00-04:00")

    # A second redacted project whose git HEAD is corrupt: the only way
    # collect_git's git.error is ever populated with actual text instead of
    # staying None, so the real scan needs one to exercise that field too.
    corrupt = _repo(tmp_path / "clients" / "acme" / "broken-integration")
    (corrupt / ".git" / "HEAD").write_text("garbage\n")

    # Sanity-check the fixture BEFORE redaction (which nulls git.error): if
    # this ever stops raising a real error, the assertion after build_facts
    # below would trivially pass having tested nothing.
    from scripts.gitinfo import collect_git

    assert collect_git(corrupt, [], NOW).get("error"), (
        "fixture must actually populate git.error, or this test cannot prove"
        " anything about how it is handled"
    )

    f = build_facts(cfg_for(tmp_path), NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)
    redacted = redacted_projects(f)
    assert len(redacted) >= 2, "fixture must produce both redacted projects above"
    offenders = sorted(
        f"{k}={v!r}"
        for rec in redacted
        for k, v in _string_leaves(rec)
        if k not in _SAFE_STRING_KEYS
    )
    assert offenders == [], f"non-allowlisted string field(s) survived redaction: {offenders}"


def test_redacted_project_error_never_leaks_client_path_into_record_or_index(
    tmp_path, monkeypatch
):
    """CRITICAL 1 leak test. Before the fix: a PermissionError raised while
    reading a client project's docs put the client's absolute path straight
    into record["error"] (a scalar, never touched by redact_record) and into
    facts["errors"][] (never redacted at all) — and render_index printed both
    verbatim into the committed, phone-synced INDEX.md.
    """
    build_tree(tmp_path)
    client_dir = tmp_path / "clients" / "agmaas" / "Acme-Dental-migration"
    client_dir.mkdir(parents=True)
    (client_dir / "TODO.md").write_text("- [ ] follow up\n")

    from scripts import scan as scan_mod

    real_docs = scan_mod.collect_docs

    def bad_docs(p, excludes, generated, self_repo, exclude_globs=()):
        if p.name == "Acme-Dental-migration":
            raise PermissionError(
                f"[Errno 13] Permission denied: '{p / 'TODO.md'}'"
            )
        return real_docs(p, excludes, generated, self_repo, exclude_globs)

    monkeypatch.setattr(scan_mod, "collect_docs", bad_docs)
    f = build_facts(cfg_for(tmp_path), NOW, remember_root=tmp_path / "_r",
                    claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)

    [rec] = redacted_projects(f)
    assert rec["redacted"] is True
    record_blob = json.dumps(rec)
    assert "agmaas" not in record_blob and "Acme-Dental" not in record_blob
    assert rec["error"] is None

    errors_blob = json.dumps(f["errors"])
    assert "agmaas" not in errors_blob and "Acme-Dental" not in errors_blob
    docs_errors = [e for e in f["errors"] if e["stage"] == "docs" and e["path"] == rec["slug"]]
    assert docs_errors and all(e["message"] is None for e in docs_errors)

    out = render_index.render(f, load_config(Path("config/projects.toml")), now=NOW)
    assert "agmaas" not in out and "Acme-Dental" not in out


def _facts(tmp_path, **overrides):
    return build_facts(
        cfg_for(tmp_path, **overrides), NOW,
        remember_root=tmp_path / "_rem", claude_root=tmp_path / "_cl",
        herdr=EMPTY_HERDR, self_path=None,
    )


def test_parallel_scan_is_deterministic(tmp_path):
    build_tree(tmp_path)
    serial = _facts(tmp_path, parallelism=1)
    parallel = _facts(tmp_path, parallelism=8)
    for f in (serial, parallel):
        f.pop("duration_ms", None)
        f.pop("scanned_at", None)
    assert serial == parallel


def test_errors_from_one_project_are_sorted_rather_than_left_in_stage_order(
    tmp_path, monkeypatch
):
    build_tree(tmp_path)
    from scripts import scan as scan_mod

    real_git, real_docs = scan_mod.collect_git, scan_mod.collect_docs

    def bad_git(p, excludes, now, exclude_globs=()):
        if p.name == "plain-repo":
            raise RuntimeError("git stage failed")
        return real_git(p, excludes, now, exclude_globs)

    def bad_docs(p, excludes, generated, self_repo, exclude_globs=()):
        if p.name == "plain-repo":
            raise RuntimeError("docs stage failed")
        return real_docs(p, excludes, generated, self_repo, exclude_globs)

    monkeypatch.setattr(scan_mod, "collect_git", bad_git)
    monkeypatch.setattr(scan_mod, "collect_docs", bad_docs)

    stages = [e["stage"] for e in _facts(tmp_path)["errors"] if e["path"] == "plain-repo"]
    # Two failures from ONE candidate is the only case the sort can affect:
    # pool.map preserves candidate order, so cross-project ordering is stable
    # with or without it. The code appends in pipeline order (git, then docs),
    # so unsorted yields ["git", "docs"] and sorted yields ["docs", "git"].
    assert stages == ["docs", "git"]


def test_project_sort_survives_a_redacted_project_sharing_a_category_with_a_named_one(
    tmp_path,
):
    """HIGH: the final sort key was (category, name), and a redacted record's
    name is None (see redact_record). `('Group', None) < ('Group', 'child-b')`
    raises TypeError — comparing NoneType to str — the moment a redacted and a
    non-redacted project land in the same category. That propagates straight
    out of build_facts and aborts the ENTIRE scan, not just one project.

    Unreachable with only the default `redact_prefixes = ["clients/"]`, because
    every project it redacts sits under the same "clients" category and their
    keys are equal (never invoking `<`). It becomes live the moment the
    per-project `redact = true` override — a supported, documented feature —
    marks one project in a category that also holds a non-redacted sibling,
    which is exactly what this test sets up: "Group/child-a" and
    "Group/child-b" both categorize as "Group" (see
    test_category_is_the_first_segment_under_root), and only child-a is
    redacted.
    """
    build_tree(tmp_path)
    cfg = cfg_for(tmp_path, redact_prefixes=())
    cfg = dataclasses.replace(
        cfg, projects={"Group/child-a": ProjectOverride(redact=True)}
    )
    # Must not raise TypeError. Two runs must also agree on order — the fallback
    # (p["name"] or p["slug"]) has to be stable, not just non-crashing.
    first = build_facts(cfg, NOW, remember_root=tmp_path / "_r",
                        claude_root=tmp_path / "_c", herdr=EMPTY_HERDR, self_path=None)
    second = build_facts(cfg, NOW, remember_root=tmp_path / "_r2",
                         claude_root=tmp_path / "_c2", herdr=EMPTY_HERDR, self_path=None)

    group = [p for p in first["projects"] if p["category"] == "Group"]
    assert {p["name"] for p in group} == {None, "child-b"}
    redacted = [p for p in group if p["redacted"]][0]
    assert redacted["name"] is None and redacted["slug"].startswith("redacted-")

    order_first = [p["name"] or p["slug"] for p in first["projects"]]
    order_second = [p["name"] or p["slug"] for p in second["projects"]]
    assert order_first == order_second


def test_a_project_failing_at_two_stages_keeps_both_error_messages(tmp_path, monkeypatch):
    """record["error"] used to be a scalar, overwritten by each failing stage in
    turn — a project that failed at git AND docs silently lost the git message
    the moment docs also failed. It is a list now specifically so this cannot
    happen."""
    build_tree(tmp_path)
    from scripts import scan as scan_mod

    real_git, real_docs = scan_mod.collect_git, scan_mod.collect_docs

    def bad_git(p, excludes, now, exclude_globs=()):
        if p.name == "plain-repo":
            raise RuntimeError("git stage failed")
        return real_git(p, excludes, now, exclude_globs)

    def bad_docs(p, excludes, generated, self_repo, exclude_globs=()):
        if p.name == "plain-repo":
            raise RuntimeError("docs stage failed")
        return real_docs(p, excludes, generated, self_repo, exclude_globs)

    monkeypatch.setattr(scan_mod, "collect_git", bad_git)
    monkeypatch.setattr(scan_mod, "collect_docs", bad_docs)

    rec = by_name(_facts(tmp_path))["plain-repo"]
    assert any("git stage failed" in m for m in rec["error"])
    assert any("docs stage failed" in m for m in rec["error"])
