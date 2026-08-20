import copy
from scripts.docs import collect_docs
from scripts.hashing import content_hash
from tests.fixtures.make_tree import build_tree

BASE = {
    "git": {
        "head_sha": "abc123", "porcelain_digest": "d1", "unpushed": 0,
        "branches_ahead": [], "stashes": 0,
    },
    "fs": {"newest_mtime": "2026-08-18T10:00:00-04:00", "file_count": 100,
           "files_touched_90d": 5},
    "docs": {
        "claude_md": {"path": "CLAUDE.md", "sha": "c1", "mtime": "t"},
        "readme": None, "changelog": None,
        "backlog_files": [{"path": "TODO.md", "sha": "b1", "mtime": "t", "open_items": 2}],
        "plans": [{"path": "p.md", "sha": "p1", "checked": 1, "unchecked": 2, "mtime": "t"}],
        "specs": [],
    },
    "activity": {"remember_tail_sha": "r1", "claude_session_count": 3,
                 "herdr_open_panes": 0, "herdr_agent_status": "idle"},
    "derived": {"status": "active", "attention": 12},
}


def h(mutate=None):
    r = copy.deepcopy(BASE)
    if mutate:
        mutate(r)
    return content_hash(r)


def test_hash_is_prefixed_and_stable():
    assert h().startswith("sha256:")
    assert h() == h()


def test_hash_ignores_mtimes():
    assert h(lambda r: r["fs"].__setitem__("newest_mtime", "2027-01-01T00:00:00-04:00")) == h()
    assert h(lambda r: r["docs"]["claude_md"].__setitem__("mtime", "later")) == h()


def test_hash_ignores_herdr_and_session_activity():
    assert h(lambda r: r["activity"].__setitem__("herdr_open_panes", 9)) == h()
    assert h(lambda r: r["activity"].__setitem__("claude_session_count", 999)) == h()
    assert h(lambda r: r["fs"].__setitem__("file_count", 9999)) == h()


def test_hash_moves_on_porcelain_content_at_constant_counts():
    assert h(lambda r: r["git"].__setitem__("porcelain_digest", "d2")) != h()


def test_hash_moves_on_branches_ahead_and_stashes():
    assert h(lambda r: r["git"].__setitem__("branches_ahead", ["feat"])) != h()
    assert h(lambda r: r["git"].__setitem__("stashes", 1)) != h()


def test_hash_moves_when_status_flips_from_elapsed_time_alone():
    assert h(lambda r: r["derived"].__setitem__("status", "stalled")) != h()


def test_hash_moves_on_doc_and_plan_changes():
    assert h(lambda r: r["docs"]["plans"][0].__setitem__("unchecked", 1)) != h()
    assert h(lambda r: r["docs"]["claude_md"].__setitem__("sha", "c2")) != h()
    assert h(lambda r: r["activity"].__setitem__("remember_tail_sha", "r2")) != h()


def test_hash_is_order_independent_for_file_lists():
    def swap(r):
        r["docs"]["backlog_files"] = [
            {"path": "Z.md", "sha": "z", "mtime": "t", "open_items": 1},
            {"path": "A.md", "sha": "a", "mtime": "t", "open_items": 1},
        ]
    def sorted_order(r):
        r["docs"]["backlog_files"] = [
            {"path": "A.md", "sha": "a", "mtime": "t", "open_items": 1},
            {"path": "Z.md", "sha": "z", "mtime": "t", "open_items": 1},
        ]
    assert h(swap) == h(sorted_order)


def test_attention_alone_does_not_move_the_hash():
    # Attention is a continuous function of elapsed time; hashing it would
    # re-brief every project every day for no content reason.
    assert h(lambda r: r["derived"].__setitem__("attention", 99)) == h()


def test_hash_moves_on_unpushed_commits():
    # The third of the unpushed/branches_ahead/stashes triad. Dropping this term
    # from the payload passes every other test in this file.
    assert h(lambda r: r["git"].__setitem__("unpushed", 1)) != h()


def test_hash_tracks_readme_and_changelog_content_but_not_their_mtimes():
    def with_docs(r):
        r["docs"]["readme"] = {"path": "README.md", "sha": "rm1", "mtime": "t"}
        r["docs"]["changelog"] = {"path": "CHANGELOG.md", "sha": "cl1", "mtime": "t"}

    present = h(with_docs)

    def changed_readme(r):
        with_docs(r)
        r["docs"]["readme"]["sha"] = "rm2"

    def changed_changelog(r):
        with_docs(r)
        r["docs"]["changelog"]["sha"] = "cl2"

    def only_touched(r):
        with_docs(r)
        r["docs"]["readme"]["mtime"] = "much later"
        r["docs"]["changelog"]["mtime"] = "much later"

    assert h(changed_readme) != present
    assert h(changed_changelog) != present
    assert h(only_touched) == present


def test_hash_tracks_spec_box_counts_but_not_spec_mtime():
    def with_spec(r):
        r["docs"]["specs"] = [
            {"path": "s.md", "sha": "s1", "checked": 0, "unchecked": 3, "mtime": "t"}
        ]

    present = h(with_spec)

    def box_checked(r):
        with_spec(r)
        r["docs"]["specs"][0]["unchecked"] = 2

    def only_touched(r):
        with_spec(r)
        r["docs"]["specs"][0]["mtime"] = "much later"

    assert h(box_checked) != present
    assert h(only_touched) == present


def test_hash_moves_when_a_roadmap_files_content_changes():
    # ROADMAP*.md entries stay in docs.backlog_files (docs.py keeps their shape
    # and "kind"), and the hash's backlog term reads every entry regardless of
    # kind — so a changed roadmap file must still move the hash even though its
    # count no longer feeds open_items or attention.
    def with_roadmap(r):
        r["docs"]["backlog_files"] = list(r["docs"]["backlog_files"]) + [
            {"path": "ROADMAP.md", "sha": "rm1", "mtime": "t", "open_items": 5,
             "kind": "roadmap"}
        ]

    present = h(with_roadmap)

    def changed_roadmap(r):
        with_roadmap(r)
        r["docs"]["backlog_files"][-1]["sha"] = "rm2"

    def only_touched(r):
        with_roadmap(r)
        r["docs"]["backlog_files"][-1]["mtime"] = "much later"

    assert h(changed_roadmap) != present
    assert h(only_touched) == present


def test_a_changed_roadmap_file_still_moves_the_real_content_hash(tmp_path):
    # End-to-end: run the actual scripts.docs.collect_docs output (not a
    # synthetic payload) through content_hash, proving the docs.py split did
    # not move ROADMAP.md's sha out of the hash's reach.
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "ROADMAP.md").write_text("- [ ] someday feature\n")

    def record_for(docs):
        r = copy.deepcopy(BASE)
        r["docs"] = docs
        return r

    before = content_hash(record_for(collect_docs(repo, [], (), False)))
    (repo / "ROADMAP.md").write_text("- [ ] someday feature\n- [ ] another\n")
    after = content_hash(record_for(collect_docs(repo, [], (), False)))
    assert before != after
