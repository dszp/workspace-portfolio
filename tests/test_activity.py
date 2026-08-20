from pathlib import Path
from scripts.activity import slug_for, collect_activity, parse_herdr, herdr_snapshot


def test_slug_matches_the_claude_and_remember_convention():
    assert (
        slug_for(Path("/home/user/workspace/Acme/web-console"))
        == "-home-user-workspace-Acme-web-console"
    )


def make_remember(root: Path, slug: str, days: list[str], tail: str = "") -> None:
    d = root / slug
    d.mkdir(parents=True)
    for day in days:
        (d / f"today-{day}.done.md").write_text(f"work on {day}\n")
    (d / "recent.md").write_text(tail)


def make_sessions(root: Path, slug: str, n: int) -> None:
    d = root / slug
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"{i}.jsonl").write_text("{}\n")


def test_alias_slugs_are_merged_and_counts_summed(tmp_path):
    remember, claude = tmp_path / "rem", tmp_path / "cl"
    real = "-home-user-workspace-Reports-Monthly-Rollup"
    alias = "-home-user-workspace-Monthly-Rollup"
    make_remember(remember, real, ["2026-08-01"])
    make_remember(remember, alias, ["2026-08-10"])
    make_sessions(claude, real, 3)
    make_sessions(claude, alias, 48)

    a = collect_activity(
        Path("/home/user/workspace/Reports/Monthly-Rollup"),
        [Path("/home/user/workspace/Monthly-Rollup")],
        remember_root=remember,
        claude_root=claude,
        herdr={"sessions": {}, "panes": []},
    )
    assert a["claude_session_count"] == 51
    assert a["remember_last_day"] == "2026-08-10"      # newest across both slugs
    assert a["remember_slug"] == real                   # realpath slug stays canonical
    assert set(a["slugs"]) == {real, alias}


def test_remember_tail_takes_the_last_dated_section_only(tmp_path):
    remember, claude = tmp_path / "rem", tmp_path / "cl"
    slug = "-home-user-workspace-x"
    make_remember(
        remember,
        slug,
        ["2026-08-10"],
        tail=(
            "# Recent\n\n## 2026-08-09\n\nolder work\n\n"
            "## 2026-08-10\n\nnewest work here\n\n"
            "## Identity Candidates\n- IDENTITY CANDIDATE: noise\n"
        ),
    )
    make_sessions(claude, slug, 1)
    a = collect_activity(
        Path("/home/user/workspace/x"), [],
        remember_root=remember, claude_root=claude,
        herdr={"sessions": {}, "panes": []},
    )
    assert "newest work here" in a["remember_tail"]
    assert "older work" not in a["remember_tail"]
    assert "IDENTITY CANDIDATE" not in a["remember_tail"]


def test_missing_remember_and_claude_dirs_are_not_fatal(tmp_path):
    a = collect_activity(
        Path("/home/user/workspace/nope"), [],
        remember_root=tmp_path / "absent", claude_root=tmp_path / "absent2",
        herdr={"sessions": {}, "panes": []},
    )
    assert a["claude_session_count"] == 0
    assert a["remember_last_day"] is None


def test_herdr_pane_inside_the_project_is_detected(tmp_path):
    project = "/home/user/workspace/Acme/web-console"
    herdr = {
        "sessions": {"Acme": {"running": True}, "MISC": {"running": True}},
        "panes": [
            # Exact match on the project root.
            {"cwd": project, "workspace_id": "w1", "tab_id": "w1:t1",
             "agent_status": "idle", "session": "Acme"},
            # Descendant of the project.
            {"cwd": f"{project}/src", "workspace_id": "w2", "tab_id": "w2:t1",
             "agent_status": "working", "session": "Acme"},
            # Sibling sharing a string prefix but NOT a path boundary. A
            # startswith() without the separator would claim this foreign pane.
            {"cwd": "/home/user/workspace/Acme/web-console-demo",
             "workspace_id": "w3", "tab_id": "w3:t1",
             "agent_status": "idle", "session": "Acme"},
            # Unrelated project. Deleting the filter entirely would count this,
            # which is exactly the regression the old single-pane fixture missed.
            {"cwd": "/home/user/workspace/MISC/books", "workspace_id": "w4",
             "tab_id": "w4:t1", "agent_status": "done", "session": "MISC"},
        ],
    }
    a = collect_activity(
        Path(project), [], remember_root=tmp_path, claude_root=tmp_path, herdr=herdr
    )
    assert a["herdr_open_panes"] == 2
    assert a["herdr_session"] == "Acme"
    assert a["herdr_agent_status"] == "working"   # "working" outranks "idle"


def test_parse_herdr_extracts_panes_from_a_snapshot_payload():
    snapshot = {
        "result": {"snapshot": {"workspaces": [
            {"workspace_id": "w1", "label": "books", "tabs": [
                {"tab_id": "w1:t1", "panes": [
                    {"pane_id": "p1",
                     "cwd": "/home/user/workspace/MISC/books",
                     # Deliberately different from cwd: the shell has cd'd deeper
                     # than the pane's launch directory, and foreground_cwd is the
                     # one that reflects where work is actually happening. Equal
                     # values here would leave the priority untested.
                     "foreground_cwd": "/home/user/workspace/MISC/books/drafts",
                     "agent_status": "idle"}
                ]}
            ]}
        ]}}
    }
    panes = parse_herdr(snapshot, session="default")["panes"]
    assert len(panes) == 1
    assert panes[0]["cwd"] == "/home/user/workspace/MISC/books/drafts"
    assert panes[0]["workspace_id"] == "w1"
    assert panes[0]["tab_id"] == "w1:t1"
    assert panes[0]["agent_status"] == "idle"
    assert panes[0]["session"] == "default"


def test_parse_herdr_falls_back_to_cwd_when_foreground_is_absent():
    snapshot = {"result": {"snapshot": {"workspaces": [
        {"workspace_id": "w1", "tabs": [{"tab_id": "w1:t1", "panes": [
            {"cwd": "/home/user/workspace/MISC/books", "agent_status": None}]}]}
    ]}}}
    panes = parse_herdr(snapshot, session="s")["panes"]
    assert panes[0]["cwd"] == "/home/user/workspace/MISC/books"


def test_herdr_snapshot_degrades_to_empty_when_the_runner_fails():
    def missing_binary(args):
        raise FileNotFoundError("herdr is not installed")

    def garbage(args):
        return "this is not json"

    empty = {"sessions": {}, "panes": []}
    assert herdr_snapshot(runner=missing_binary) == empty
    assert herdr_snapshot(runner=garbage) == empty
