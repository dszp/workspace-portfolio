from pathlib import Path
from scripts.docs import collect_docs, count_open_items
from tests.fixtures.make_tree import build_tree


def test_count_open_items_counts_only_unchecked_boxes():
    text = "- [ ] one\n- [x] done\n- [ ] two\n  - [ ] nested\nplain bullet\n"
    assert count_open_items(text) == 3


def test_count_open_items_ignores_boxes_inside_fenced_code():
    text = "- [ ] real\n```\n- [ ] not real\n```\n- [ ] also real\n"
    assert count_open_items(text) == 2


def test_backlog_files_are_found_and_totalled(tmp_path):
    build_tree(tmp_path)
    d = collect_docs(tmp_path / "outer-repo", [tmp_path / "outer-repo" / "inner-repo"], (), False)
    assert [b["path"] for b in d["backlog_files"]] == ["BACKLOG.md"]
    assert d["open_items"] == 1  # the inner repo's two items must not be counted


def test_handoff_presence_is_flagged(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "HANDOFF.md").write_text("Stopped mid-refactor.\n")
    d = collect_docs(repo, [], (), False)
    assert d["has_handoff"] is True


def test_plans_are_parsed_with_date_title_and_box_counts_but_do_not_feed_open_items(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    plans = repo / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-08-15-slice-one.md").write_text(
        "# Slice One Plan\n- [x] a\n- [ ] b\n- [ ] c\n"
    )
    d = collect_docs(repo, [], (), False)
    assert d["plans"][0]["date"] == "2026-08-15"
    assert d["plans"][0]["title"] == "Slice One Plan"
    assert (d["plans"][0]["checked"], d["plans"][0]["unchecked"]) == (1, 2)
    assert d["half_checked_plan"] is True
    # A plan's own unchecked count is retained on its record (half_checked_plan
    # reads it above) but no longer summed into open_items: a half-executed plan
    # from weeks ago is not near-term work still owed.
    assert d["open_items"] == 0


def test_fully_checked_plan_is_not_half_checked(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    plans = repo / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-08-15-done.md").write_text("# Done\n- [x] a\n- [x] b\n")
    d = collect_docs(repo, [], (), False)
    assert d["half_checked_plan"] is False
    assert d["open_items"] == 0


def test_generated_globs_are_excluded_for_this_repo(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "state").mkdir()
    (repo / "state" / "BACKLOG.md").write_text("- [ ] generated noise\n")
    d = collect_docs(repo, [], ("state/**",), True)
    assert d["backlog_files"] == []
    assert d["open_items"] == 0


def test_headline_docs_are_detected_with_readme_priority(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "CLAUDE.md").write_text("# guide\n")
    (repo / "CHANGELOG.md").write_text("# changes\n")
    (repo / "readme.md").write_text("# lowercase\n")
    d = collect_docs(repo, [], (), False)
    assert d["claude_md"]["path"] == "CLAUDE.md"
    assert d["changelog"]["path"] == "CHANGELOG.md"
    assert d["readme"]["path"] == "readme.md"      # the only candidate present
    (repo / "README.md").write_text("# canonical\n")
    # README.md outranks readme.md: find_one returns the FIRST name that exists,
    # so the priority order in its argument list is the behaviour under test.
    assert collect_docs(repo, [], (), False)["readme"]["path"] == "README.md"


def test_headline_doc_sha_tracks_content_not_just_presence(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "CLAUDE.md").write_text("# guide\n")
    before = collect_docs(repo, [], (), False)["claude_md"]["sha"]
    (repo / "CLAUDE.md").write_text("# guide, revised\n")
    after = collect_docs(repo, [], (), False)["claude_md"]["sha"]
    # hashing.py consumes this sha to decide whether a project needs re-summarizing.
    # A sha that reported only presence would freeze every project's brief.
    assert before != after


def test_headline_doc_respects_generated_globs_when_self_repo(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "CLAUDE.md").write_text("# guide\n")
    assert collect_docs(repo, [], ("CLAUDE.md",), True)["claude_md"] is None


def test_backlog_globs_do_not_descend_into_vendor_trees(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    vendor = repo / "node_modules" / "smart-buffer" / "docs"
    vendor.mkdir(parents=True)
    (vendor / "ROADMAP.md").write_text("- [ ] a dependency's own roadmap item\n")
    d = collect_docs(repo, [], (), False, exclude_globs=("**/node_modules/**",))
    # A dependency's roadmap must not become this project's backlog, and must not
    # inflate open_items — which feeds derived.status and the attention score.
    assert d["backlog_files"] == []
    assert d["open_items"] == 0


def test_specs_are_parsed_with_box_counts_but_feed_neither_open_items_nor_half_checked_plan(
    tmp_path,
):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    specs = repo / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-08-10-design.md").write_text("# Design\n- [x] a\n- [ ] b\n")
    d = collect_docs(repo, [], (), False)
    assert d["specs"][0]["date"] == "2026-08-10"
    assert d["specs"][0]["title"] == "Design"
    # The spec's own unchecked count is retained on its record, but a spec's
    # boxes are neither near-term obligation nor a half-executed-plan signal, so
    # it feeds neither open_items nor half_checked_plan.
    assert d["specs"][0]["unchecked"] == 1
    assert d["open_items"] == 0
    # half_checked_plan is a PLANS-only signal: a half-executed plan means work
    # was started and abandoned, which a partially-checked spec does not imply.
    assert d["half_checked_plan"] is False


def test_roadmap_and_backlog_items_are_counted_into_separate_totals(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "BACKLOG.md").write_text("- [ ] near-term one\n- [ ] near-term two\n")
    (repo / "ROADMAP.md").write_text(
        "- [ ] someday one\n- [ ] someday two\n- [ ] someday three\n"
    )
    d = collect_docs(repo, [], (), False)
    # Both directions in one fixture: BACKLOG.md's items land in open_items and
    # nowhere else; ROADMAP.md's items land in roadmap_items and nowhere else.
    assert d["open_items"] == 2
    assert d["roadmap_items"] == 3
    # Both files are still present in backlog_files with their existing shape —
    # the split changes which total each feeds, not whether it is inventoried.
    kinds = {b["path"]: b["kind"] for b in d["backlog_files"]}
    assert kinds == {"BACKLOG.md": "backlog", "ROADMAP.md": "roadmap"}
