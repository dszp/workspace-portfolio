from pathlib import Path
from scripts.descriptions import (
    Description,
    load_descriptions,
    needs_regeneration,
    project_key,
    save_descriptions,
)


def test_load_descriptions_missing_file_returns_empty(tmp_path):
    assert load_descriptions(tmp_path / "nope.toml") == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "descriptions.toml"
    entries = {
        "a/b": Description(text="Does a thing.", source="ai", hash="sha256:x"),
        "c/d": Description(text="Owner's own words.", source="manual"),
    }
    save_descriptions(path, entries)
    assert load_descriptions(path) == entries


def test_save_descriptions_orders_by_key_regardless_of_insertion_order(tmp_path):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    save_descriptions(a, {
        "z/last": Description(text="z", source="ai", hash="h"),
        "a/first": Description(text="a", source="ai", hash="h"),
    })
    save_descriptions(b, {
        "a/first": Description(text="a", source="ai", hash="h"),
        "z/last": Description(text="z", source="ai", hash="h"),
    })
    assert a.read_text() == b.read_text()
    assert a.read_text().index('"a/first"') < a.read_text().index('"z/last"')


def test_save_descriptions_returns_false_when_content_is_unchanged(tmp_path):
    path = tmp_path / "descriptions.toml"
    entries = {"a/b": Description(text="Same.", source="ai", hash="h")}
    assert save_descriptions(path, entries) is True
    assert save_descriptions(path, dict(entries)) is False


def test_manual_source_never_needs_regeneration_even_with_a_drifted_hash():
    entry = Description(text="Owner tweaked this.", source="manual", hash="sha256:old")
    assert needs_regeneration(entry, "sha256:new") is False


def test_ai_source_needs_regeneration_when_the_hash_has_drifted():
    # Contrasts directly with the manual case above: same shape (an existing
    # entry, a hash that no longer matches), opposite source, opposite result.
    entry = Description(text="AI wrote this.", source="ai", hash="sha256:old")
    assert needs_regeneration(entry, "sha256:new") is True


def test_ai_source_does_not_need_regeneration_when_the_hash_still_matches():
    entry = Description(text="AI wrote this.", source="ai", hash="sha256:same")
    assert needs_regeneration(entry, "sha256:same") is False


def test_missing_entry_always_needs_generation():
    # No "first run vs later run" distinction anywhere: a project with no
    # entry gets one, whenever it first appears.
    assert needs_regeneration(None, "sha256:anything") is True


def test_project_key_is_the_path_relative_to_root_when_not_redacted():
    root = Path("/home/x/workspace")
    record = {"path": "/home/x/workspace/Acme/web-console", "slug": "irrelevant"}
    assert project_key(record, root) == "Acme/web-console"


def test_project_key_falls_back_to_slug_when_path_is_absent():
    # A redacted record's `path` is nulled by scan.py's redact_record --
    # using the real path here would put the client's folder name straight
    # into a git-tracked file.
    root = Path("/home/x/workspace")
    record = {"path": None, "slug": "redacted-abc123"}
    assert project_key(record, root) == "redacted-abc123"
