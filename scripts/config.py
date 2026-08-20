"""Load and validate config/projects.toml into frozen dataclasses.

Everything downstream reads config through these objects, never the raw dict,
so a typo in the TOML fails here with a clear message instead of silently
producing a wrong number three modules later.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from scripts.paths import config_path

VALID_STATUSES = frozenset(
    {"archived", "done", "mid-flight", "active", "stalled", "dormant"}
)

_DEFAULT_SETTINGS = {
    "root": "~/workspace",
    "max_depth": 4,
    "active_days": 21,
    "parallelism": 1,  # measured default; see config/projects.toml for the rationale
    # describe is network-bound (one `claude -p` subprocess per project talking
    # to the API), unlike the git subprocess work above — no fork/exec
    # contention argument against concurrency here, so the default is higher.
    "describe_parallelism": 6,
    # A description is 1-3 sentences from a prompt that already carries
    # everything needed. Left unset, `claude -p` inherits the interactive
    # default model — which is how 80-odd trivial calls quietly become an
    # Opus bill. Named here so the choice is visible and tunable.
    "describe_model": "sonnet",
    "exclude_globs": ["**/node_modules/**", "**/.venv/**", "**/vendor/**"],
    "generated_globs": ["state/**", "INDEX.md", "html/**"],
    "hide_status": ["dormant", "archived"],
    "redact_prefixes": ["clients/"],
    # How many of the top (currently sorted) terminal rows carry their
    # description by default; `--desc` shows it for every visible row instead.
    "desc_rows": 10,
}

_DEFAULT_WEIGHTS = {
    "dirty": 25,
    "unpushed": 15,
    "handoff": 20,
    "half_plan": 10,
    "branch_ahead": 10,
    "mid_flight_cap": 60,
    "stall_max": 40,
    "intensity_commits": 60,
    "intensity_files": 20,
    "pressure_cap": 20,
    "stall_grace_days": 7,
    "stall_ramp_days": 60,
}


@dataclass(frozen=True)
class Settings:
    root: Path
    max_depth: int
    active_days: int
    parallelism: int
    describe_parallelism: int
    describe_model: str
    exclude_globs: tuple[str, ...]
    generated_globs: tuple[str, ...]
    hide_status: tuple[str, ...]
    redact_prefixes: tuple[str, ...]
    desc_rows: int


@dataclass(frozen=True)
class Weights:
    dirty: int
    unpushed: int
    handoff: int
    half_plan: int
    branch_ahead: int
    mid_flight_cap: int
    stall_max: int
    intensity_commits: int
    intensity_files: int
    pressure_cap: int
    stall_grace_days: int
    stall_ramp_days: int


@dataclass(frozen=True)
class ProjectOverride:
    status: str | None = None
    note: str | None = None
    redact: bool = False


@dataclass(frozen=True)
class Config:
    settings: Settings
    weights: Weights
    categories: dict[str, str]
    projects: dict[str, ProjectOverride]
    exclude_paths: tuple[str, ...]


def _default_path() -> Path:
    return config_path()


def load_config(path: Path | None = None) -> Config:
    path = path or _default_path()
    raw = tomllib.loads(path.read_text()) if path.exists() else {}

    s = {**_DEFAULT_SETTINGS, **raw.get("settings", {})}
    w = {**_DEFAULT_WEIGHTS, **raw.get("weights", {})}

    projects: dict[str, ProjectOverride] = {}
    for rel, body in raw.get("projects", {}).items():
        status = body.get("status")
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(
                f"unknown status {status!r} for project {rel!r}; "
                f"valid: {sorted(VALID_STATUSES)}"
            )
        projects[rel.strip("/")] = ProjectOverride(
            status=status, note=body.get("note"), redact=bool(body.get("redact", False))
        )

    return Config(
        settings=Settings(
            root=Path(s["root"]).expanduser(),
            max_depth=int(s["max_depth"]),
            active_days=int(s["active_days"]),
            parallelism=int(s["parallelism"]),
            describe_parallelism=int(s["describe_parallelism"]),
            describe_model=str(s["describe_model"]),
            exclude_globs=tuple(s["exclude_globs"]),
            generated_globs=tuple(s["generated_globs"]),
            hide_status=tuple(s["hide_status"]),
            redact_prefixes=tuple(s["redact_prefixes"]),
            desc_rows=int(s["desc_rows"]),
        ),
        weights=Weights(**{k: int(v) for k, v in w.items()}),
        categories=dict(raw.get("categories", {})),
        projects=projects,
        exclude_paths=tuple(e["path"].strip("/") for e in raw.get("exclude", [])),
    )


def _under(rel_path: str, prefix: str) -> bool:
    """True when rel_path IS prefix or lies beneath it.

    Compares on path separators so that "clientside-tools" is not treated as
    living under "clients/".
    """
    rel_path = rel_path.strip("/")
    prefix = prefix.strip("/")
    return rel_path == prefix or rel_path.startswith(prefix + "/")


def is_redacted(cfg: Config, rel_path: str) -> bool:
    for prefix in cfg.settings.redact_prefixes:
        if _under(rel_path, prefix):
            return True
    for rel, ov in cfg.projects.items():
        if ov.redact and _under(rel_path, rel):
            return True
    return False


def override_for(cfg: Config, rel_path: str) -> ProjectOverride | None:
    return cfg.projects.get(rel_path.strip("/"))
