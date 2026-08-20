from scripts.discovery import discover
from tests.conftest import cfg_for
from tests.fixtures.make_tree import build_tree


def names(cands):
    return sorted(c.path.name for c in cands)


def test_repos_are_discovered(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    assert "plain-repo" in found
    assert "dirty-repo" in found


def test_category_container_is_not_a_record(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    # Group's children DO carry markdown (README.md each), so this assertion can
    # only hold if the claimed-subtree subtraction actually runs. Delete that
    # subtraction and this test must fail.
    assert "Group" not in found
    assert "child-a" in found and "child-b" in found


def test_container_with_its_own_markdown_is_a_record(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    # "Docs-Group" holds a child repo AND loose markdown of its own.
    assert "Docs-Group" in found


def test_deep_non_repo_folder_with_unclaimed_markdown_is_a_record(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    assert "deep-notes" in found  # sits at depth 3


def test_nested_repo_gets_its_own_record_and_outer_excludes_it(tmp_path):
    build_tree(tmp_path)
    cands = {c.path.name: c for c in discover(cfg_for(tmp_path))}
    assert "inner-repo" in cands
    outer = cands["outer-repo"]
    assert any(p.name == "inner-repo" for p in outer.claimed_excludes)


def test_symlink_collapses_to_one_record_with_an_alias(tmp_path):
    build_tree(tmp_path)
    cands = [c for c in discover(cfg_for(tmp_path)) if c.path.name == "plain-repo"]
    assert len(cands) == 1
    assert any(a.name == "link-to-plain" for a in cands[0].aliases)


def test_excluded_glob_directories_are_skipped(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    assert "node_modules" not in found


def test_vendor_markdown_does_not_promote_a_wrapper_directory(tmp_path):
    build_tree(tmp_path)
    found = names(discover(cfg_for(tmp_path)))
    # Noise/pkg/node_modules/dep/README.md is the ONLY markdown under Noise/.
    # It is excluded, so neither Noise nor pkg has content of its own.
    assert "Noise" not in found
    assert "pkg" not in found
    assert "dep" not in found


def _container_with(tmp_path, relative_md):
    """A NON-repo category container holding one markdown file at `relative_md`,
    plus a real repo child so the container is a plausible container and not an
    empty directory.
    """
    (tmp_path / "Container" / "child-repo" / ".git").mkdir(parents=True)
    (tmp_path / "Container" / "child-repo" / "README.md").write_text("# child\n")
    md = tmp_path / "Container" / relative_md
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# doc\n")
    return discover(cfg_for(tmp_path))


def test_dot_directory_markdown_does_not_become_a_record(tmp_path):
    """`Reports/.claude/skills/some-skill` was a real record.

    A category container is not a repo, so it never claims its own tree, and
    every dot-directory beneath it is unclaimed. Any one of them holding
    markdown was promoted to a project on that basis.
    """
    found = names(_container_with(tmp_path, ".claude/skills/a-skill/SKILL.md"))
    assert "a-skill" not in found
    assert "skills" not in found and ".claude" not in found
    # ...and the container must not be promoted on the strength of markdown it
    # only holds inside a dot-directory either.
    assert "Container" not in found
    assert "child-repo" in found


def test_a_normal_directory_with_markdown_is_still_a_record(tmp_path):
    """The other side of the same conditional. Without this, a rule that simply
    promoted nothing would pass the test above.
    """
    found = names(_container_with(tmp_path, "notes/a-topic/GUIDE.md"))
    assert "a-topic" in found
    assert "child-repo" in found
