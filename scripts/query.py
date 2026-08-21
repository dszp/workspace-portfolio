"""Machine-readable access to the facts document: `psum query`.

The renderers answer the questions they were designed around. This verb
answers the ones they were not, by handing a model the same facts in a shape
small enough to reason over: `state/facts.json` is ~300 KB and cannot go into
a context window whole.

Two shapes, mirroring the two the phone gets from the vault notes: a compact
projection of every project, then the verbatim record for the few that matter.
`compact()` is the single definition of that projection -- `render_data.py`
imports it rather than re-deriving it, because a phone and a terminal
disagreeing about what a project's numbers are is worse than either being
wrong alone.

Filter flags are deliberately few. The projection carries enough fields that
the caller filters the result itself, which answers questions a flag
vocabulary would never have anticipated.
"""
from __future__ import annotations

from datetime import datetime

from scripts.render_terminal import relative_age


def compact(p: dict, now: datetime) -> dict:
    """One project, projected to the fields both backends show."""
    d = p["derived"]
    g = p.get("git") or {}
    docs = p.get("docs") or {}
    plans = docs.get("plans") or []
    checked = sum(pl.get("checked") or 0 for pl in plans)
    total = sum((pl.get("checked") or 0) + (pl.get("unchecked") or 0) for pl in plans)
    return {
        "slug": p.get("slug"),
        # Null for a redacted record (scan.py). Callers fall back to `slug`;
        # this projection does NOT substitute, so a consumer can still tell
        # an unnamed project from a named one.
        "name": p.get("name"),
        "category": p.get("category_display") or p.get("category"),
        "status": d["status"],
        "attention": d["attention"],
        "last_worked": d.get("last_worked"),
        # Same formatter the table uses, so a conversational answer and the
        # printed table can never disagree about how old something is.
        "age": relative_age(d.get("last_worked"), now),
        "open": d.get("open_items") or 0,
        "roadmap": d.get("roadmap_items") or 0,
        # checked/total across every plan file, where total = checked +
        # unchecked. This is the direct answer to "what did I have left".
        "plans": f"{checked}/{total}",
        # Untracked files are uncommitted work too -- a project with only
        # untracked files is dirty, and summing dirty_files alone hides it.
        "dirty": (g.get("dirty_files") or 0) + (g.get("untracked_files") or 0),
        "unpushed": g.get("unpushed") or 0,
        "ahead": len(g.get("branches_ahead") or []),
        "stashes": g.get("stashes") or 0,
        "handoff": bool(docs.get("has_handoff")),
        "half_plan": bool(docs.get("half_checked_plan")),
        "redacted": bool(p.get("redacted")),
        "path": p.get("display_path"),
        "reasons": list(d.get("attention_reasons") or []),
    }


def select(
    projects: list[dict],
    *,
    status: str | None = None,
    category: str | None = None,
    sort: str = "attention",
    limit: int | None = None,
) -> list[dict]:
    """Filter and order raw facts records. Returns records, not projections.

    Dormant and archived are INCLUDED, unlike bare `psum`: hiding rows is a
    display decision, and this is not a display.
    """
    rows = list(projects)
    if category:
        rows = [p for p in rows
                if category in (p.get("category"), p.get("category_display"))]
    if status:
        rows = [p for p in rows if p["derived"]["status"] == status]

    # Successive stable sorts, least-significant key first. A single tuple key
    # cannot express "attention descending, then last_worked DESCENDING, then
    # slug ascending" -- you cannot negate a string -- and reverse=True on the
    # whole tuple would flip the slug tiebreak too. Python's sort is stable
    # even with reverse=True, so this composes exactly.
    rows.sort(key=lambda p: p.get("slug") or "")
    if sort == "name":
        # `name` is None for a redacted record; comparing None to str raises
        # TypeError the moment two rows tie. Fall back to slug, as every
        # other surface does.
        rows.sort(key=lambda p: (p.get("name") or p.get("slug") or "").lower())
    elif sort == "recent":
        rows.sort(key=lambda p: p["derived"].get("last_worked") or "", reverse=True)
    else:
        rows.sort(key=lambda p: p["derived"].get("last_worked") or "", reverse=True)
        rows.sort(key=lambda p: p["derived"]["attention"], reverse=True)

    return rows[:limit] if limit else rows


def envelope(facts: dict, rows: list[dict], now: datetime) -> dict:
    """Wrap results with the freshness the caller must not have to compute.

    `scan_age` lives here rather than being left to the caller because the
    skill's freshness rule depends on it, and a caller that has to subtract
    timestamps itself will eventually forget to.
    """
    return {
        "scanned_at": facts.get("scanned_at"),
        "scan_age": relative_age(facts.get("scanned_at"), now),
        "count": len(rows),
        "projects": rows,
    }
