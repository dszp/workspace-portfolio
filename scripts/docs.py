"""Inventory a project's documentation and count what is still open.

Checkbox counting deliberately ignores fenced code blocks, because plans and
specs routinely SHOW checkbox syntax in examples; counting those would inflate
every documentation-heavy project's open-item total.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from scripts.discovery import is_excluded

#: Curated near-term work, maintained by hand. Feeds `open_items`, which in turn
#: feeds `derived.status` (stalled vs dormant) and the attention score's pressure
#: term.
BACKLOG_PATTERNS = ("BACKLOG*.md", "TODO*.md", "HANDOFF*.md", "NEXT*.md")
#: Aspirational, not obligatory — a project should not look like it needs
#: attention because it has ambitions. Counted separately as `roadmap_items` and
#: never fed into status or attention.
ROADMAP_PATTERNS = ("ROADMAP*.md",)
_ROADMAP_KINDS = {p.split("*")[0].lower() for p in ROADMAP_PATTERNS}
_UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]\s", re.MULTILINE)
_CHECKED = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s", re.MULTILINE)
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def count_open_items(text: str) -> int:
    return len(_UNCHECKED.findall(_strip_fences(text)))


def count_checked(text: str) -> int:
    return len(_CHECKED.findall(_strip_fences(text)))


def _meta(path: Path, rel: str) -> dict:
    data = path.read_bytes()
    return {
        "path": rel,
        "sha": hashlib.sha256(data).hexdigest(),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .astimezone()
        .isoformat(),
    }


def _is_excluded(
    rel: str,
    excl_rel: list[str],
    generated: Iterable[str],
    exclude_globs: tuple[str, ...] = (),
) -> bool:
    if any(rel == e or rel.startswith(e + "/") for e in excl_rel):
        return True
    if is_excluded(rel, exclude_globs):
        return True
    return any(fnmatch.fnmatch(rel, g) for g in generated)


def collect_docs(
    path: Path,
    excludes: Iterable[Path],
    generated_globs: Iterable[str],
    self_repo: bool,
    exclude_globs: tuple[str, ...] = (),
) -> dict:
    excl_rel = []
    for e in excludes:
        try:
            excl_rel.append(str(Path(e).resolve().relative_to(path.resolve())))
        except ValueError:
            continue
    generated = tuple(generated_globs) if self_repo else ()

    def find_one(*names: str) -> dict | None:
        for name in names:
            p = path / name
            if p.is_file() and not _is_excluded(name, excl_rel, generated, exclude_globs):
                return _meta(p, name)
        return None

    backlog_files: list[dict] = []
    has_handoff = False
    for pattern in BACKLOG_PATTERNS + ROADMAP_PATTERNS:
        for p in sorted(path.rglob(pattern)):
            rel = str(p.relative_to(path))
            if _is_excluded(rel, excl_rel, generated, exclude_globs) or not p.is_file():
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            entry = _meta(p, rel)
            entry["open_items"] = count_open_items(text)
            entry["kind"] = pattern.split("*")[0].lower()
            backlog_files.append(entry)
            if entry["kind"] == "handoff":
                has_handoff = True

    def collect_planlike(subdir: str) -> list[dict]:
        out: list[dict] = []
        base = path / "docs" / "superpowers" / subdir
        if not base.is_dir():
            return out
        for p in sorted(base.glob("*.md")):
            rel = str(p.relative_to(path))
            if _is_excluded(rel, excl_rel, generated, exclude_globs):
                continue
            text = p.read_text(errors="replace")
            m = _DATE_PREFIX.match(p.name)
            h1 = _H1.search(text)
            entry = _meta(p, rel)
            entry.update(
                date=m.group(1) if m else None,
                title=h1.group(1) if h1 else p.stem,
                checked=count_checked(text),
                unchecked=count_open_items(text),
            )
            out.append(entry)
        return out

    plans = collect_planlike("plans")
    specs = collect_planlike("specs")

    # open_items feeds status and attention, so only the curated near-term lists
    # count toward it. ROADMAP*.md is tallied separately as roadmap_items and
    # never summed in here. Plan and spec unchecked counts stay on their own
    # records (half_checked_plan reads them) but no longer sum into either total.
    open_items = sum(
        b["open_items"] for b in backlog_files if b["kind"] not in _ROADMAP_KINDS
    )
    roadmap_items = sum(
        b["open_items"] for b in backlog_files if b["kind"] in _ROADMAP_KINDS
    )
    half_checked = any(p["checked"] > 0 and p["unchecked"] > 0 for p in plans)

    return {
        "claude_md": find_one("CLAUDE.md"),
        "readme": find_one("README.md", "README.markdown", "readme.md"),
        "changelog": find_one("CHANGELOG.md", "CHANGELOG.markdown"),
        "backlog_files": sorted(backlog_files, key=lambda b: b["path"]),
        "plans": plans,
        "specs": specs,
        "open_items": open_items,
        "roadmap_items": roadmap_items,
        "has_handoff": has_handoff,
        "half_checked_plan": half_checked,
    }
