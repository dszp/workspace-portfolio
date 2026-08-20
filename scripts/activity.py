"""Join a project to its Claude Code, Remember, and herdr activity.

Claude Code and the Remember plugin slug by the directory AS ENTERED, not the
resolved realpath, so a project reachable through a symlink accumulates two
distinct slugs with real content in each. Joining on the realpath alone
silently discards whichever side was used more; every candidate slug is
therefore merged, with the realpath slug kept as the canonical name.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_DAY_FILE = re.compile(r"^today-(\d{4}-\d{2}-\d{2})(?:\.done)?\.md$")
_SECTION = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def slug_for(path: Path) -> str:
    return str(path).replace("/", "-")


def _newest_mtime(d: Path) -> str | None:
    newest = None
    try:
        for p in d.iterdir():
            if p.is_file():
                newest = max(newest or 0.0, p.stat().st_mtime)
    except OSError:
        return None
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, timezone.utc).astimezone().isoformat()


def _remember_for(dirpath: Path) -> tuple[str | None, str | None]:
    """Return (newest day string, tail text) for one Remember slug directory."""
    if not dirpath.is_dir():
        return None, None
    days = sorted(
        m.group(1) for p in dirpath.iterdir() if (m := _DAY_FILE.match(p.name))
    )
    tail = None
    recent = dirpath / "recent.md"
    if recent.is_file():
        text = recent.read_text(errors="replace")
        matches = list(_SECTION.finditer(text))
        if matches:
            last = matches[-1]
            body = text[last.end():]
            cut = body.find("\n## ")
            tail = (last.group(0) + (body if cut == -1 else body[:cut])).strip()
    return (days[-1] if days else None), tail


def parse_herdr(snapshot: dict, session: str) -> dict:
    """Flatten one herdr api snapshot payload into a pane list."""
    panes: list[dict] = []
    ws_list = (
        snapshot.get("result", {}).get("snapshot", {}).get("workspaces", [])
        if isinstance(snapshot, dict)
        else []
    )
    for ws in ws_list:
        for tab in ws.get("tabs", []):
            for pane in tab.get("panes", []):
                panes.append(
                    {
                        "cwd": pane.get("foreground_cwd") or pane.get("cwd"),
                        "workspace_id": ws.get("workspace_id"),
                        "tab_id": tab.get("tab_id"),
                        "agent_status": pane.get("agent_status"),
                        "session": session,
                    }
                )
    return {"panes": panes}


def herdr_snapshot(runner=None) -> dict:
    """Query every herdr session's socket. Absent or dead herdr yields empties."""
    runner = runner or (
        lambda args: subprocess.run(
            args, capture_output=True, text=True, timeout=10, env=os.environ
        ).stdout
    )
    sessions: dict[str, dict] = {}
    panes: list[dict] = []
    try:
        listing = json.loads(runner(["herdr", "session", "list", "--json"]) or "{}")
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return {"sessions": {}, "panes": []}

    for s in listing.get("sessions", []):
        name, running = s.get("name"), bool(s.get("running"))
        sessions[name] = {"running": running, "socket": s.get("socket_path")}
        if not running:
            continue
        args = ["herdr", "api", "snapshot"]
        if name != "default":
            args = ["herdr", "--session", name, "api", "snapshot"]
        try:
            snap = json.loads(runner(args) or "{}")
        except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
            continue
        panes.extend(parse_herdr(snap, name)["panes"])
    return {"sessions": sessions, "panes": panes}


def collect_activity(
    path: Path,
    aliases,
    *,
    remember_root: Path,
    claude_root: Path,
    herdr: dict,
) -> dict:
    canonical = slug_for(path)
    slugs = [canonical] + [slug_for(a) for a in aliases]
    slugs = list(dict.fromkeys(slugs))  # de-dupe, preserve order

    session_count = 0
    last_session = None
    best_day: str | None = None
    best_tail: str | None = None

    for slug in slugs:
        cdir = claude_root / slug
        if cdir.is_dir():
            files = [p for p in cdir.iterdir() if p.is_file()]
            session_count += len(files)
            newest = _newest_mtime(cdir)
            if newest and (last_session is None or newest > last_session):
                last_session = newest
        day, tail = _remember_for(remember_root / slug)
        if day and (best_day is None or day > best_day):
            best_day, best_tail = day, tail
        elif best_tail is None and tail:
            best_tail = tail

    project = str(path)
    my_panes = [
        p for p in herdr.get("panes", [])
        if p.get("cwd") and (p["cwd"] == project or p["cwd"].startswith(project + "/"))
    ]
    session = my_panes[0]["session"] if my_panes else None
    statuses = [p.get("agent_status") for p in my_panes if p.get("agent_status")]
    for rank in ("working", "idle", "done"):
        if rank in statuses:
            agent_status = rank
            break
    else:
        agent_status = statuses[0] if statuses else None

    return {
        "slugs": slugs,
        "remember_slug": canonical,
        "remember_last_day": best_day,
        "remember_tail": best_tail,
        "remember_tail_sha": (
            hashlib.sha256(best_tail.encode()).hexdigest() if best_tail else None
        ),
        "claude_session_count": session_count,
        "claude_last_session_at": last_session,
        "herdr_session": session,
        "herdr_session_running": bool(
            session and herdr.get("sessions", {}).get(session, {}).get("running")
        ),
        "herdr_open_panes": len(my_panes),
        "herdr_agent_status": agent_status,
    }
