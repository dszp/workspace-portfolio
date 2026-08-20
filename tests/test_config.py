from pathlib import Path
import textwrap
import pytest
from scripts.config import load_config, is_redacted, override_for


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "projects.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_defaults_apply_when_file_is_empty(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.settings.max_depth == 4
    assert cfg.settings.active_days == 21
    assert cfg.settings.parallelism == 1
    assert cfg.weights.dirty == 25
    assert cfg.weights.stall_max == 40
    assert cfg.settings.redact_prefixes == ("clients/",)


def test_settings_and_weights_override(tmp_path):
    cfg = load_config(write(tmp_path, """
        [settings]
        active_days = 30
        [weights]
        dirty = 40
    """))
    assert cfg.settings.active_days == 30
    assert cfg.weights.dirty == 40
    assert cfg.weights.unpushed == 15  # untouched default survives


def test_redaction_matches_by_prefix_not_exact_path(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert is_redacted(cfg, "clients/agmaas/integration-project")
    assert is_redacted(cfg, "clients/agmaas/integration-project/sos-integration")
    assert not is_redacted(cfg, "clientside-tools")  # prefix must respect the separator


def test_explicit_project_redaction_also_covers_descendants(tmp_path):
    cfg = load_config(write(tmp_path, """
        [projects."Acme/private-thing"]
        redact = true
    """))
    assert is_redacted(cfg, "Acme/private-thing/inner")


def test_project_override_returns_pinned_status_and_note(tmp_path):
    cfg = load_config(write(tmp_path, """
        [projects."Acme/web-console"]
        status = "active"
        note = "primary focus"
    """))
    ov = override_for(cfg, "Acme/web-console")
    assert ov.status == "active"
    assert ov.note == "primary focus"
    assert override_for(cfg, "Acme/other") is None


def test_unknown_status_value_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown status"):
        load_config(write(tmp_path, """
            [projects."a/b"]
            status = "in-progress"
        """))


def test_root_tilde_is_expanded(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert str(cfg.settings.root).startswith("/")
