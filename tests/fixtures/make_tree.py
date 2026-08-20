"""Build a fixture workspace exercising every discovery and git edge case.

Deterministic: all commits use a fixed author, fixed dates, and no signing,
so hashes and counts are reproducible across runs and machines.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
}


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(ENV)
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**env, "PATH": "/usr/bin:/bin"},
        timeout=10,
    )


def _backdate(path: Path, when: str) -> None:
    """Set a file's mtime (and atime) to a fixed, absolute timestamp — never
    relative to wall-clock "now" — so file-age-dependent fixtures stay
    reproducible regardless of when or on what machine the suite runs.
    """
    ts = datetime.fromisoformat(when).timestamp()
    os.utime(path, (ts, ts))


def _repo(path: Path, *, commits: int = 1, when: str = "2026-08-01T12:00:00-04:00") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    for i in range(commits):
        (path / f"file{i}.txt").write_text(f"content {i}\n")
        _git(path, "add", f"file{i}.txt")
        _git(path, "commit", "-q", "-m", f"commit {i}", when=when)
    return path


def build_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)

    plain = _repo(root / "plain-repo", commits=2)
    (root / "link-to-plain").symlink_to(plain, target_is_directory=True)

    dirty = _repo(root / "dirty-repo")
    (dirty / "file0.txt").write_text("changed\n")
    (dirty / "untracked.txt").write_text("new\n")

    empty = root / "empty-repo"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")   # zero commits

    outer = _repo(root / "outer-repo")
    _repo(outer / "inner-repo")
    (outer / "BACKLOG.md").write_text("- [ ] outer item\n")
    (outer / "inner-repo" / "BACKLOG.md").write_text("- [ ] inner item\n- [ ] second\n")

    # Group's children carry markdown ON PURPOSE. Without it,
    # test_category_container_is_not_a_record passes even if the claimed-subtree
    # subtraction is deleted outright — there would be no markdown under Group for
    # the rule to have to subtract. The READMEs are what make the assertion able
    # to fail.
    group = root / "Group"
    for child in ("child-a", "child-b"):
        c = _repo(group / child)
        (c / "README.md").write_text(f"# {child}\n")
        _git(c, "add", "README.md")
        _git(c, "commit", "-q", "-m", "readme", when="2026-08-01T12:00:00-04:00")

    noise = root / "Noise" / "pkg" / "node_modules" / "dep"
    noise.mkdir(parents=True)
    (noise / "README.md").write_text("# bundled dependency readme\n")

    docs_group = root / "Docs-Group"
    _repo(docs_group / "child-c")
    (docs_group / "overview.md").write_text("# Overview\n")

    deep = root / "clientsX" / "acme" / "deep-notes"
    deep.mkdir(parents=True)
    (deep / "notes.md").write_text("# Notes\n")

    nm = root / "noisy-repo"
    _repo(nm)
    (nm / "node_modules" / "pkg").mkdir(parents=True)
    (nm / "node_modules" / "pkg" / "readme.md").write_text("# vendor\n")

    # Non-repo projects, both quiet the same (long) time relative to the
    # suite's fixed NOW (2026-08-18) but with different historical busyness —
    # exercising files_touched_90d's anchor-to-own-newest-file fix rather than
    # an anchor at "now". All files in both directories share one backdated
    # mtime, so a "now"-anchored window (any 90-day slice ending at scan time)
    # sees zero touched files in either one; a window anchored at each
    # project's own newest file sees every file in once-busy-docs (all of them
    # land on the exact same day) and only the single file in
    # always-quiet-docs.
    once_busy = root / "once-busy-docs"
    once_busy.mkdir()
    for i in range(20):
        note = once_busy / f"note{i}.md"
        note.write_text(f"# note {i}\n")
        _backdate(note, "2025-11-01T12:00:00-04:00")

    always_quiet = root / "always-quiet-docs"
    always_quiet.mkdir()
    lone = always_quiet / "overview.md"
    lone.write_text("# Always Quiet\n")
    _backdate(lone, "2025-11-01T12:00:00-04:00")
