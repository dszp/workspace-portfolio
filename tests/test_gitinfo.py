import os
from datetime import datetime
from pathlib import Path
from scripts import gitinfo
from scripts.gitinfo import DIGEST_READ_BUDGET_BYTES, LARGE_FILE_BYTES, _fingerprint, collect_git
from tests.fixtures.make_tree import _git, _repo, build_tree

NOW = datetime.fromisoformat("2026-08-18T19:00:00-04:00")


def test_non_repo_returns_none(tmp_path):
    (tmp_path / "plain").mkdir()
    assert collect_git(tmp_path / "plain", [], NOW) is None


def test_plain_repo_reports_head_and_commit_counts(tmp_path):
    build_tree(tmp_path)
    g = collect_git(tmp_path / "plain-repo", [], NOW)
    assert g["branch"] == "main"
    assert len(g["head_sha"]) == 40
    assert g["last_commit_subject"] == "commit 1"
    assert g["commits_90d"] == 2
    assert g["dirty_files"] == 0


def test_dirty_repo_reports_counts_and_a_porcelain_digest(tmp_path):
    build_tree(tmp_path)
    g = collect_git(tmp_path / "dirty-repo", [], NOW)
    assert g["dirty_files"] == 1
    assert g["untracked_files"] == 1
    assert len(g["porcelain_digest"]) == 64


def test_porcelain_digest_changes_when_content_changes_at_constant_count(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "dirty-repo"
    first = collect_git(repo, [], NOW)["porcelain_digest"]
    (repo / "file0.txt").write_text("changed again but still one dirty file\n")
    second = collect_git(repo, [], NOW)["porcelain_digest"]
    # Same counts, different content: the digest must move, the counts must not.
    assert collect_git(repo, [], NOW)["dirty_files"] == 1
    assert first != second


def test_repo_with_no_commits_does_not_raise(tmp_path):
    build_tree(tmp_path)
    g = collect_git(tmp_path / "empty-repo", [], NOW)
    assert g["head_sha"] is None
    assert g["commits_90d"] == 0
    assert g["last_commit_at"] is None


def test_commits_90d_anchored_counts_around_the_last_commit_not_now(tmp_path):
    # NOW is 2026-08-18; commits from 2020 fall well outside a 90-day window
    # ending now, but all land the same day, well inside a 90-day window ending
    # at the repo's own last commit — which is what "how hot was this before
    # it went quiet" has to measure to stay useful past the 90-day mark.
    old = _repo(tmp_path / "old-repo", commits=3, when="2020-01-01T12:00:00-04:00")
    g = collect_git(old, [], NOW)
    assert g["commits_90d"] == 0
    assert g["commits_90d_anchored"] == 3


def test_porcelain_excludes_vendor_dirs_unconditionally(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    vendor = repo / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    for i in range(5):
        (vendor / f"f{i}.js").write_text("x")
    g = collect_git(repo, [], NOW)
    # node_modules is unconditionally vendor noise (VENDOR_DIRS), independent
    # of exclude_globs, so an unignored dependency install must not turn into
    # one porcelain line, one resolve(), and one read per vendor file.
    assert g["untracked_files"] == 0


def test_porcelain_respects_configured_exclude_globs(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    # One level deep so the "**/dist/**" glob has a path component to match
    # before "dist" — fnmatch has no directory-boundary semantics for "**", so
    # a root-level "dist/" would not match that pattern at all; this mirrors
    # how the existing default globs ("**/node_modules/**" etc.) are shaped.
    dist = repo / "app" / "dist"
    dist.mkdir(parents=True)
    (dist / "bundle.js").write_text("x")
    without_globs = collect_git(repo, [], NOW)
    assert without_globs["untracked_files"] == 1
    with_globs = collect_git(repo, [], NOW, ("**/dist/**",))
    assert with_globs["untracked_files"] == 0


def test_nested_repo_is_excluded_from_outer_porcelain(tmp_path):
    build_tree(tmp_path)
    outer = tmp_path / "outer-repo"
    # outer-repo already carries its own untracked BACKLOG.md (fixture-provided,
    # for discovery's markdown-claiming tests), which is unrelated to the nested
    # repo and must still be counted. git never descends into a nested repo's
    # working tree, so it reports "inner-repo/" as a single untracked entry
    # regardless of what's inside it — the exclusion must strip exactly that one
    # sentinel entry, no more and no less, whether or not it has new content.
    (outer / "inner-repo" / "scratch.txt").write_text("noise\n")
    included = collect_git(outer, [], NOW)["untracked_files"]
    excluded = collect_git(outer, [outer / "inner-repo"], NOW)["untracked_files"]
    assert excluded == included - 1


def test_corrupt_repo_returns_an_error_key_instead_of_raising(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / ".git" / "HEAD").write_text("garbage\n")
    g = collect_git(repo, [], NOW)
    assert g["error"] is not None


def test_digest_moves_when_a_file_inside_an_untracked_directory_changes(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "scratch").mkdir()
    (repo / "scratch" / "note.txt").write_text("first\n")
    first = collect_git(repo, [], NOW)["porcelain_digest"]
    (repo / "scratch" / "note.txt").write_text("second, entirely different\n")
    second = collect_git(repo, [], NOW)["porcelain_digest"]
    # An untracked DIRECTORY collapses to one unreadable entry under
    # --untracked-files=normal, which froze the digest exactly as hashing status
    # lines alone did. Listing untracked files individually is what fixes it.
    assert first != second


def test_digest_moves_when_a_renamed_files_content_changes(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    _git(repo, "mv", "file0.txt", "renamed.txt")
    # Edit once FIRST so the status line is already "RM old -> new". A further
    # edit leaves that line byte-identical, so only the file's content can move
    # the digest. Comparing across the first edit instead would be vacuous: the
    # worktree status code flips R -> RM on its own, moving the digest even when
    # _entry_path is broken and every fingerprint is the constant "ABSENT".
    (repo / "renamed.txt").write_text("first edit after the rename\n")
    first = collect_git(repo, [], NOW)["porcelain_digest"]
    (repo / "renamed.txt").write_text("second edit, entirely different\n")
    second = collect_git(repo, [], NOW)["porcelain_digest"]
    assert first != second


def test_large_uncommitted_file_uses_the_stat_fallback(tmp_path):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    big = repo / "big.bin"
    big.write_bytes(b"\0" * (LARGE_FILE_BYTES + 1))
    fingerprint, used = _fingerprint(big, DIGEST_READ_BUDGET_BYTES)
    assert fingerprint.startswith("stat:") and used == 0
    small = repo / "small.bin"
    small.write_bytes(b"\0" * 16)
    fingerprint, used = _fingerprint(small, DIGEST_READ_BUDGET_BYTES)
    assert fingerprint.startswith("sha:") and used == 16


def test_fingerprint_falls_back_to_stat_when_the_read_budget_cannot_cover_the_file(tmp_path):
    f = tmp_path / "small.txt"
    f.write_bytes(b"x" * 1024)
    full, used = _fingerprint(f, DIGEST_READ_BUDGET_BYTES)
    assert full.startswith("sha:") and used == 1024
    starved, used_starved = _fingerprint(f, 512)
    assert starved.startswith("stat:") and used_starved == 0


def test_entries_past_the_read_budget_are_fingerprinted_by_stat(tmp_path, monkeypatch):
    build_tree(tmp_path)
    repo = tmp_path / "plain-repo"
    (repo / "aaa.bin").write_bytes(b"a" * 2048)
    (repo / "zzz.bin").write_bytes(b"z" * 2048)
    monkeypatch.setattr(gitinfo, "DIGEST_READ_BUDGET_BYTES", 2048)
    first = collect_git(repo, [], NOW)["porcelain_digest"]
    # "zzz.bin" sorts last, so the budget is spent before it is reached and it is
    # fingerprinted by (size, mtime_ns). Rewriting it with the same size and the
    # same mtime therefore does NOT move the digest. That is the price of the
    # budget, and this test states it plainly rather than leaving it implied.
    before = (repo / "zzz.bin").stat()
    (repo / "zzz.bin").write_bytes(b"q" * 2048)
    os.utime(repo / "zzz.bin", ns=(before.st_atime_ns, before.st_mtime_ns))
    assert collect_git(repo, [], NOW)["porcelain_digest"] == first
