import dataclasses
from datetime import datetime
from pathlib import Path
from scripts.config import load_config
from scripts.descriptions import Description
from scripts.render_terminal import MAX_NAME, render, relative_age

NOW = datetime.fromisoformat("2026-08-18T19:00:00-04:00")
CFG = load_config(Path("config/projects.toml"))


def proj(name, category="Cat", status="active", attention=10, open_items=0,
         last="2026-08-18T12:00:00-04:00", redacted=False):
    return {"name": name, "category": category, "category_display": category,
            "redacted": redacted, "error": None,
            "derived": {"status": status, "attention": attention,
                        "open_items": open_items, "last_worked": last,
                        "attention_reasons": []}}


def facts(*projects):
    return {"schema_version": 1, "scanned_at": NOW.isoformat(),
            "projects": list(projects), "errors": []}


def test_relative_age_formats_compactly():
    assert relative_age("2026-08-18T17:00:00-04:00", NOW) == "2h"
    assert relative_age("2026-08-12T19:00:00-04:00", NOW) == "6d"
    assert relative_age("2026-05-18T19:00:00-04:00", NOW) == "3mo"
    assert relative_age(None, NOW) == "-"


def test_default_sort_is_attention_descending():
    # Names are chosen so alphabetical order is the OPPOSITE of attention order
    # ("alow" < "zhigh" alphabetically, but zhigh must sort first). A "sort by
    # name" implementation that drops the attention metric from the key would
    # pass under "high"/"low" (alphabetical happens to agree there) but fails
    # here, because it would put "alow" first.
    out = render(facts(proj("alow", attention=5), proj("zhigh", attention=90)),
                 CFG, color=False)
    assert out.index("zhigh") < out.index("alow")


def test_recent_sort_orders_by_last_worked():
    # Same reasoning as above: "aolder" < "znewer" alphabetically, opposite of
    # recency order, so a name-sort implementation is caught rather than
    # coincidentally passing.
    out = render(
        facts(proj("aolder", last="2026-01-01T00:00:00-04:00", attention=99),
              proj("znewer", last="2026-08-18T18:00:00-04:00", attention=1)),
        CFG, sort="recent", color=False)
    assert out.index("znewer") < out.index("aolder")


def test_dormant_is_hidden_by_default_and_shown_with_all():
    f = facts(proj("visible"), proj("sleepy", status="dormant"))
    assert "sleepy" not in render(f, CFG, color=False)
    assert "sleepy" in render(f, CFG, show_all=True, color=False)


def test_category_and_status_filters():
    # Brief used single-letter names ("a"/"b"); the bare letter "a" also occurs
    # inside fixed table chrome ("CATEGORY", "...ago"), so that assertion fails
    # under any renderer matching the brief's own reference implementation.
    # Distinctive names isolate the filter behavior from the chrome text.
    f = facts(proj("aproj", category="X"), proj("bproj", category="Y", status="stalled"))
    assert "bproj" not in render(f, CFG, category="X", color=False)
    assert "aproj" not in render(f, CFG, status="stalled", color=False)
    # The absences above pass just as well under a renderer whose whole body
    # is `return "no projects match those filters\n"` — only the matching row
    # actually surviving the filter proves the filter selects rather than
    # merely excludes.
    assert "aproj" in render(f, CFG, category="X", color=False)
    assert "bproj" in render(f, CFG, status="stalled", color=False)


def test_color_off_emits_no_escape_sequences():
    assert "\x1b[" not in render(facts(proj("a")), CFG, color=False)


def test_color_on_emits_escape_sequences():
    assert "\x1b[" in render(facts(proj("a")), CFG, color=True)


def test_redacted_project_with_no_name_falls_back_to_its_slug():
    # A redacted record's real name is null (scan.py's redact_record nulls it —
    # a doc-only project promoted purely by discovery can be named after the
    # client itself). Rendering must not print "None" or crash sorting.
    p = proj("ignored", redacted=True)
    p["name"] = None
    p["slug"] = "redacted-abc123"
    out = render(facts(p), CFG, color=False)
    assert "redacted-abc123" in out
    assert "None" not in out


def test_redacted_and_errored_projects_are_marked_not_dropped():
    f = facts(proj("client-thing", redacted=True))
    f["projects"][0]["error"] = None
    out = render(f, CFG, color=False)
    assert "client-thing" in out and "*" in out


def test_errored_projects_are_marked_rather_than_dropped():
    f = facts(proj("broken-thing"))
    f["projects"][0]["error"] = "git: unreadable HEAD"
    out = render(f, CFG, color=False)
    # A project that vanishes from the index is worse than one flagged unreadable.
    # Check the marker is attached to the row's name, not merely present
    # somewhere in the output — the footer legend for "!" also contains "!",
    # so a bare "'!' in out" check would pass even if the row lost its marker.
    assert "broken-thing!" in out


def test_category_is_blank_when_it_merely_repeats_the_project_name():
    out = render(facts(proj("Atlas", category="Atlas")), CFG, color=False)
    assert out.count("Atlas") == 1


def test_a_distinct_category_is_still_rendered():
    out = render(
        facts(
            proj("Atlas", category="Atlas"),
            proj("web-console", category="Acme"),
        ),
        CFG, color=False,
    )
    # Both halves are load-bearing: the first rules out "never blank", the second
    # rules out "always blank". One fixture row can only ever test one of them.
    assert out.count("Atlas") == 1
    assert "Acme" in out


def test_one_long_name_cannot_widen_the_table_for_every_other_row():
    long_name = "an-extremely-long-project-name-that-keeps-going-and-going"
    long_cat = "a-very-long-category-name-indeed"
    out = render(
        facts(proj(long_name, category=long_cat), proj("short", category="X")),
        CFG, color=False,
    )
    # Rendered clipped, not in full: without _clip the untruncated name appears
    # verbatim and sets the column width for all 2 rows.
    assert long_name not in out
    assert long_cat not in out
    assert "…" in out
    assert max(len(line) for line in out.splitlines()) <= 80


def test_markers_never_push_a_name_cell_past_the_cap():
    p = proj("x" * MAX_NAME, category="C")
    p["redacted"] = True
    p["error"] = "boom"
    out = render(facts(p), CFG, color=False)
    name_col = out.splitlines()[2].split("  ")[0]
    # Markers are reserved out of the budget, not appended on top of it.
    assert len(name_col) <= MAX_NAME
    assert name_col.endswith("*!")


def test_empty_result_set_says_so_rather_than_printing_a_bare_header():
    assert "no projects" in render(facts(), CFG, color=False).lower()


def _with_path(p, name, category="Cat"):
    p["path"] = str(CFG.settings.root.resolve() / category / name)
    return p


def _cfg_with_desc_rows(n):
    return dataclasses.replace(CFG, settings=dataclasses.replace(CFG.settings, desc_rows=n))


def test_description_shown_under_a_top_desc_rows_row_and_hidden_below_it():
    cfg = _cfg_with_desc_rows(1)
    top = _with_path(proj("top", attention=90), "top")
    bottom = _with_path(proj("bottom", attention=10), "bottom")
    descriptions = {
        "Cat/top": Description(text="What top does.", source="ai", hash="h"),
        "Cat/bottom": Description(text="What bottom does.", source="ai", hash="h"),
    }
    out = render(facts(top, bottom), cfg, color=False, descriptions=descriptions)
    assert "What top does." in out
    assert "What bottom does." not in out


def test_desc_flag_shows_the_description_for_every_visible_row():
    cfg = _cfg_with_desc_rows(1)
    top = _with_path(proj("top", attention=90), "top")
    bottom = _with_path(proj("bottom", attention=10), "bottom")
    descriptions = {
        "Cat/top": Description(text="What top does.", source="ai", hash="h"),
        "Cat/bottom": Description(text="What bottom does.", source="ai", hash="h"),
    }
    out = render(facts(top, bottom), cfg, color=False, descriptions=descriptions, show_desc=True)
    assert "What top does." in out
    assert "What bottom does." in out


def test_description_is_wrapped_and_indented_within_the_80_column_budget():
    p = _with_path(proj("wide", attention=90), "wide")
    long_text = "This project does a great many specific and concrete things " * 3
    descriptions = {"Cat/wide": Description(text=long_text.strip(), source="ai", hash="h")}
    out = render(facts(p), CFG, color=False, descriptions=descriptions, show_desc=True)
    desc_lines = [l for l in out.splitlines() if l.startswith("    ")]
    assert len(desc_lines) > 1, "a long description must wrap onto more than one line"
    assert all(len(l) <= 80 for l in desc_lines)


def test_description_is_dim_when_color_on_and_plain_when_off():
    p = _with_path(proj("a", attention=90), "a")
    descriptions = {"Cat/a": Description(text="Some description.", source="ai", hash="h")}
    plain = render(facts(p), CFG, color=False, descriptions=descriptions, show_desc=True)
    colored = render(facts(p), CFG, color=True, descriptions=descriptions, show_desc=True)
    assert "\x1b[" not in plain
    assert "\x1b[2m" in colored and "Some description." in colored


def test_main_reports_a_missing_facts_file_instead_of_traceback(monkeypatch, capsys):
    import scripts.render_terminal as rt
    monkeypatch.setattr(rt, "read_json", lambda *a, **k: None)
    assert rt.main([]) == 1
    assert "psum scan" in capsys.readouterr().err
