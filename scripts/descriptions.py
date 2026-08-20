"""Storage for per-project descriptions: `descriptions.toml` at the repo root.

Curated content, not scan output, so it lives beside `config/` rather than
`state/` and is tracked in git -- the diff of this file is the record of a
project's description actually changing, the same way `state/briefs/*.md`
will be for the fuller judgment pass.

Two sources, one rule each:

- `source = "manual"` -- the owner's own words. Never regenerated, whatever
  the hash says. This is how he takes ownership of a description he tweaked.
- `source = "ai"` -- regenerated only when the project's `content_hash` has
  moved since this description was written. Same gate the briefs will use.
- `source = "redacted"` -- the fixed placeholder for client work. No model
  ever sees a redacted project's contents; see `describe.py`.

Entries are never deleted for a project that has (temporarily or otherwise)
stopped appearing in `facts.json` -- a scan hiccup, a move, or an archive must
not cost the owner a description he may have hand-written. `describe.py`
marks such an entry `stale = true` rather than dropping it.
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from scripts.fsutil import atomic_write

#: Fixed text for a redacted project. No project contents ever reach a model
#: to produce this -- see the Redaction section of the design spec.
REDACTED_PLACEHOLDER = "Client work — contents not summarized."


@dataclass(frozen=True)
class Description:
    text: str
    source: str  # "manual" | "ai" | "redacted"
    hash: str | None = None
    stale: bool = False


def load_descriptions(path: Path) -> dict[str, Description]:
    if not path.exists():
        return {}
    raw = tomllib.loads(path.read_text())
    out: dict[str, Description] = {}
    for key, body in raw.items():
        if not isinstance(body, dict):
            continue
        out[key] = Description(
            text=str(body.get("text", "")),
            source=str(body.get("source", "ai")),
            hash=body.get("hash"),
            stale=bool(body.get("stale", False)),
        )
    return out


def _render_entry(key: str, d: Description) -> str:
    # json.dumps produces a double-quoted string whose escaping rules are a
    # safe subset of TOML's basic-string rules for the plain prose this holds
    # -- and tomllib round-trips it, which is what actually matters here.
    lines = [f"[{json.dumps(key)}]", f"text = {json.dumps(d.text)}", f"source = {json.dumps(d.source)}"]
    if d.hash:
        lines.append(f"hash = {json.dumps(d.hash)}")
    if d.stale:
        lines.append("stale = true")
    return "\n".join(lines)


def save_descriptions(path: Path, entries: dict[str, Description]) -> bool:
    """Serialize deterministically (sorted by key) and write via atomic_write.

    Sorted key order is what keeps the git diff of this file readable --
    a new project's entry lands where it alphabetically belongs, not
    wherever dict insertion order happened to put it. Returns whatever
    atomic_write returns: False when the content is byte-identical to what's
    on disk, so an unchanged pass produces no git diff.
    """
    blocks = [_render_entry(key, entries[key]) for key in sorted(entries)]
    text = "\n\n".join(blocks) + ("\n" if blocks else "")
    return atomic_write(path, text)


def needs_regeneration(entry: Description | None, content_hash: str) -> bool:
    """Whether an AI-sourced description should be (re)generated.

    Missing -> True, so a project that has never had a description (whether
    this is the very first run or its 500th) is always picked up -- there is
    no separate "bootstrap" mode to keep in sync with this rule.

    `manual` is the owner's own text and is never touched, whatever the hash
    says. Anything else -- `ai` with a matching hash -- is up to date and
    left alone; `ai` with a drifted hash, or any other source (e.g. a
    leftover `redacted` placeholder on a project that is no longer redacted),
    is regenerated.
    """
    if entry is None:
        return True
    if entry.source == "manual":
        return False
    return not (entry.source == "ai" and entry.hash == content_hash)


def project_key(record: dict, root: Path) -> str:
    """The descriptions.toml key for one facts record: its path relative to
    `root` (~/workspace), POSIX-separated so the file reads the same on every
    host.

    A redacted record's `path` is null (scan.py strips it -- see Redaction in
    the design spec), so falling back to `path` would either crash or, worse,
    silently key on `None`. The record's `slug` is already a client-free
    digest for a redacted project (`redact_record` in scan.py), so it is the
    only safe key: using the real path here would put the client's folder
    name straight into a git-tracked file, defeating the whole point of
    redacting it everywhere else.
    """
    path = record.get("path")
    if path:
        try:
            return Path(path).resolve().relative_to(root).as_posix()
        except ValueError:
            pass
    return record.get("slug") or "?"
