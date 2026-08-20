"""Find project records using a bottom-up claiming rule.

A directory is a project if it is a git repo, or if it still holds markdown of
its own after every already-claimed subtree is subtracted. Claiming is what
keeps category containers (which hold only their children's files) from
appearing as projects alongside those children.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scripts.config import Config


@dataclass(frozen=True)
class Candidate:
    path: Path
    is_repo: bool
    aliases: tuple[Path, ...] = ()
    claimed_excludes: tuple[Path, ...] = field(default=())


#: Directory names always treated as vendor noise, independently of exclude_globs.
#: Kept as a name set rather than globs because these must match at any depth and
#: users should not have to remember to re-add them when overriding exclude_globs.
VENDOR_DIRS = frozenset({"node_modules", ".venv", "vendor"})


def is_hidden(rel: str) -> bool:
    """True when any component of `rel` is a dot-entry.

    Dot-directories hold tool and editor state, not projects: `.claude/skills`,
    `.github`, `.vscode`, `.agents`, `.superpowers`. Inside a git repo none of
    them are visible to discovery anyway, because the repo claims its whole
    tree — but a CATEGORY CONTAINER is not a repo, so under one of those the
    dot-directories are unclaimed, and any of them holding markdown gets
    promoted to a project. That is how `Reports/.claude/skills/*` (two
    SKILL.md files) became two of the 86 records.

    This is deliberately NOT folded into `is_excluded`. That predicate also
    decides what counts as a project's own content for `gitinfo` (the porcelain
    digest) and `docs` — and an uncommitted edit under `.github/workflows` is
    real work that must still move the content hash. The question here is
    narrower: what may become a RECORD. Both discovery call sites below go
    through this one function, for the same reason `is_excluded` exists.
    """
    return any(part.startswith(".") for part in PurePosixPath(rel).parts)


def is_excluded(rel: str, globs: tuple[str, ...]) -> bool:
    """The single exclusion rule. Both the directory walk and the markdown scan
    MUST go through this — two copies of this predicate is precisely how an
    excluded subtree ends up invisible to one and visible to the other.
    """
    parts = PurePosixPath(rel).parts
    if any(part in VENDOR_DIRS for part in parts):
        return True
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/*")) for g in globs)


def _walk_dirs(root: Path, max_depth: int, globs: tuple[str, ...]) -> list[Path]:
    """Directories under root, deepest-first, excluding glob matches and .git internals."""
    out: list[Path] = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = [e for e in d.iterdir() if e.is_dir() and not e.is_symlink()]
        except (PermissionError, OSError):
            continue
        for e in entries:
            rel = str(e.relative_to(root))
            # is_hidden subsumes the old `.git` name check.
            if is_hidden(rel) or is_excluded(rel, globs):
                continue
            out.append(e)
            stack.append((e, depth + 1))
    out.sort(key=lambda p: len(p.parts), reverse=True)
    return out


def _has_unclaimed_markdown(
    d: Path, claimed: set[Path], root: Path, globs: tuple[str, ...]
) -> bool:
    """True when d holds markdown that no record has claimed and no rule excludes.

    Hidden paths are filtered here as well as in the walk. Filtering only in the
    walk would stop `.claude/skills/x` from BECOMING a record while still
    letting its SKILL.md promote the container above it — the two-copies-of-the-
    predicate failure this module already warns about, arriving by a new route.

    The exclusion check is not optional. `_walk_dirs` never descends into vendor
    trees, so nothing inside one is ever claimed — and markdown that is neither
    claimed nor excluded reads as "this directory has content of its own". A
    wrapper directory whose only markdown is a bundled node_modules README would
    otherwise be promoted to a project.
    """
    for md in d.rglob("*.md"):
        if any(c == md or c in md.parents for c in claimed):
            continue
        try:
            rel = str(md.relative_to(root))
        except ValueError:
            continue
        if is_hidden(rel) or is_excluded(rel, globs):
            continue
        return True
    return False


def _symlink_aliases(root: Path, targets: set[Path]) -> dict[Path, list[Path]]:
    aliases: dict[Path, list[Path]] = {}
    for entry in root.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            continue
        if resolved in targets:
            aliases.setdefault(resolved, []).append(entry)
    return aliases


def discover(cfg: Config) -> list[Candidate]:
    root = cfg.settings.root.resolve()
    globs = cfg.settings.exclude_globs
    dirs = _walk_dirs(root, cfg.settings.max_depth, globs)

    claimed: set[Path] = set()
    records: list[tuple[Path, bool, list[Path]]] = []

    # Pass 1: repos, deepest-first so an inner repo claims before its outer.
    for d in dirs:
        if not (d / ".git").exists():
            continue
        rel = str(d.relative_to(root))
        if rel in cfg.exclude_paths:
            continue
        inner = sorted(p for p in claimed if d in p.parents)
        records.append((d, True, inner))
        claimed.add(d)

    # Pass 2: non-repo directories with markdown nobody has claimed.
    for d in dirs:
        if (d / ".git").exists() or d in claimed:
            continue
        rel = str(d.relative_to(root))
        if rel in cfg.exclude_paths:
            continue
        if _has_unclaimed_markdown(d, claimed, root, globs):
            inner = sorted(p for p in claimed if d in p.parents)
            records.append((d, False, inner))
            claimed.add(d)

    alias_map = _symlink_aliases(root, {p for p, _, _ in records})
    return [
        Candidate(
            path=p,
            is_repo=is_repo,
            aliases=tuple(sorted(alias_map.get(p, []))),
            claimed_excludes=tuple(inner),
        )
        for p, is_repo, inner in sorted(records, key=lambda r: str(r[0]))
    ]
