"""Shared test helpers. Both test_discovery and test_scan build a Config
pointed at a temporary tree, so the helper lives here rather than being
imported across test modules.
"""
import dataclasses
from pathlib import Path
from scripts.config import load_config


def cfg_for(root: Path, **settings_overrides):
    cfg = load_config(Path("config/projects.toml"))
    settings = dataclasses.replace(cfg.settings, root=Path(root), **settings_overrides)
    return dataclasses.replace(cfg, settings=settings)
