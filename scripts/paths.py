"""Where the tool's DATA lives, as opposed to where its CODE lives.

Everything mutable or personal — `config/projects.toml`, `state/`,
`descriptions.toml`, `INDEX.md` — belongs to the person running the tool, not
to the tool. Without this split they all land at the code repo's root, so
cloning this project and running `psum index` auto-commits your private
workspace map into your checkout of somebody else's source. Tool and data are
welded together, and there is nowhere to put your data that is not "inside the
program".

`PSUM_HOME` unwelds them. Set it and the code can live anywhere, read-only and
shared; unset, it resolves to the code repo root, which is exactly the old
behaviour, so a single-user checkout keeps working with no configuration.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Environment variable naming the data directory.
HOME_ENV = "PSUM_HOME"


def code_root() -> Path:
    """The directory holding `scripts/`, `psum`, and the packaged defaults."""
    return Path(__file__).resolve().parent.parent


def psum_home() -> Path:
    """The data directory: config, state, descriptions, and the rendered index.

    Falls back to `code_root()` so an untouched clone behaves exactly as it did
    before this existed.
    """
    raw = os.environ.get(HOME_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    return code_root()


def config_path() -> Path:
    """`config/projects.toml` from PSUM_HOME, falling back to the copy shipped
    with the code.

    The fallback is what makes a fresh clone runnable: point PSUM_HOME at an
    empty directory and you still get the documented defaults rather than a
    crash, and you only create your own config when you want to change one.
    """
    candidate = psum_home() / "config" / "projects.toml"
    if candidate.exists():
        return candidate
    return code_root() / "config" / "projects.toml"


def state_dir() -> Path:
    return psum_home() / "state"


def facts_path() -> Path:
    return state_dir() / "facts.json"


def descriptions_path() -> Path:
    return psum_home() / "descriptions.toml"


def index_path() -> Path:
    return psum_home() / "INDEX.md"
