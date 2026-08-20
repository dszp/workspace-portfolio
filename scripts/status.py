"""Derive a project's status and attention score. Pure — no I/O, no clock.

`done` is evaluated BEFORE the recency rules and carries no time window. Placed
after them it would be unreachable for anything finished this week and would
decay back to dormant once a finished project went quiet, so it would describe
a window rather than a state.
"""
from __future__ import annotations

from datetime import datetime

from scripts.config import Config, ProjectOverride


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _last_worked(record: dict) -> tuple[str | None, str | None]:
    candidates: list[tuple[datetime, str, str]] = []
    git = record.get("git") or {}
    for value, source in (
        (git.get("last_commit_at"), "git"),
        ((record.get("fs") or {}).get("newest_mtime"), "filesystem"),
        ((record.get("activity") or {}).get("claude_last_session_at"), "claude-session"),
    ):
        dt = _parse(value)
        if dt:
            candidates.append((dt, value, source))
    day = (record.get("activity") or {}).get("remember_last_day")
    if day:
        dt = _parse(f"{day}T23:59:59")
        if dt:
            candidates.append((dt.astimezone(), dt.astimezone().isoformat(), "remember"))
    if not candidates:
        return None, None
    _, value, source = max(candidates, key=lambda c: c[0])
    return value, source


def _mid_flight_terms(record: dict, w) -> list[tuple[int, str]]:
    git = record.get("git") or {}
    docs = record.get("docs") or {}
    terms: list[tuple[int, str]] = []
    dirty = git.get("dirty_files", 0) + git.get("untracked_files", 0)
    if dirty:
        terms.append((w.dirty, f"{dirty} uncommitted file(s)"))
    if git.get("unpushed", 0):
        terms.append((w.unpushed, f"{git['unpushed']} unpushed commit(s)"))
    if docs.get("has_handoff"):
        terms.append((w.handoff, "HANDOFF.md present"))
    if docs.get("half_checked_plan"):
        terms.append((w.half_plan, "a plan is partially executed"))
    if git.get("branches_ahead"):
        terms.append((w.branch_ahead, f"branch ahead: {', '.join(git['branches_ahead'])}"))
    return terms


def _is_done(record: dict) -> bool:
    git = record.get("git") or {}
    docs = record.get("docs") or {}
    plans = docs.get("plans") or []
    if docs.get("open_items", 0) or docs.get("has_handoff"):
        return False
    if git.get("dirty_files") or git.get("untracked_files"):
        return False
    if git.get("unpushed") or git.get("branches_ahead"):
        return False
    return bool(plans) and all(p.get("unchecked", 0) == 0 for p in plans)


def derive(
    record: dict, cfg: Config, override: ProjectOverride | None, now: datetime
) -> dict:
    w = cfg.weights
    last_worked, source = _last_worked(record)
    docs = record.get("docs") or {}
    open_items = docs.get("open_items", 0)
    # Aspirational, not obligatory: read here so both renderers pull from
    # `derived` consistently, but never fed into a status rule or the attention
    # formula below — a roadmap full of SOMEDAY entries must not make a project
    # look like it needs attention.
    roadmap_items = docs.get("roadmap_items", 0)

    quiet_days = 10_000.0
    dt = _parse(last_worked)
    if dt:
        quiet_days = max(0.0, (now - dt).total_seconds() / 86400.0)

    mid_terms = _mid_flight_terms(record, w)

    if override and override.status:
        status, status_source = override.status, "config"
    elif _is_done(record):
        status, status_source = "done", "derived"
    elif mid_terms:
        status, status_source = "mid-flight", "derived"
    elif quiet_days <= cfg.settings.active_days:
        status, status_source = "active", "derived"
    elif open_items > 0:
        status, status_source = "stalled", "derived"
    else:
        status, status_source = "dormant", "derived"

    git = record.get("git") or {}
    # Anchored at the repo's own last commit, not at "now": commits_90d is a
    # window ending at the moment of THIS scan, so it is exactly 0 for anything
    # quiet longer than 90 days — which silences the stall term precisely for
    # the projects "what did I forget" cares about most. commits_90d_anchored
    # answers "how hot was this before it went quiet" instead, and stays
    # meaningful no matter how long ago that was.
    commits = git.get("commits_90d_anchored", 0)
    if record.get("is_repo") and commits > 0:
        intensity = min(1.0, commits / w.intensity_commits)
    else:
        touched = (record.get("fs") or {}).get("files_touched_90d", 0)
        intensity = min(1.0, touched / w.intensity_files)

    mid_flight = min(w.mid_flight_cap, sum(points for points, _ in mid_terms))
    ramp = min(1.0, max(0.0, (quiet_days - w.stall_grace_days) / w.stall_ramp_days))
    stall = w.stall_max * intensity * ramp
    pressure = min(w.pressure_cap, open_items)

    reasons = [label for _, label in mid_terms]
    if stall >= 1:
        reasons.append(f"quiet {int(quiet_days)}d after sustained activity")
    if pressure:
        reasons.append(f"{open_items} open item(s)")

    return {
        "last_worked": last_worked,
        "last_worked_source": source,
        "open_items": open_items,
        "roadmap_items": roadmap_items,
        "status": status,
        "status_source": status_source,
        "attention": min(100, round(mid_flight + stall + pressure)),
        "attention_reasons": reasons,
    }
