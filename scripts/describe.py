"""`psum describe` -- the generation pass for per-project descriptions.

Deliberately cheap: no repository crawl. The prompt for one project is built
from its facts record plus at most the first paragraph of README.md, the
first paragraph of CLAUDE.md, and package.json's `description` field --
whichever of those exist -- read directly off disk, one project at a time.

One `claude -p` subprocess per project that needs one, with bounded
parallelism (network-bound, unlike the scan's git subprocesses -- see
`describe_parallelism` in config.py). A project whose `claude` call fails, or
who has no `claude` on PATH at all, is simply skipped for this run: nothing
here is allowed to be fatal, and the failure is counted and reported rather
than silently swallowed.

Redacted projects never reach a model -- see REDACTED_FIELDS in scan.py and
the Redaction section of the design spec. They get a fixed placeholder
instead, assigned with zero subprocess calls.

CAVEAT, measured and currently true: the `claude -p` subprocesses are NOT
sandboxed. `--allowed-tools ""` does not restrict them (it is an allowlist
addition, and an empty one adds nothing), so each subprocess can read any path
this user can read. A redacted project therefore never has its contents *sent*
-- no prompt is ever built for it -- but that guarantee rests on no prompt
naming it, not on the subprocess being unable to reach it. Treat "no repo
crawl" as a statement about what the prompt CONTAINS, not about what the
subprocess CAN DO.
"""
from __future__ import annotations

import argparse
import atexit
import fnmatch
import json
import re
import shutil
import subprocess
import tempfile
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from scripts.config import Config, load_config
from scripts.descriptions import (
    Description,
    REDACTED_PLACEHOLDER,
    load_descriptions,
    needs_regeneration,
    project_key,
    save_descriptions,
)
from scripts.fsutil import LockBusy, read_json, state_lock

Generator = Callable[[str], "str | None"]

_PARA_BREAK = re.compile(r"\n\s*\n")

#: One empty directory per process, used as every subprocess's cwd. Process-
#: scoped rather than per-generator: tying its lifetime to the closure meant it
#: vanished the moment the generator was garbage-collected, which is a real
#: footgun for any caller that does not hold the generator for the whole run.
_NEUTRAL: "tempfile.TemporaryDirectory | None" = None


#: A restriction that actually restricts. `--allowed-tools ""` does not (it is
#: an allowlist addition, and an empty one adds nothing) -- measured, twice.
#: `--disallowed-tools` with an explicit list does: the same probe that read a
#: file through the allowlist flag answers NO_TOOLS through this one.
_NO_TOOLS = (
    "--disallowed-tools",
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
)


def _neutral_cwd() -> str:
    global _NEUTRAL
    if _NEUTRAL is None:
        _NEUTRAL = tempfile.TemporaryDirectory(prefix="psum-describe-")
        atexit.register(_NEUTRAL.cleanup)
    return _NEUTRAL.name


@dataclass
class DescribeReport:
    generated: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    would_generate: list[str] = field(default_factory=list)
    skipped_manual: list[str] = field(default_factory=list)
    skipped_up_to_date: list[str] = field(default_factory=list)
    skipped_filtered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    newly_stale: list[str] = field(default_factory=list)
    unstaled: list[str] = field(default_factory=list)


def _first_paragraph(text: str) -> str:
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    para: list[str] = []
    for line in lines[i:]:
        if not line.strip():
            break
        para.append(line.strip())
    return " ".join(para).strip()


def _read_first_paragraph(p: Path) -> str:
    if not p.is_file():
        return ""
    try:
        return _first_paragraph(p.read_text(errors="replace"))
    except OSError:
        return ""


def _package_json_description(p: Path) -> str | None:
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    desc = data.get("description") if isinstance(data, dict) else None
    return desc.strip() if isinstance(desc, str) and desc.strip() else None


#: How many top-level names to show. Enough to characterise a project, few
#: enough that a directory of hundreds of files does not dominate the prompt.
ENTRY_LIMIT = 40


def _top_level_entries(path: Path) -> list[str]:
    """Names directly inside `path`, dirs suffixed with `/`. One `iterdir`.

    This is the cheapest real evidence available about a project that has no
    README, no CLAUDE.md and no package.json description -- and 13 of the 84
    have none of the three. Without it the model is asked to describe a name
    and a category and nothing else, which is an invitation to invent. With
    it, `dns-audit.py`, `domains.txt`, `METHODOLOGY.md` and a handful of
    `.typ` files say what the project is without anyone reading a byte of
    their contents.

    Dot-entries are skipped: they are tooling state, and the same reasoning
    that keeps them from becoming records (discovery.is_hidden) applies to
    describing one.
    """
    try:
        names = sorted(
            e.name + ("/" if e.is_dir() else "")
            for e in path.iterdir()
            if not e.name.startswith(".")
        )
    except OSError:
        return []
    return names[:ENTRY_LIMIT]


def gather_context(path: Path) -> dict:
    """Three cheap top-level reads plus one listing. Never a repository crawl."""
    return {
        "readme": _read_first_paragraph(path / "README.md"),
        "claude_md": _read_first_paragraph(path / "CLAUDE.md"),
        "pkg_description": _package_json_description(path / "package.json"),
        "entries": _top_level_entries(path),
    }


def build_prompt(record: dict, context: dict) -> str:
    name = record.get("name") or record.get("slug") or "?"
    category = record.get("category_display") or record.get("category") or ""
    parts = [
        "Write 1-3 sentences describing this software project: what it is and "
        "what it is for. Be concrete and specific. No filler, and do not "
        "restate the project's name.",
        "",
        "Match this register (for tone only -- do not copy them):",
        '- "n8n community node built to integrate with Acme PBX and '
        'published publicly for community use."',
        '- "An MCP server run on Cloudflare Workers that links a single '
        'Microsoft To Do account to an MCP interface for agent use."',
        "",
        f"Project name: {name}",
        f"Category: {category}",
    ]
    if context.get("pkg_description"):
        parts.append(f"package.json description: {context['pkg_description']}")
    if context.get("readme"):
        parts.append(f"README.md, first paragraph: {context['readme']}")
    if context.get("claude_md"):
        parts.append(f"CLAUDE.md, first paragraph: {context['claude_md']}")
    if context.get("entries"):
        parts.append("Top-level contents: " + ", ".join(context["entries"]))
    parts += [
        "",
        "Base the description only on the evidence above. Do not invent client "
        "names, product names, versions, or capabilities it does not support; "
        "if the evidence is thin, write something correspondingly general "
        "rather than something specific and possibly wrong.",
        "",
        "Respond with only the description text -- no preamble, no quotes.",
    ]
    return "\n".join(parts)


def claude_generator(model: str) -> Generator:
    """A generator backed by `claude -p`, pinned to `model` and given no tools.

    Three details here are load-bearing, and each one was a real defect first:

    - The prompt goes on **stdin**, not argv. `--allowed-tools` is variadic, so
      a trailing positional prompt is swallowed as a tool name and the call
      dies with "Input must be provided". Stdin also sidesteps ARG_MAX once a
      README paragraph is in the prompt.
    - `--model` is passed explicitly. Without it every call inherits the
      interactive default, so a pass over ~80 projects silently runs on the
      most expensive model available to write 1-3 sentences each.
    - `--allowed-tools ""` does NOT disable tools. Measured: a subprocess run
      with that flag read a file in its working directory, and read
      `/home/user/workspace/...` from an unrelated cwd, exactly like a run
      with no flag at all. The option is an allowlist ADDITION, not a
      restriction, and an empty addition restricts nothing. The flag is kept
      only because it is harmless; it buys nothing. Anything that depends on
      the subprocess being unable to read a path must enforce that some other
      way -- see the module docstring.
    - It runs in an EMPTY directory. Disabling tools does not stop `claude`
      from putting its working directory's own context — the repo's CLAUDE.md,
      its branch, its recent commit subjects — into the subprocess's system
      prompt. Run from this repo, that context is "86 projects", "portfolio
      index", "workspace scan", and it bleeds into any project whose own
      prompt is thin. It produced two visibly wrong descriptions in the first
      full pass: a book manuscript and the remote-vs-code dev-VM repo were
      both described as workspace-portfolio scanners, the latter even though
      its correct README and CLAUDE.md paragraphs were in the prompt.
    """

    def generate(prompt: str) -> str | None:
        if shutil.which("claude") is None:
            return None
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model, *_NO_TOOLS],
                input=prompt, capture_output=True, text=True, timeout=180,
                cwd=_neutral_cwd(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        return text or None

    return generate


def _matches_only(record: dict, key: str, pattern: str) -> bool:
    candidates = (key, record.get("slug") or "", record.get("name") or "")
    return any(fnmatch.fnmatch(c, pattern) for c in candidates)


def run(
    facts: dict,
    cfg: Config,
    descriptions: dict[str, Description],
    *,
    generate: Generator,
    context_fn: Callable[[Path], dict] = gather_context,
    only: str | None = None,
    force_all: bool = False,
    dry_run: bool = False,
) -> tuple[dict[str, Description], DescribeReport]:
    root = cfg.settings.root.resolve()
    projects = facts.get("projects", [])
    new_entries = dict(descriptions)
    report = DescribeReport()

    seen_keys: set[str] = set()
    to_generate: list[tuple[str, dict]] = []

    for record in projects:
        key = project_key(record, root)
        seen_keys.add(key)
        existing = descriptions.get(key)

        if existing is not None and existing.source == "manual":
            report.skipped_manual.append(key)
            continue

        if record.get("redacted"):
            desired = Description(
                text=REDACTED_PLACEHOLDER, source="redacted",
                hash=record.get("content_hash"),
            )
            if existing is None or (existing.text, existing.source) != (
                desired.text, desired.source,
            ):
                new_entries[key] = desired
                report.placeholders.append(key)
            else:
                report.skipped_up_to_date.append(key)
            continue

        if only and not _matches_only(record, key, only):
            report.skipped_filtered.append(key)
            continue

        force = force_all and existing is not None and existing.source == "ai"
        if force or needs_regeneration(existing, record.get("content_hash")):
            to_generate.append((key, record))
        else:
            report.skipped_up_to_date.append(key)

    if to_generate:
        if dry_run:
            report.would_generate = [key for key, _ in to_generate]
        else:
            def _one(item: tuple[str, dict]):
                key, record = item
                context = context_fn(Path(record["path"]))
                prompt = build_prompt(record, context)
                try:
                    text = generate(prompt)
                except Exception:  # noqa: BLE001 - one failure must not end the run
                    text = None
                return key, record, text

            workers = max(1, cfg.settings.describe_parallelism)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_one, to_generate))
            for key, record, text in results:
                if not text or not text.strip():
                    report.failed.append(key)
                    continue
                new_entries[key] = Description(
                    text=text.strip(), source="ai", hash=record.get("content_hash"),
                )
                report.generated.append(key)

    # Retain, never delete: a project absent from this scan (moved, archived,
    # or the scan simply hiccuped) keeps its entry. Mark it stale rather than
    # silently dropping a description the owner may have hand-written.
    for key, entry in list(new_entries.items()):
        if key in seen_keys:
            if entry.stale:
                new_entries[key] = replace(entry, stale=False)
                report.unstaled.append(key)
            continue
        if not entry.stale:
            new_entries[key] = replace(entry, stale=True)
            report.newly_stale.append(key)

    return new_entries, report


def main(argv: list[str], *, generate: Generator | None = None) -> int:
    ap = argparse.ArgumentParser(prog="psum describe")
    ap.add_argument("--all", action="store_true",
                     help="regenerate every AI-sourced entry, ignoring the hash gate")
    ap.add_argument("--only", help="only projects whose slug/name/path matches PATTERN")
    ap.add_argument("--dry-run", action="store_true",
                     help="print what would be generated; call nothing")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    cfg = load_config()
    facts = read_json(repo / "state" / "facts.json")
    if facts is None:
        print("no state/facts.json — run `psum scan` first", file=sys.stderr)
        return 1

    desc_path = repo / "descriptions.toml"
    gen = generate or claude_generator(cfg.settings.describe_model)

    if args.dry_run:
        descriptions = load_descriptions(desc_path)
        _, report = run(
            facts, cfg, descriptions, generate=gen,
            only=args.only, force_all=args.all, dry_run=True,
        )
        pending = sorted(set(report.would_generate) | set(report.placeholders))
        if not pending:
            print("0 projects would be generated")
            return 0
        print(f"{len(pending)} project(s) would be generated:")
        for key in pending:
            print(f"  {key}")
        return 0

    try:
        with state_lock(repo / "state"):
            descriptions = load_descriptions(desc_path)
            new_entries, report = run(
                facts, cfg, descriptions, generate=gen,
                only=args.only, force_all=args.all, dry_run=False,
            )
            changed = save_descriptions(desc_path, new_entries)
    except LockBusy as exc:
        print(f"psum describe: {exc}", file=sys.stderr)
        return 75

    bits = [f"{len(report.generated)} generated"]
    if report.placeholders:
        bits.append(f"{len(report.placeholders)} redacted placeholder(s)")
    if report.failed:
        bits.append(f"{len(report.failed)} failed")
    if report.newly_stale:
        bits.append(f"{len(report.newly_stale)} newly stale")
    suffix = " (descriptions.toml unchanged)" if not changed else ""
    print(", ".join(bits) + suffix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
