"""Collect git facts for one repository.

Every call is bounded by a timeout and runs with GIT_OPTIONAL_LOCKS=0 so a
locked or wedged repo degrades to an error field rather than stalling a scan
of eighty repositories.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from scripts.discovery import is_excluded

_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def _run(repo: Path, *args: str) -> str | None:
    """Return stdout, or None when git exits non-zero, times out, or is absent."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=_ENV,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


def _default_branch(repo: Path) -> str | None:
    head = _run(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head:
        return head.strip().rsplit("/", 1)[-1]
    for name in ("main", "master"):
        if _run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return None


def _remote_slug(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip().removesuffix(".git")
    for sep in ("://", ":"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[1]
    parts = [p for p in cleaned.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


#: Above this size, an uncommitted file is fingerprinted by (size, mtime) rather
#: than by hashing its bytes. Hashing a 300MB working-tree file measured 0.66s,
#: against a whole-workspace target of a few seconds across ~80 repositories.
#: This is the ONLY place mtime is allowed to influence the content hash, and it
#: is deliberately narrow: the no-mtime rule exists so that touching an unchanged
#: file cannot cost tokens, and a touched-but-unchanged multi-megabyte
#: uncommitted file is a far rarer event than a touched source file.
LARGE_FILE_BYTES = 4 * 1024 * 1024

#: Total bytes psum will read to build ONE repository's digest. Past this, the
#: remaining entries are fingerprinted by (size, mtime_ns) instead of by their
#: contents. This bounds both runaway axes with one rule: a single enormous file
#: (already capped by LARGE_FILE_BYTES) and tens of thousands of small ones.
#: The second axis is real only because --untracked-files=all expands an
#: unignored scratch directory into one entry per file — the cost of fixing the
#: collapsed-directory blind spot.
DIGEST_READ_BUDGET_BYTES = 32 * 1024 * 1024


def _fingerprint(path: Path, budget_left: int) -> tuple[str, int]:
    """Fingerprint one working-tree path. Returns (fingerprint, bytes_read).

    Falls back to a stat-based fingerprint when the file exceeds
    LARGE_FILE_BYTES or when the per-repository read budget cannot cover it, so
    that neither one enormous file nor a directory full of small ones can
    dominate a whole-workspace scan.
    """
    try:
        st = path.stat()
    except OSError:
        return "ABSENT", 0
    if not path.is_file():
        return "NOTFILE", 0
    if st.st_size > LARGE_FILE_BYTES or st.st_size > budget_left:
        return f"stat:{st.st_size}:{st.st_mtime_ns}", 0
    try:
        data = path.read_bytes()
    except OSError:
        return "UNREADABLE", 0
    return "sha:" + hashlib.sha256(data).hexdigest(), len(data)


def _entry_path(line: str) -> str:
    """The working-tree path a porcelain line refers to.

    Rename and copy lines are formatted `R  old -> new` / `C  old -> new`; the
    file that exists on disk is the right-hand side. Treating the whole string
    as a path yields something that cannot be read, which silently degrades the
    entry to a constant and freezes the digest for that file.
    """
    rel = line[3:].strip()
    if " -> " in rel:
        rel = rel.split(" -> ", 1)[1]
    return rel.strip().strip('"')


def _porcelain(
    repo: Path, excludes: Iterable[Path], exclude_globs: tuple[str, ...] = ()
) -> tuple[int, int, str]:
    """Return (dirty_files, untracked_files, sha256 of the filtered working tree).

    A porcelain *status line* like " M file0.txt" does not change when the file's
    content changes further while it stays modified — the status code is stable,
    only the bytes on disk move. Hashing status lines alone would leave the
    digest stale exactly when there is new uncommitted work to report. So each
    kept entry is paired with a fingerprint of its current on-disk bytes (or a
    fixed marker when the path no longer exists, e.g. a deletion). Entries are
    sorted before fingerprinting (not after) and hashed in that order —
    content-based and order-stable, and see the read-budget note below for why
    the sort has to happen first.

    Untracked directories are expanded to one line per file (`--untracked-files
    =all`) rather than collapsed to a single `?? dir/` entry: an unreadable
    directory would otherwise fingerprint as a constant, freezing the digest for
    everything inside it regardless of what changes there.

    This reads a `git status` snapshot and then re-reads each file's bytes in a
    second pass; a concurrent write between the two passes can produce a digest
    that does not exactly match the status line it was paired with. Not made
    atomic — the window is a live edit racing a scan, and a slightly stale
    digest at that boundary is an acceptable trade against locking or snapshotting.

    `--untracked-files=all` is bounded only by git's own ignore rules, not by
    `exclude_globs` — an unignored `node_modules/` produces one porcelain line,
    one `resolve()`, and one `stat()`/read per vendor file, with nothing
    bounding the entry count the way `DIGEST_READ_BUDGET_BYTES` bounds bytes.
    Every kept line is therefore also run through `discovery.is_excluded`, the
    same predicate the directory walk and the doc scan already use — a second,
    independent exclusion check here would be exactly how a vendor tree stays
    invisible to one and visible to the other.
    """
    raw = _run(repo, "status", "--porcelain=v1", "--untracked-files=all") or ""
    ex = [str(Path(e).resolve()) for e in excludes]
    kept: list[str] = []
    for line in raw.splitlines():
        rel = _entry_path(line)
        full_path = repo / rel
        full = str(full_path.resolve())
        if any(full == e or full.startswith(e + "/") for e in ex):
            continue
        if is_excluded(rel, exclude_globs):
            continue
        kept.append(line)
    dirty = sum(1 for line in kept if not line.startswith("??"))
    untracked = sum(1 for line in kept if line.startswith("??"))

    # Sorted before fingerprinting, not after: this is what makes which files
    # get the real read (vs. the stat fallback once the budget runs out)
    # deterministic. Fingerprinting in git's arbitrary status order would let
    # the same working-tree state produce a different digest on different
    # runs, which would trigger phantom re-briefs forever.
    budget = DIGEST_READ_BUDGET_BYTES
    entries: list[str] = []
    for line in sorted(kept):
        fingerprint, used = _fingerprint(repo / _entry_path(line), budget)
        budget -= used
        entries.append(f"{line}\x00{fingerprint}")
    digest = hashlib.sha256("\n".join(entries).encode()).hexdigest()
    return dirty, untracked, digest


def _count_since(repo: Path, days: int, now: datetime) -> int:
    since = (now - timedelta(days=days)).isoformat()
    out = _run(repo, "rev-list", "--count", "HEAD", f"--since={since}")
    return int(out.strip()) if out and out.strip().isdigit() else 0


def _count_window_ending(repo: Path, until_iso: str | None, days: int) -> int:
    """Commits in the `days`-day window ending at `until_iso` (a repo's own
    last commit timestamp), NOT at "now".

    `commits_90d` (via `_count_since`) answers "how much git activity in the
    last 90 days as of THIS SCAN" — which is exactly 0 for anything quiet
    longer than 90 days, by construction. That makes the attention score's
    stall term go silent precisely where "what did I forget" matters most: a
    project hot for weeks and then abandoned five months ago scores the same
    zero intensity as a project that was always slow. Anchoring the window to
    the repo's own last commit instead answers "how hot was this before it
    went quiet", which stays meaningful no matter how long ago that was.
    """
    if not until_iso:
        return 0
    try:
        until_dt = datetime.fromisoformat(until_iso)
    except ValueError:
        return 0
    since = (until_dt - timedelta(days=days)).isoformat()
    out = _run(repo, "rev-list", "--count", "HEAD", f"--since={since}", f"--until={until_iso}")
    return int(out.strip()) if out and out.strip().isdigit() else 0


def collect_git(
    path: Path,
    excludes: Iterable[Path],
    now: datetime,
    exclude_globs: tuple[str, ...] = (),
) -> dict | None:
    if not (path / ".git").exists():
        return None

    excludes = list(excludes)
    head_sha = _run(path, "rev-parse", "HEAD")
    head_sha = head_sha.strip() if head_sha else None
    branch = _run(path, "rev-parse", "--abbrev-ref", "HEAD")
    default = _default_branch(path)
    remote = _run(path, "remote", "get-url", "origin")
    remote = remote.strip() if remote else None

    error = None
    if head_sha is None:
        # An empty repo legitimately has no HEAD commit. A corrupt one has refs
        # that rev-list can still walk, so the two are distinguishable — and
        # only the second is an error worth reporting.
        reachable = _run(path, "rev-list", "--all", "--count")
        if reachable is None or (reachable.strip().isdigit() and int(reachable.strip()) > 0):
            error = "HEAD is unreadable but commits exist; repository looks corrupt"

    last_at = last_subject = None
    if head_sha:
        meta = _run(path, "log", "-1", "--format=%cI%x00%s")
        if meta and "\x00" in meta:
            last_at, last_subject = meta.strip().split("\x00", 1)

    unpushed = 0
    if head_sha and remote and branch:
        out = _run(path, "rev-list", "--count", f"origin/{branch.strip()}..HEAD")
        unpushed = int(out.strip()) if out and out.strip().isdigit() else 0

    ahead: list[str] = []
    if head_sha and default:
        out = _run(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        for name in (out or "").split():
            if name == default:
                continue
            cnt = _run(path, "rev-list", "--count", f"{default}..{name}")
            if cnt and cnt.strip().isdigit() and int(cnt.strip()) > 0:
                ahead.append(name)

    dirty, untracked, digest = _porcelain(path, excludes, exclude_globs)
    stash = _run(path, "stash", "list") or ""
    worktrees = [
        line.split(" ", 1)[1]
        for line in (_run(path, "worktree", "list", "--porcelain") or "").splitlines()
        if line.startswith("worktree ")
    ]

    return {
        "branch": branch.strip() if branch else None,
        "default_branch": default,
        "remote": remote,
        "remote_slug": _remote_slug(remote),
        "head_sha": head_sha,
        "last_commit_at": last_at,
        "last_commit_subject": last_subject,
        "commits_30d": _count_since(path, 30, now) if head_sha else 0,
        "commits_90d": _count_since(path, 90, now) if head_sha else 0,
        "commits_90d_anchored": _count_window_ending(path, last_at, 90) if head_sha else 0,
        "dirty_files": dirty,
        "untracked_files": untracked,
        "porcelain_digest": digest,
        "unpushed": unpushed,
        "branches_ahead": sorted(ahead),
        "worktrees": worktrees[1:],  # index 0 is the main working tree itself
        "stashes": len([line for line in stash.splitlines() if line.strip()]),
        "error": error,
    }
