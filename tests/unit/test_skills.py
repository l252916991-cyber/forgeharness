"""Tests for strict Skill loading and capability enforcement."""

from pathlib import Path

import pytest

from forgeharness.skills.loader import SkillLoader, SkillLoadError, SkillRegistry


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str,
    tools: tuple[str, ...] = (),
    instructions: str = "Inspect evidence before concluding.",
) -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True)
    tools_yaml = (
        "allowed_tools:\n" + "\n".join(f"  - {tool}" for tool in tools)
        if tools
        else "allowed_tools: []"
    )
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        "description: Evidence-first review\n"
        f"{tools_yaml}\n"
        "---\n"
        f"{instructions}\n"
    )
    return path


def test_loader_and_registry_compile_selected_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "review", name="review", tools=("read_file", "git_diff"))
    skill = SkillLoader().load_directory(tmp_path)[0]

    compiled = SkillRegistry((skill,)).compile(
        ("review",), available_tools={"read_file", "git_diff"}
    )

    assert "<skill name='review' version='1.0.0'" in compiled
    assert "Inspect evidence" in compiled
    assert skill.source.endswith("review/SKILL.md")


def test_registry_rejects_unknown_duplicate_and_unavailable_tools(tmp_path: Path) -> None:
    skill = SkillLoader().load(
        write_skill(tmp_path, "review", name="review", tools=("dangerous_shell",))
    )
    registry = SkillRegistry((skill,))

    with pytest.raises(SkillLoadError, match="unknown skill"):
        registry.compile(("missing",), available_tools=set())
    with pytest.raises(SkillLoadError, match="selected skill names must be unique"):
        registry.compile(("review", "review"), available_tools=set())
    with pytest.raises(SkillLoadError, match="requests unavailable tools"):
        registry.compile(("review",), available_tools={"read_file"})


def test_loader_rejects_malformed_and_symlink_entry_points(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed" / "SKILL.md"
    malformed.parent.mkdir()
    malformed.write_text("no frontmatter")
    with pytest.raises(SkillLoadError, match="missing YAML frontmatter"):
        SkillLoader().load(malformed)

    valid = write_skill(tmp_path, "valid", name="valid")
    link = tmp_path / "linked-SKILL.md"
    link.symlink_to(valid)
    with pytest.raises(SkillLoadError, match=r"non-symlink SKILL\.md"):
        SkillLoader().load(link)


def test_loader_rejects_duplicate_names(tmp_path: Path) -> None:
    write_skill(tmp_path, "one", name="duplicate")
    write_skill(tmp_path, "two", name="duplicate")

    with pytest.raises(SkillLoadError, match="skill names must be unique"):
        SkillLoader().load_directory(tmp_path)
