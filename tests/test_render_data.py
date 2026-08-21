from datetime import datetime

from scripts.render_data import COLUMNS, render
from tests.test_query import facts, proj

NOW = datetime.fromisoformat("2026-08-21T12:00:00-04:00")


def rows(text: str) -> list[str]:
    """The data lines inside the fenced block."""
    inside = text.split("```")[1]
    return [ln for ln in inside.splitlines() if ln.strip()]


def test_header_names_exactly_the_fields_each_row_carries():
    out = render(facts(proj("alpha"), proj("beta")), NOW)
    assert f"Columns: `{'|'.join(COLUMNS)}`" in out
    for row in rows(out):
        assert len(row.split("|")) == len(COLUMNS)


def test_one_row_per_project():
    out = render(facts(proj("a"), proj("b"), proj("c")), NOW)
    assert len(rows(out)) == 3
    assert "**3 projects**" in out


def test_a_pipe_inside_a_reason_is_replaced_and_the_field_count_holds():
    # A stray pipe would shift every later field by one, silently
    # misattributing every value in the row.
    out = render(facts(proj("a", reasons=("branch ahead: dev|main",))), NOW)
    row = rows(out)[0]
    assert len(row.split("|")) == len(COLUMNS)
    assert "dev/main" in row


def test_multiple_reasons_join_with_semicolons():
    out = render(facts(proj("a", reasons=("one", "two"))), NOW)
    assert "one; two" in rows(out)[0]


def test_booleans_render_as_y_and_dash():
    out = render(facts(proj("a", handoff=True, half_plan=False)), NOW)
    cells = rows(out)[0].split("|")
    assert cells[COLUMNS.index("handoff")] == "Y"
    assert cells[COLUMNS.index("half_plan")] == "-"


def test_no_path_and_no_slug_appear_anywhere_in_the_note():
    # A non-redacted slug IS the absolute path with separators swapped, so
    # carrying it would reintroduce through the back door exactly what
    # dropping `path` removes.
    p = proj("alpha", slug="-home-someone-workspace-Cat-alpha",
             path="~/workspace/Cat/alpha")
    out = render(facts(p), NOW)
    assert "slug" not in COLUMNS
    assert "path" not in COLUMNS
    assert "-home-someone-workspace" not in out
    assert "~/workspace/Cat/alpha" not in out


def test_a_redacted_project_is_identified_by_its_digest_slug():
    p = proj("x", slug="redacted-abc123", redacted=True)
    p["name"] = None
    p["display_path"] = None
    row = rows(render(facts(p), NOW))[0]
    cells = row.split("|")
    # The name column carries the digest, matching PORTFOLIO-INDEX.md, so no
    # row is anonymous and the two notes agree about what to call it.
    assert cells[COLUMNS.index("name")] == "redacted-abc123"
    assert cells[COLUMNS.index("redacted")] == "Y"


def test_last_worked_is_date_only_and_age_carries_the_precision():
    row = rows(render(facts(proj("a", last="2026-08-18T12:00:00-04:00")), NOW))[0]
    cells = row.split("|")
    assert cells[COLUMNS.index("last_worked")] == "2026-08-18"
    assert cells[COLUMNS.index("age")] == "3d"


def test_the_scan_timestamp_is_present():
    # Without it a reader on a phone cannot tell a fresh answer from a
    # week-old one, which is this note's characteristic failure.
    assert "2026-08-21T05:30:00-04:00" in render(facts(proj("a")), NOW)


def test_rows_are_attention_ordered():
    out = render(facts(proj("low", attention=1), proj("high", attention=99)), NOW)
    assert rows(out)[0].startswith("high|")
