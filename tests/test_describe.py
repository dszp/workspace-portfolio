import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import scripts.describe as describe_mod
from scripts.config import load_config
from scripts.describe import build_prompt, gather_context, run
from scripts.descriptions import (
    Description,
    REDACTED_PLACEHOLDER,
    load_descriptions,
    project_key,
    save_descriptions,
)
from scripts.fsutil import atomic_write
from tests.conftest import cfg_for


def rec(name, path, *, category="Cat", redacted=False, content_hash="sha256:1"):
    return {
        "slug": f"-slug-{category}-{name}",
        "name": None if redacted else name,
        "category": category,
        "category_display": category,
        "path": None if redacted else str(path),
        "redacted": redacted,
        "content_hash": content_hash,
    }


def facts(*projects):
    return {"schema_version": 1, "projects": list(projects), "errors": []}


class Spy:
    """A fake generator that records every prompt it was called with and
    returns a fixed (or per-call) reply. Standing in for `claude -p` per the
    brief: tests must never shell out to the real thing.
    """

    def __init__(self, reply="A concrete, specific description."):
        self.calls: list[str] = []
        self._reply = reply

    def __call__(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        if callable(self._reply):
            return self._reply(prompt)
        return self._reply


def test_a_new_project_with_no_entry_is_generated(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha")
    gen = Spy("Does a specific thing.")
    entries, report = run(facts(p), cfg, {}, generate=gen)
    key = project_key(p, cfg.settings.root.resolve())
    assert report.generated == [key]
    assert entries[key] == Description(text="Does a specific thing.", source="ai", hash="sha256:1")
    assert len(gen.calls) == 1


def test_a_run_with_nothing_new_calls_the_generator_zero_times_and_writes_nothing(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha")
    key = project_key(p, cfg.settings.root.resolve())

    first_gen = Spy("Does a specific thing.")
    entries, _ = run(facts(p), cfg, {}, generate=first_gen)
    desc_path = tmp_path / "descriptions.toml"
    save_descriptions(desc_path, entries)

    # Second pass: same project, same content_hash, entry already on disk.
    reloaded = load_descriptions(desc_path)
    second_gen = Spy("should never be produced")
    entries2, report2 = run(facts(p), cfg, reloaded, generate=second_gen)

    assert second_gen.calls == []
    assert report2.generated == []
    assert entries2[key].text == "Does a specific thing."
    # Byte-identical: no diff on a routine re-run.
    assert save_descriptions(desc_path, entries2) is False


def test_manual_entry_is_never_regenerated_even_with_a_drifted_hash(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha", content_hash="sha256:new")
    key = project_key(p, cfg.settings.root.resolve())
    existing = {key: Description(text="Owner's own words.", source="manual", hash="sha256:old")}
    gen = Spy()
    entries, report = run(facts(p), cfg, existing, generate=gen)
    assert report.skipped_manual == [key]
    assert entries[key] == existing[key]
    assert gen.calls == []


def test_ai_entry_is_regenerated_when_its_hash_has_drifted(tmp_path):
    # Contrasts directly with the manual case above: same starting shape (an
    # existing entry, a hash that no longer matches the project), opposite
    # source, opposite outcome.
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha", content_hash="sha256:new")
    key = project_key(p, cfg.settings.root.resolve())
    existing = {key: Description(text="Stale.", source="ai", hash="sha256:old")}
    gen = Spy("Freshly regenerated.")
    entries, report = run(facts(p), cfg, existing, generate=gen)
    assert report.generated == [key]
    assert entries[key].text == "Freshly regenerated."
    assert len(gen.calls) == 1


def test_redacted_project_gets_the_placeholder_without_calling_the_generator(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("client-thing", tmp_path / "clients" / "acme", redacted=True)
    key = project_key(p, cfg.settings.root.resolve())
    gen = Spy()
    entries, report = run(facts(p), cfg, {}, generate=gen)
    assert entries[key] == Description(text=REDACTED_PLACEHOLDER, source="redacted", hash="sha256:1")
    assert report.placeholders == [key]
    assert gen.calls == []


def test_a_non_redacted_project_does_not_get_the_placeholder(tmp_path):
    # Contrasts with the redacted case above: same call shape, opposite flag,
    # opposite outcome -- proves the branch actually discriminates.
    cfg = cfg_for(tmp_path)
    p = rec("open-thing", tmp_path / "Cat" / "open-thing", redacted=False)
    key = project_key(p, cfg.settings.root.resolve())
    gen = Spy("A real generated description.")
    entries, report = run(facts(p), cfg, {}, generate=gen)
    assert entries[key].text == "A real generated description."
    assert entries[key].source == "ai"
    assert key not in report.placeholders
    assert len(gen.calls) == 1


def test_entry_for_a_vanished_project_is_retained_and_marked_stale(tmp_path):
    cfg = cfg_for(tmp_path)
    gone_key = "Cat/gone"
    still_here = rec("here", tmp_path / "Cat" / "here")
    existing = {
        gone_key: Description(text="A description the owner may have hand-written.",
                               source="manual", hash="sha256:x"),
    }
    gen = Spy("Newly generated.")
    entries, report = run(facts(still_here), cfg, existing, generate=gen)
    # Retained, not deleted, and its text is untouched.
    assert entries[gone_key].text == existing[gone_key].text
    assert entries[gone_key].stale is True
    assert report.newly_stale == [gone_key]


def test_a_reappearing_project_is_unmarked_stale(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("back-again", tmp_path / "Cat" / "back-again")
    key = project_key(p, cfg.settings.root.resolve())
    existing = {key: Description(text="Still true.", source="ai", hash="sha256:1", stale=True)}
    gen = Spy()
    entries, report = run(facts(p), cfg, existing, generate=gen)
    assert entries[key].stale is False
    assert report.unstaled == [key]
    assert gen.calls == []  # hash unchanged -> no regeneration needed either


def test_a_failed_generation_is_reported_and_does_not_stop_the_rest(tmp_path):
    cfg = cfg_for(tmp_path)
    ok = rec("ok", tmp_path / "Cat" / "ok")
    broken = rec("broken", tmp_path / "Cat" / "broken")
    ok_key = project_key(ok, cfg.settings.root.resolve())
    broken_key = project_key(broken, cfg.settings.root.resolve())

    def flaky(prompt: str) -> str | None:
        return None if "broken" in prompt else "Fine description."

    entries, report = run(facts(ok, broken), cfg, {}, generate=flaky)
    assert report.failed == [broken_key]
    assert broken_key not in entries
    assert entries[ok_key].text == "Fine description."


def test_all_flag_forces_regeneration_of_an_up_to_date_ai_entry(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha", content_hash="sha256:same")
    key = project_key(p, cfg.settings.root.resolve())
    existing = {key: Description(text="Old but hash-matching.", source="ai", hash="sha256:same")}
    without_all = Spy()
    _, report_default = run(facts(p), cfg, existing, generate=without_all)
    assert report_default.generated == []
    assert without_all.calls == []

    with_all = Spy("Regenerated by --all.")
    entries, report_all = run(facts(p), cfg, existing, generate=with_all, force_all=True)
    assert report_all.generated == [key]
    assert entries[key].text == "Regenerated by --all."


def test_only_flag_filters_which_projects_are_generated(tmp_path):
    cfg = cfg_for(tmp_path)
    match = rec("web-console", tmp_path / "Acme" / "web-console", category="Acme")
    other = rec("unrelated", tmp_path / "MISC" / "unrelated", category="MISC")
    match_key = project_key(match, cfg.settings.root.resolve())
    other_key = project_key(other, cfg.settings.root.resolve())
    gen = Spy("Generated.")
    entries, report = run(facts(match, other), cfg, {}, generate=gen, only="*web-console*")
    assert report.generated == [match_key]
    assert other_key not in entries
    assert len(gen.calls) == 1


def test_dry_run_calls_the_generator_zero_times_and_reports_what_would_be_generated(tmp_path):
    cfg = cfg_for(tmp_path)
    p = rec("alpha", tmp_path / "Cat" / "alpha")
    key = project_key(p, cfg.settings.root.resolve())
    gen = Spy("should never be produced")
    entries, report = run(facts(p), cfg, {}, generate=gen, dry_run=True)
    assert gen.calls == []
    assert report.would_generate == [key]
    assert key not in entries  # nothing was actually written into the entries


def test_gather_context_reads_first_paragraphs_and_package_json_description(tmp_path):
    (tmp_path / "README.md").write_text(
        "# My Project\n\nThis is the first paragraph of the readme.\nStill part of it.\n\n"
        "This second paragraph must not be included.\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "\n\n# Notes\n\nDurable ground rules go here.\n\nMore stuff not wanted.\n"
    )
    (tmp_path / "package.json").write_text(json.dumps({"description": "A tiny npm package."}))
    ctx = gather_context(tmp_path)
    assert ctx["readme"] == "This is the first paragraph of the readme. Still part of it."
    assert "second paragraph" not in ctx["readme"]
    assert ctx["claude_md"] == "Durable ground rules go here."
    assert ctx["pkg_description"] == "A tiny npm package."


def test_gather_context_tolerates_missing_files(tmp_path):
    ctx = gather_context(tmp_path)
    assert ctx == {"readme": "", "claude_md": "", "pkg_description": None, "entries": []}


def test_build_prompt_includes_name_category_and_gathered_context():
    record = {"name": "web-console", "category_display": "Acme"}
    context = {"readme": "Readme first paragraph.", "claude_md": "", "pkg_description": None}
    prompt = build_prompt(record, context)
    assert "web-console" in prompt
    assert "Acme" in prompt
    assert "Readme first paragraph." in prompt


def _run_main(repo, argv, **kw):
    describe_mod.__file__ = str(repo / "scripts" / "describe.py")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = describe_mod.main(argv, **kw)
    return rc, buf.getvalue()


def _fake_repo(tmp_path, project):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "state").mkdir()
    atomic_write(tmp_path / "state" / "facts.json", json.dumps(facts(project)))
    return tmp_path


def test_main_dry_run_lists_the_pending_project_and_calls_nothing(tmp_path):
    cfg = load_config()
    p = rec("alpha", cfg.settings.root.resolve() / "Cat" / "alpha")
    _fake_repo(tmp_path, p)
    key = project_key(p, cfg.settings.root.resolve())

    gen = Spy("should never run")
    rc, out = _run_main(tmp_path, ["--dry-run"], generate=gen)
    assert rc == 0
    assert gen.calls == []
    assert "1 project(s) would be generated" in out
    assert key in out
    assert not (tmp_path / "descriptions.toml").exists()


def test_main_writes_descriptions_toml_and_reports_the_generated_count(tmp_path):
    cfg = load_config()
    p = rec("alpha", cfg.settings.root.resolve() / "Cat" / "alpha")
    _fake_repo(tmp_path, p)
    gen = Spy("A generated description.")
    rc, out = _run_main(tmp_path, [], generate=gen)
    assert rc == 0
    assert "1 generated" in out
    saved = load_descriptions(tmp_path / "descriptions.toml")
    key = project_key(p, cfg.settings.root.resolve())
    assert saved[key].text == "A generated description."


def test_claude_generator_pins_the_model_gives_no_tools_and_sends_the_prompt_on_stdin(
    monkeypatch,
):
    """All three of these were real defects, so all three are asserted.

    Dropping --model puts ~80 trivial calls on whatever the interactive
    default happens to be; dropping --allowed-tools lets the subprocess crawl
    this repo instead of answering from the prompt; and moving the prompt back
    to argv makes the variadic --allowed-tools swallow it, which fails with
    "Input must be provided" rather than anything that names the cause.
    """
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout="  a description.  ", stderr="")

    monkeypatch.setattr(describe_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(describe_mod.subprocess, "run", fake_run)

    out = describe_mod.claude_generator("sonnet")("PROMPT BODY")

    assert out == "a description."
    argv = seen["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    # A restriction that restricts. `--allowed-tools ""` was measured NOT to:
    # it is an allowlist addition, and an empty addition adds nothing, so the
    # subprocess kept full tool access. Asserting the flag's mere presence is
    # what let that ship, so assert the named tools instead.
    assert "--allowed-tools" not in argv
    denied = argv[argv.index("--disallowed-tools") + 1:]
    for tool in ("Read", "Bash", "Glob", "Grep", "WebFetch", "Task"):
        assert tool in denied, tool
    assert seen["input"] == "PROMPT BODY"
    assert "PROMPT BODY" not in argv


def test_claude_generator_returns_none_rather_than_raising_when_claude_is_absent(
    monkeypatch,
):
    monkeypatch.setattr(describe_mod.shutil, "which", lambda _: None)
    assert describe_mod.claude_generator("sonnet")("PROMPT") is None


def test_claude_generator_runs_in_an_empty_directory_not_the_repo(monkeypatch, tmp_path):
    """Disabling tools does not stop `claude` from loading its working
    directory's own context into the system prompt. Run from this repo, that
    context is "86 projects / portfolio index / workspace scan", and it bled
    into two projects whose own prompts were thin -- one of which had correct
    README and CLAUDE.md paragraphs in the prompt and was described as a
    workspace scanner anyway.
    """
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="ok.", stderr="")

    monkeypatch.setattr(describe_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(describe_mod.subprocess, "run", fake_run)

    describe_mod.claude_generator("sonnet")("PROMPT")

    cwd = Path(seen["cwd"])
    assert cwd.is_dir()
    assert list(cwd.iterdir()) == []           # nothing for claude to read
    assert cwd.resolve() != Path.cwd().resolve()
