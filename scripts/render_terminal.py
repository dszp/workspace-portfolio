"""The at-a-glance table. A pure function of the facts document."""
from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from scripts.config import Config, load_config
from scripts.paths import descriptions_path, facts_path
from scripts.descriptions import Description, load_descriptions, project_key
from scripts.fsutil import read_json

RESET = "\x1b[0m"
DIM = "\x1b[2m"
#: Continuation line budget: the 80-column table budget minus the indent, so
#: a description never widens the terminal output past what the table itself
#: is already capped at.
DESC_INDENT = "    "
#: A single long name or category among ~86 real projects otherwise widens the
#: whole table for every row. Clip at the outlier's expense, not the reader's.
MAX_NAME = 28
MAX_CATEGORY = 20
STATUS_COLOR = {
    "mid-flight": "\x1b[33m",   # yellow — you left something running
    "stalled": "\x1b[31m",      # red
    "active": "\x1b[32m",       # green
    "done": "\x1b[36m",         # cyan
    "dormant": "\x1b[90m",      # grey
    "archived": "\x1b[90m",
}


def relative_age(iso: str | None, now: datetime) -> str:
    if not iso:
        return "-"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "-"
    seconds = max(0, (now - then).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    days = seconds / 86400
    if days < 30:
        return f"{int(days)}d"
    if days < 365:
        return f"{int(days // 30)}mo"
    return f"{days / 365:.1f}y"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _desc_lines(text: str, color: bool) -> list[str]:
    wrapped = textwrap.wrap(text, width=80 - len(DESC_INDENT)) or [text]
    lines = [f"{DESC_INDENT}{w}" for w in wrapped]
    if not color:
        return lines
    return [f"{DIM}{line}{RESET}" for line in lines]


def render(
    facts: dict,
    cfg: Config,
    *,
    sort: str = "attention",
    category: str | None = None,
    status: str | None = None,
    show_all: bool = False,
    color: bool = True,
    now: datetime | None = None,
    descriptions: dict[str, Description] | None = None,
    show_desc: bool = False,
) -> str:
    now = now or datetime.fromisoformat(facts["scanned_at"])
    rows = list(facts.get("projects", []))
    if category:
        rows = [p for p in rows if category in (p["category"], p.get("category_display"))]
    if status:
        rows = [p for p in rows if p["derived"]["status"] == status]
    elif not show_all:
        rows = [p for p in rows if p["derived"]["status"] not in cfg.settings.hide_status]

    if sort == "recent":
        rows.sort(key=lambda p: p["derived"]["last_worked"] or "", reverse=True)
    else:
        # p["name"] is null for a redacted record — sort on the same
        # slug-falls-back display name used below, not the raw field, or a
        # redacted row racing a real name on an attention tie raises TypeError.
        rows.sort(
            key=lambda p: (-p["derived"]["attention"], p.get("name") or p.get("slug") or "?")
        )

    if not rows:
        return "no projects match those filters\n"

    header = ("PROJECT", "CATEGORY", "STATUS", "LAST", "OPEN", "ATTN")
    body = []
    for p in rows:
        # A redacted record's name is null (see redact_record in scan.py) —
        # fall back to the digest slug rather than print "None".
        name = p.get("name") or p.get("slug") or "?"
        category = p.get("category_display") or p["category"]
        if category == name:
            # A repo living directly under ~/workspace is its own category —
            # printing it twice is noise, and it pays for itself in width.
            category = ""
        markers = ("*" if p.get("redacted") else "") + ("!" if p.get("error") else "")
        body.append((
            _clip(name, MAX_NAME - len(markers)) + markers,
            _clip(category, MAX_CATEGORY),
            p["derived"]["status"],
            relative_age(p["derived"]["last_worked"], now),
            str(p["derived"]["open_items"] or ""),
            str(p["derived"]["attention"]),
        ))
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(6)]

    def line(cells, tint: str | None = None) -> str:
        text = "  ".join(
            c.ljust(widths[i]) if i < 3 else c.rjust(widths[i])
            for i, c in enumerate(cells)
        ).rstrip()
        return f"{tint}{text}{RESET}" if (color and tint) else text

    out = [line(header), line(tuple("-" * w for w in widths))]
    root = cfg.settings.root.resolve()
    for idx, (cells, p) in enumerate(zip(body, rows)):
        out.append(line(cells, STATUS_COLOR.get(p["derived"]["status"])))
        if descriptions is None:
            continue
        if not (show_desc or idx < cfg.settings.desc_rows):
            continue
        entry = descriptions.get(project_key(p, root))
        if entry and entry.text:
            out.extend(_desc_lines(entry.text, color))
    shown, total = len(rows), len(facts.get("projects", []))
    out.append("")
    out.append(f"{shown} of {total} projects   scanned {relative_age(facts['scanned_at'], now)} ago")
    if facts.get("errors"):
        out.append(f"{len(facts['errors'])} project(s) had scan errors — see state/facts.json")
    if any(p.get("error") for p in rows):
        out.append("! scan error for this project — see state/facts.json")
    if any(p.get("redacted") for p in rows):
        out.append("* redacted (client work: counts only, no content summarized)")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="psum")
    ap.add_argument("--recent", action="store_true", help="sort by last worked")
    ap.add_argument("--category")
    ap.add_argument("--status")
    ap.add_argument("--all", action="store_true", help="include dormant and archived")
    ap.add_argument("--desc", action="store_true", help="show descriptions for every visible row")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)

    facts = read_json(facts_path())
    if facts is None:
        print("no state/facts.json — run `psum scan` first", file=sys.stderr)
        return 1

    sys.stdout.write(
        render(
            facts, load_config(),
            sort="recent" if args.recent else "attention",
            category=args.category, status=args.status, show_all=args.all,
            color=sys.stdout.isatty() and not args.no_color,
            now=datetime.now().astimezone(),
            descriptions=load_descriptions(descriptions_path()),
            show_desc=args.desc,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
