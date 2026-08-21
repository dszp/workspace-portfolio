import io
import json
import subprocess
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import scripts.render_index as render_index_mod
from scripts.config import load_config
from scripts.descriptions import Description
from scripts.render_index import render, commit_index

NOW = datetime.fromisoformat("2026-08-18T19:00:00-04:00")
CFG = load_config(Path("config/projects.toml"))


def proj(name, category="Cat", status="active", attention=10, open_items=0,
         roadmap_items=0, last="2026-08-18T12:00:00-04:00", reasons=(), redacted=False):
    return {"name": name, "slug": f"-home-user-workspace-{category}-{name}",
            "category": category, "category_display": category,
            "display_path": f"~/workspace/{category}/{name}", "redacted": redacted,
            "error": None, "is_repo": True,
            "derived": {"status": status, "attention": attention,
                        "open_items": open_items, "roadmap_items": roadmap_items,
                        "last_worked": last,
                        "attention_reasons": list(reasons)}}


def facts(*projects):
    return {"schema_version": 1, "scanned_at": NOW.isoformat(),
            "projects": list(projects), "errors": []}


def test_has_attention_and_recent_sections():
    out = render(facts(proj("a")), CFG, now=NOW)
    assert "## Attention" in out and "## Recent" in out


def test_attention_section_lists_reasons():
    out = render(facts(proj("a", attention=80, reasons=["3 uncommitted file(s)"])),
                 CFG, now=NOW)
    assert "3 uncommitted file(s)" in out


def test_projects_are_grouped_under_category_headings():
    out = render(facts(proj("a", category="Acme"), proj("b", category="MISC")),
                 CFG, now=NOW)
    assert "### MISC" in out and "### Acme" in out
    assert out.index("### Acme") < out.index("### MISC")  # alphabetical


def test_dormant_projects_appear_in_the_per_category_tables():
    out = render(
        facts(
            proj("sleepy", status="dormant", attention=0),
            proj("busy", status="active", attention=50),
        ),
        CFG, now=NOW,
    )
    # Slice to the per-category section. A whole-document check passes even with
    # dormant filtering reintroduced here, because the project also appears in
    # Attention and Recent above — attention=0 keeps it out of Attention, and the
    # slice keeps Recent out of scope. INDEX.md is the full record; the terminal
    # table is the one that hides dormant.
    all_projects = out.split("## All projects", 1)[1]
    assert "sleepy" in all_projects
    assert "busy" in all_projects


def test_roadmap_items_are_reported_separately_from_open_items():
    out = render(
        facts(
            proj("with-roadmap", category="X", roadmap_items=7),
            proj("without-roadmap", category="X", roadmap_items=0),
        ),
        CFG, now=NOW,
    )
    per_category = out.split("## All projects", 1)[1]
    with_line = next(
        line for line in per_category.splitlines() if "Open items" in line
        and "roadmap: 7" in line
    )
    without_line = next(
        line for line in per_category.splitlines() if "Open items" in line
        and "roadmap: 0" in line
    )
    assert with_line and without_line


def test_project_section_shows_description_before_its_stats():
    p = proj("a", category="Cat")
    p["path"] = str(CFG.settings.root.resolve() / "Cat" / "a")
    descriptions = {"Cat/a": Description(text="Does a specific thing.", source="ai", prompt_hash="sha256:x")}
    out = render(facts(p), CFG, now=NOW, descriptions=descriptions)
    assert "#### a" in out
    tail = out[out.index("#### a"):]
    assert "Does a specific thing." in tail
    assert tail.index("Does a specific thing.") < tail.index("- Status:")


def test_project_without_a_description_gets_a_placeholder_not_nothing():
    # Contrast with the test above: an UNDESCRIBED project must not render a
    # blank line where the description belongs -- the reader needs to be able
    # to tell "not described yet" apart from "described as empty".
    p = proj("b", category="Cat")
    out = render(facts(p), CFG, now=NOW, descriptions={})
    assert "No description yet" in out


def test_attention_and_recent_link_to_the_project_heading_not_a_dead_brief_path():
    # state/briefs/<slug>.md does not exist until Phase 2 -- linking there
    # renders as a dead link today. The anchor must resolve to the real
    # "#### <name>" heading this same render emits.
    out = render(facts(proj("web-console", category="Acme", attention=80)),
                 CFG, now=NOW)
    assert "state/briefs/" not in out
    assert "#### web-console" in out
    assert "(#web-console)" in out


def test_anchor_collision_across_categories_gets_a_numeric_suffix_pointing_at_the_right_project():
    # Two projects can share a display name across categories (this workspace
    # really does have Remote-VS-Code and remote-vs-code). Both halves matter:
    # the first heading in document order keeps the bare slug, the second
    # gets "-1" -- and each project's own Attention row must link to ITS OWN
    # anchor, not its collision partner's.
    first = proj("Remote-VS-Code", category="AAA", attention=50)   # category sorts first
    second = proj("remote-vs-code", category="ZZZ", attention=90)  # category sorts second
    out = render(facts(first, second), CFG, now=NOW)
    assert "#### Remote-VS-Code" in out and "#### remote-vs-code" in out

    attention_section = out.split("## Attention", 1)[1].split("## Recent", 1)[0]
    rows = [l for l in attention_section.splitlines()
            if l.startswith("| ") and "Project" not in l and "---" not in l]
    row_90 = next(r for r in rows if "| 90 " in r)
    row_50 = next(r for r in rows if "| 50 " in r)
    assert "(#remote-vs-code-1)" in row_90
    assert "(#remote-vs-code)" in row_50 and "(#remote-vs-code-1)" not in row_50


def test_redacted_project_with_no_name_falls_back_to_its_slug():
    # A redacted record's real name is null (scan.py's redact_record nulls it
    # — a doc-only project promoted purely by discovery can be named after the
    # client itself). Every section that shows a project name must fall back
    # to the digest slug rather than print "None" or crash the sort.
    p = proj("ignored", redacted=True, attention=50)
    p["name"] = None
    out = render(facts(p), CFG, now=NOW)
    assert p["slug"] in out
    assert "None" not in out


def test_redacted_projects_appear_without_a_path_or_narrative():
    out = render(facts(proj("client-thing", redacted=True)), CFG, now=NOW)
    assert "client-thing" in out
    assert "redacted" in out.lower()


def test_a_non_redacted_project_still_shows_its_path():
    out = render(
        facts(proj("open-thing", redacted=False), proj("client-thing", redacted=True)),
        CFG, now=NOW,
    )
    # Both halves load-bearing: the existing test rules out "never redact",
    # this one rules out "always redact". Neither alone is sufficient.
    assert "open-thing" in out and "_redacted_" in out
    assert proj("open-thing")["display_path"] in out


def test_output_is_deterministic_and_actually_reflects_its_input():
    f = facts(proj("alpha", attention=30), proj("beta", attention=10))
    once, twice = render(f, CFG, now=NOW), render(f, CFG, now=NOW)
    assert once == twice
    # A constant-string implementation satisfies the equality above trivially.
    # These two assertions are what make the test about this input rather than
    # about determinism in the abstract.
    assert "alpha" in once and "beta" in once
    assert render(facts(proj("gamma", attention=30)), CFG, now=NOW) != once


# commit_index builds its subprocess env from os.environ, so the identity has to
# be set there — not passed to a local subprocess.run — for the commit to succeed
# with the ambient git config neutralized.
GIT_IDENTITY = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def init_repo(tmp_path, monkeypatch):
    for k, v in GIT_IDENTITY.items():
        monkeypatch.setenv(k, v)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],
                   check=True, capture_output=True)
    return tmp_path


def test_commit_index_stages_only_the_named_paths(tmp_path, monkeypatch):
    repo = init_repo(tmp_path, monkeypatch)
    (repo / "INDEX.md").write_text("# Index\n")
    (repo / "UNRELATED.txt").write_text("work in progress\n")
    assert commit_index(repo, [repo / "INDEX.md"], "chore: index") is True
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    assert "UNRELATED.txt" in out   # untouched, still uncommitted
    assert "INDEX.md" not in out    # committed


def test_commit_index_is_a_noop_when_nothing_changed(tmp_path, monkeypatch):
    repo = init_repo(tmp_path, monkeypatch)
    (repo / "INDEX.md").write_text("# Index\n")
    commit_index(repo, [repo / "INDEX.md"], "chore: index")
    assert commit_index(repo, [repo / "INDEX.md"], "chore: index") is False


def _run_main(repo, monkeypatch, argv=()):
    # main() reads and writes everything under PSUM_HOME, so pointing that at a
    # scratch repo is what makes main() testable without touching this checkout's
    # own state/. It also exercises the real mechanism: before PSUM_HOME existed
    # these tests had to monkeypatch the module's __file__, which tested the
    # trick rather than the behaviour.
    monkeypatch.setenv("PSUM_HOME", str(repo))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = render_index_mod.main(list(argv))
    return rc, buf.getvalue()


def test_main_reports_committed_when_a_commit_actually_happens(tmp_path, monkeypatch):
    # MINOR fix: commit_index's return value used to be discarded and main()
    # printed "INDEX.md updated" unconditionally whenever the file changed —
    # even if the git commit itself no-opped or failed. This proves the two
    # are distinguished, and along the way that facts.json is read from inside
    # the lock rather than before it (main() would raise/misbehave here if the
    # read happened before state/ existed).
    repo = init_repo(tmp_path, monkeypatch)
    (repo / "scripts").mkdir()
    (repo / "state").mkdir()
    (repo / "state" / "facts.json").write_text(json.dumps(facts(proj("a"))))
    rc, out = _run_main(repo, monkeypatch)
    assert rc == 0
    assert "updated and committed" in out
    assert (repo / "INDEX.md").exists()
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "chore(index):" in log


def test_main_reports_unchanged_without_touching_git_on_a_second_run(tmp_path, monkeypatch):
    repo = init_repo(tmp_path, monkeypatch)
    (repo / "scripts").mkdir()
    (repo / "state").mkdir()
    (repo / "state" / "facts.json").write_text(json.dumps(facts(proj("a"))))
    _run_main(repo, monkeypatch)
    rc, out = _run_main(repo, monkeypatch)
    assert rc == 0
    assert "INDEX.md unchanged" in out


def test_main_reports_a_missing_facts_file_instead_of_a_traceback(tmp_path, monkeypatch):
    repo = init_repo(tmp_path, monkeypatch)
    (repo / "scripts").mkdir()
    rc, out = _run_main(repo, monkeypatch)
    assert rc == 1


def test_vault_index_name_has_no_whitespace():
    """pvault splits each config line on its first whitespace run, so a
    vault-path may contain spaces but a SOURCE path may not. A space here
    mounts the file as everything up to the space and then reports the source
    as missing -- with a zero exit code, so nothing surfaces the failure.
    """
    # The whitespace rule now lives with the filenames themselves, in
    # scripts/paths.py — see test_paths.test_vault_filenames_contain_no_whitespace.
    assert render_index_mod.vault_index_path().name.endswith(".md")
