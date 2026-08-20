"""Assemble facts.json from every collector, then redact before anyone reads it.

Redaction happens HERE, on the record, not later at the brief step. A raw
record carries commit subjects, file paths and plan titles; the synthesis pass
reads every record, and its output is committed and synced to a phone. Stripping
only at the brief step would leak all of it.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from scripts import activity as activity_mod
from scripts.config import Config, is_redacted, load_config, override_for
from scripts.discovery import discover, is_excluded
from scripts.paths import facts_path, state_dir
from scripts.docs import collect_docs
from scripts.fsutil import LockBusy, atomic_write, state_lock
from scripts.gitinfo import collect_git
from scripts.hashing import content_hash
from scripts.status import derive

SCHEMA_VERSION = 1

#: Free-text keys stripped from a redacted record. Anything added to a record
#: that could carry client-identifying prose MUST be listed here.
#:
#: `branches_ahead` and `herdr_session` are here because branch names and herdr
#: session names are developer-chosen free text, exactly like a commit subject
#: — a branch called `fix/acme-billing-export` or a herdr session named after
#: the client project leaks the same way `last_commit_subject` would.
#: `default_branch` is here for the same reason: it is the developer's own
#: choice of default branch name, the same free-text class as `branches_ahead`
#: — not a fixed enum like `status`.
#:
#: `git.error`'s value is always one of collect_git's own hardcoded diagnostic
#: strings (e.g. "HEAD is unreadable but commits exist; repository looks
#: corrupt"), never git's own stderr or a path — but nothing downstream reads
#: it for a redacted project (facts["errors"][]'s `message` for a redacted
#: project is already nulled independently, in build_one), so there is no
#: reason to make this key an exception to "redact unless a consumer needs
#: it", and "error" is also the same JSON key as the top-level `error` field
#: that DOES carry free text — keeping both nulled avoids relying on a reader
#: to know those are two unrelated fields that happen to share a name.
REDACTED_FIELDS = {
    "git": ("last_commit_subject", "remote", "remote_slug", "branch",
            "head_sha", "worktrees", "branches_ahead", "default_branch", "error"),
    "fs": ("newest_path",),
    "activity": ("remember_tail", "remember_slug", "slugs", "herdr_session"),
}

#: Top-level keys stripped entirely from a redacted record. `path`,
#: `display_path` and `aliases` embed the full filesystem path, and therefore
#: every intermediate directory name — which for client work IS the client
#: name. `error` is here because an exception message routinely embeds the
#: absolute path that caused it (e.g. `PermissionError` from `docs.py`'s file
#: reads), and that channel is not covered by any other rule. `slug` is not
#: merely dropped: it is replaced by a deterministic digest so brief filenames
#: stay stable across runs without carrying the path. `name` is dropped too —
#: see redact_record — because a discovery-promoted doc-only folder can be
#: named after the client itself.
REDACTED_TOP_LEVEL = ("path", "display_path", "aliases", "error")


def _fs_stats(
    path: Path, excludes, generated: tuple[str, ...], globs: tuple[str, ...]
) -> dict:
    """Filesystem stats for one project, pruning excluded trees during the walk.

    Pruning rather than filtering is the point: rglob() yields every path before
    anything can skip it, and one repository in this workspace holds 62,561 files
    under node_modules. The first real scan spent most of its 133 seconds inside
    trees every other module already ignores. Excluding them is also more correct
    than fast — an `npm install` must not make a project look recently worked on.

    files_touched_90d counts files touched in the 90 days *before this
    project's own newest file*, not before "now". Anchoring at "now" makes the
    count zero by construction for any project quiet longer than 90 days,
    which silences status.py's non-repo intensity term (and therefore stall)
    exactly for the doc folders and old repos that have been quiet longest —
    the ones the fallback exists to serve. Mirrors gitinfo.py's
    _count_window_ending, which anchors commits_90d_anchored at the repo's own
    last commit for the same reason.
    """
    ex_rel: set[str] = set()
    for e in excludes:
        try:
            ex_rel.add(str(Path(e).resolve().relative_to(path.resolve())))
        except ValueError:
            continue

    def skip(rel: str) -> bool:
        if rel in ex_rel or any(rel.startswith(x + "/") for x in ex_rel):
            return True
        if is_excluded(rel, globs):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in generated)

    newest_m, newest_p, count = 0.0, None, 0
    mtimes: list[float] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, path)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if d != ".git" and not skip(prefix + d)]
        for name in filenames:
            rel = prefix + name
            if skip(rel):
                continue
            try:
                m = (Path(dirpath) / name).stat().st_mtime
            except OSError:
                continue
            count += 1
            mtimes.append(m)
            if m > newest_m:
                newest_m, newest_p = m, rel
    # Portfolio-wide this is ~10k files post-pruning, so holding every mtime
    # for one project's walk in memory is trivial.
    cutoff = newest_m - 90 * 86400
    touched = sum(1 for m in mtimes if m >= cutoff)
    return {
        "newest_mtime": (
            datetime.fromtimestamp(newest_m, timezone.utc).astimezone().isoformat()
            if newest_m
            else None
        ),
        "newest_path": newest_p,
        "file_count": count,
        "files_touched_90d": touched,
    }


def redact_record(record: dict) -> dict:
    r = json.loads(json.dumps(record))  # deep copy; records are plain JSON
    r["slug"] = "redacted-" + hashlib.sha256(record["path"].encode()).hexdigest()[:16]
    # The folder name is NOT safe by itself: a directory promoted to a record
    # purely because it holds unclaimed markdown (the doc-only discovery path)
    # can be named after the client — `clients/agmaas/` with one loose .md in
    # it makes `name` literally "agmaas". Every surface that displays `name`
    # falls back to the digest slug above when this is None.
    r["name"] = None
    for key in REDACTED_TOP_LEVEL:
        r[key] = None
    for group, keys in REDACTED_FIELDS.items():
        if isinstance(r.get(group), dict):
            for k in keys:
                if k in r[group]:
                    r[group][k] = None
    docs = r.get("docs") or {}
    for key in ("claude_md", "readme", "changelog"):
        if docs.get(key):
            docs[key] = {"path": None, "sha": None, "mtime": docs[key].get("mtime")}
    for b in docs.get("backlog_files") or []:
        b["path"], b["sha"] = None, None
    for coll in ("plans", "specs"):
        for entry in docs.get(coll) or []:
            entry["path"], entry["title"], entry["sha"] = None, None, None
    d = r.get("derived") or {}
    d["attention_reasons"] = [
        reason for reason in d.get("attention_reasons", [])
        if "open item" in reason or "uncommitted" in reason or "unpushed" in reason
    ]
    r["redacted"] = True
    return r


#: Minimal valid sub-records used when a collector stage raises. derive() reads
#: fields off these unconditionally (record.get("fs") or {}, etc.), so a stage
#: that fails must still leave something shaped correctly behind it — an absent
#: key is not the same contract as "collected, and there was nothing to report."
_EMPTY_FS = {"newest_mtime": None, "newest_path": None, "file_count": 0,
             "files_touched_90d": 0}
_EMPTY_ACTIVITY = {
    "remember_tail": None, "remember_tail_sha": None, "slugs": [],
    "remember_slug": None, "remember_last_day": None, "claude_session_count": 0,
    "claude_last_session_at": None, "herdr_session": None,
    "herdr_session_running": False, "herdr_open_panes": 0, "herdr_agent_status": None,
}


def build_facts(
    cfg: Config,
    now: datetime,
    *,
    remember_root: Path,
    claude_root: Path,
    herdr: dict,
    self_path: Path | None,
) -> dict:
    root = cfg.settings.root.resolve()
    started = datetime.now()

    def build_one(cand) -> tuple[dict, list[dict]]:
        rel = str(cand.path.relative_to(root))
        category = rel.split("/")[0] if "/" in rel else cand.path.name
        is_self = self_path is not None and cand.path == Path(self_path).resolve()
        generated = cfg.settings.generated_globs if is_self else ()
        errs: list[dict] = []

        record = {
            "slug": activity_mod.slug_for(cand.path),
            "name": cand.path.name,
            "category": category,
            "category_display": cfg.categories.get(category, category),
            "path": str(cand.path),
            "display_path": str(cand.path).replace(str(Path.home()), "~", 1),
            "aliases": [str(a) for a in cand.aliases],
            "is_repo": cand.is_repo,
            "redacted": is_redacted(cfg, rel),
            # A list, not a scalar: a project that fails at two stages (e.g.
            # docs AND fs) must keep both messages. A bare assignment here
            # silently drops the first failure the moment a second one occurs.
            "error": [],
        }

        try:
            record["git"] = collect_git(
                cand.path, cand.claimed_excludes, now, cfg.settings.exclude_globs
            )
            if record["git"] and record["git"].get("error"):
                errs.append(
                    {"path": rel, "stage": "git", "message": record["git"]["error"]}
                )
        except Exception as exc:  # noqa: BLE001 - one bad repo must not end the scan
            record["git"] = None
            record["error"].append(f"git: {exc}")
            errs.append({"path": rel, "stage": "git", "message": str(exc)})

        try:
            record["docs"] = collect_docs(
                cand.path, cand.claimed_excludes, generated, is_self,
                cfg.settings.exclude_globs,
            )
        except Exception as exc:  # noqa: BLE001
            record["docs"] = {"open_items": 0, "roadmap_items": 0, "backlog_files": [],
                              "plans": [], "specs": [], "has_handoff": False,
                              "half_checked_plan": False, "claude_md": None,
                              "readme": None, "changelog": None}
            record["error"].append(f"docs: {exc}")
            errs.append({"path": rel, "stage": "docs", "message": str(exc)})

        try:
            record["fs"] = _fs_stats(
                cand.path, cand.claimed_excludes, generated, cfg.settings.exclude_globs
            )
        except Exception as exc:  # noqa: BLE001
            record["fs"] = dict(_EMPTY_FS)
            record["error"].append(f"fs: {exc}")
            errs.append({"path": rel, "stage": "fs", "message": str(exc)})

        try:
            record["activity"] = activity_mod.collect_activity(
                cand.path, cand.aliases,
                remember_root=remember_root, claude_root=claude_root, herdr=herdr,
            )
        except Exception as exc:  # noqa: BLE001
            record["activity"] = dict(_EMPTY_ACTIVITY)
            record["error"].append(f"activity: {exc}")
            errs.append({"path": rel, "stage": "activity", "message": str(exc)})

        record["derived"] = derive(record, cfg, override_for(cfg, rel), now)
        record["content_hash"] = content_hash(record)

        final = redact_record(record) if record["redacted"] else record
        if record["redacted"]:
            # facts["errors"][] is a second channel into the committed
            # INDEX.md, and it is NOT covered by redact_record (that function
            # only ever sees this one project's record, never the top-level
            # errors list). Redact it here, in the same closure, so there is
            # still exactly one place a redacted project's data escapes from —
            # not a second funnel to keep in sync with the first. `stage` is
            # structural (git/docs/fs/activity) and safe; `path` and `message`
            # are free text and are replaced/dropped the same way the record's
            # own `path` and `error` fields are.
            errs = [
                {"path": final["slug"], "stage": e["stage"], "message": None}
                for e in errs
            ]
        return final, errs

    # Every candidate's work is independent (no shared mutable state; herdr is
    # read-only and snapshotted once by the caller), and each stage's real cost
    # is spawning git subprocesses — I/O-bound, so a thread pool parallelizes it
    # without fighting the GIL. pool.map preserves input order so the result is
    # deterministic before the final sort below ever runs.
    with ThreadPoolExecutor(max_workers=max(1, cfg.settings.parallelism)) as pool:
        results = list(pool.map(build_one, discover(cfg)))

    projects = [record for record, _ in results]
    errors = [err for _, errs in results for err in errs]
    # Concurrent completion order is not append order; without this sort, two
    # runs over identical state could serialize errors[] differently and cause
    # a spurious facts.json diff — and a phantom re-brief downstream.
    errors.sort(key=lambda e: (e["path"], e["stage"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": now.isoformat(),
        "root": str(root),
        "duration_ms": int((datetime.now() - started).total_seconds() * 1000),
        "projects": sorted(
            projects, key=lambda p: (p["category"], p["name"] or p["slug"])
        ),
        "errors": errors,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="psum scan")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out = args.out or facts_path()

    try:
        with state_lock(state_dir()):
            facts = build_facts(
                cfg,
                datetime.now().astimezone(),
                remember_root=Path.home() / ".remember",
                claude_root=Path.home() / ".claude" / "projects",
                herdr=activity_mod.herdr_snapshot(),
                self_path=repo,
            )
            atomic_write(out, json.dumps(facts, indent=2, sort_keys=False) + "\n")
    except LockBusy as exc:
        print(f"psum scan: {exc}", file=sys.stderr)
        return 75  # EX_TEMPFAIL

    summary = f"scanned {len(facts['projects'])} projects in {facts['duration_ms']}ms"
    if facts["errors"]:
        summary += f", {len(facts['errors'])} error(s)"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
