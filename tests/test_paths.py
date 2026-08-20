"""PSUM_HOME separates the tool's data from the tool's code.

Both sides of that conditional are asserted here. A rule that always used the
env var would break every existing single-checkout install; a rule that never
did would leave the two welded together, which is the defect this exists to fix.
"""
from pathlib import Path

from scripts import paths


def test_psum_home_defaults_to_the_code_root_when_unset(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    assert paths.psum_home() == paths.code_root()


def test_psum_home_follows_the_environment_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    assert paths.psum_home() == tmp_path.resolve()
    assert paths.psum_home() != paths.code_root()


def test_an_empty_or_whitespace_value_is_treated_as_unset(monkeypatch):
    """An exported-but-empty variable is the normal result of `export PSUM_HOME=`
    in a shell profile. Honouring it literally would resolve the data directory
    to the process's cwd, quietly scattering state wherever psum was invoked."""
    for value in ("", "   "):
        monkeypatch.setenv(paths.HOME_ENV, value)
        assert paths.psum_home() == paths.code_root()


def test_every_data_path_lives_under_psum_home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    home = tmp_path.resolve()
    assert paths.state_dir() == home / "state"
    assert paths.facts_path() == home / "state" / "facts.json"
    assert paths.descriptions_path() == home / "descriptions.toml"
    assert paths.index_path() == home / "INDEX.md"


def test_config_falls_back_to_the_shipped_default_when_home_has_none(tmp_path, monkeypatch):
    """A brand-new PSUM_HOME is an empty directory. Without this fallback the
    first run of a fresh install crashes on a missing config instead of using
    the documented defaults."""
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    assert paths.config_path() == paths.code_root() / "config" / "projects.toml"


def test_a_config_in_psum_home_wins_over_the_shipped_one(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    own = tmp_path / "config" / "projects.toml"
    own.parent.mkdir(parents=True)
    own.write_text("[settings]\n")
    assert paths.config_path() == own
