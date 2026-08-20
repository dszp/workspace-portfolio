"""Hash the fields that would change a written summary — and only those.

Included: head sha, the porcelain DIGEST (not counts, so editing an already-
dirty file registers), unpushed/branches_ahead/stashes (all feed status), every
doc and plan sha with its box counts, the Remember tail sha, and the derived
status (which can flip from elapsed time with no content change at all).

Excluded: every mtime, file counts, herdr state, session counts, and the
attention score. Those move constantly without the project's substance
changing, and each one would cost tokens on the next brief run.
"""
from __future__ import annotations

import hashlib
import json


def _doc(entry: dict | None) -> str | None:
    return entry.get("sha") if entry else None


def content_hash(record: dict) -> str:
    git = record.get("git") or {}
    docs = record.get("docs") or {}
    activity = record.get("activity") or {}
    derived = record.get("derived") or {}

    payload = {
        "head_sha": git.get("head_sha"),
        "porcelain_digest": git.get("porcelain_digest"),
        "unpushed": git.get("unpushed", 0),
        "branches_ahead": sorted(git.get("branches_ahead") or []),
        "stashes": git.get("stashes", 0),
        "claude_md": _doc(docs.get("claude_md")),
        "readme": _doc(docs.get("readme")),
        "changelog": _doc(docs.get("changelog")),
        "backlog": sorted(
            (b["path"], b["sha"], b.get("open_items", 0))
            for b in docs.get("backlog_files") or []
        ),
        "plans": sorted(
            (p["path"], p["sha"], p.get("checked", 0), p.get("unchecked", 0))
            for p in docs.get("plans") or []
        ),
        "specs": sorted(
            (s["path"], s["sha"], s.get("checked", 0), s.get("unchecked", 0))
            for s in docs.get("specs") or []
        ),
        "remember_tail_sha": activity.get("remember_tail_sha"),
        "status": derived.get("status"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
