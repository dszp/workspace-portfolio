import json
import multiprocessing as mp
import os
from pathlib import Path
import pytest
from scripts.fsutil import state_lock, atomic_write, read_json, LockBusy


def test_atomic_write_creates_the_file(tmp_path):
    p = tmp_path / "out.txt"
    assert atomic_write(p, "hello\n") is True
    assert p.read_text() == "hello\n"


def test_atomic_write_reports_no_change_for_identical_content(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write(p, "same\n")
    assert atomic_write(p, "same\n") is False


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    atomic_write(tmp_path / "out.txt", "x\n")
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_creates_parent_directories(tmp_path):
    p = tmp_path / "a" / "b" / "out.txt"
    atomic_write(p, "deep\n")
    assert p.read_text() == "deep\n"


def test_read_json_returns_default_when_missing_or_corrupt(tmp_path):
    assert read_json(tmp_path / "nope.json", default={"a": 1}) == {"a": 1}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_json(bad, default=[]) == []


def test_read_json_round_trips(tmp_path):
    p = tmp_path / "ok.json"
    atomic_write(p, json.dumps({"k": "v"}))
    assert read_json(p) == {"k": "v"}


def test_atomic_write_publishes_via_a_rename_from_the_same_directory(tmp_path, monkeypatch):
    from scripts import fsutil

    calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((str(src), str(dst), Path(src).read_text()))
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", recording_replace)
    target = tmp_path / "out.txt"
    fsutil.atomic_write(target, "new content\n")

    # A direct path.write_text() implementation never reaches os.replace at all,
    # so len(calls) == 0 and this fails. Same-directory matters on its own:
    # rename is only atomic within one filesystem.
    assert len(calls) == 1, "must publish through exactly one rename"
    src, dst, staged = calls[0]
    assert src != dst
    assert Path(src).parent == Path(dst).parent
    assert staged == "new content\n", "full content must be staged before publishing"
    assert target.read_text() == "new content\n"


def test_atomic_write_leaves_the_old_file_intact_until_the_rename(tmp_path, monkeypatch):
    from scripts import fsutil

    target = tmp_path / "out.txt"
    target.write_text("old\n")
    seen = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append(Path(dst).read_text())
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", recording_replace)
    fsutil.atomic_write(target, "new and rather longer content\n")

    # This is the actual atomicity claim: at the instant before publishing, a
    # concurrent reader still sees the COMPLETE old file, never a truncated or
    # half-written one. A direct write would have clobbered it before this point.
    assert seen == ["old\n"]
    assert target.read_text() == "new and rather longer content\n"


def _hold(state_dir, started, release):
    with state_lock(Path(state_dir), timeout=5):
        started.set()
        release.wait(timeout=10)


def test_second_writer_is_refused_while_the_lock_is_held(tmp_path):
    started, release = mp.Event(), mp.Event()
    proc = mp.Process(target=_hold, args=(str(tmp_path), started, release))
    proc.start()
    try:
        assert started.wait(timeout=10)
        with pytest.raises(LockBusy):
            with state_lock(tmp_path, timeout=0.5):
                pass
    finally:
        release.set()
        proc.join(timeout=10)
        if proc.is_alive():          # never leave a process holding the lock —
            proc.terminate()         # it would wedge every later run and look
            proc.join(timeout=5)     # like an unrelated flake


def test_lock_is_reacquirable_after_release(tmp_path):
    with state_lock(tmp_path, timeout=1):
        pass
    with state_lock(tmp_path, timeout=1):
        pass  # no exception


def test_facts_error_distinguishes_missing_from_corrupt(tmp_path):
    from scripts.fsutil import facts_error

    missing = tmp_path / "facts.json"
    assert "run `psum scan`" in facts_error(missing)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    msg = facts_error(corrupt)
    assert "malformed" in msg
    # The corrupt message must NOT be the missing message: re-running a scan
    # is the fix for one and not the other, and a reader who is told the
    # wrong one loses the time it takes to run scan and get the same error.
    assert msg != facts_error(missing)
    assert str(corrupt) in msg
