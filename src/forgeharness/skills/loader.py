"""Strict loader for operator-installed `SKILL.md` packages."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from forgeharness.domain.models import FrozenModel


class SkillLoadError(ValueError):
    """A Skill package is malformed, ambiguous, or requests unavailable tools."""


class SkillManifest(FrozenModel):
    """Versioned metadata and declared tool capabilities from YAML frontmatter."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=500)
    allowed_tools: tuple[str, ...] = ()


class LoadedSkill(FrozenModel):
    """Validated Skill instructions plus their source provenance."""

    manifest: SkillManifest
    instructions: str = Field(min_length=1, max_length=20_000)
    source: str

    def validate_capabilities(self, available_tools: set[str]) -> None:
        """Reject capabilities not present in the composed server-side registry."""
        unavailable = sorted(set(self.manifest.allowed_tools) - available_tools)
        if unavailable:
            raise SkillLoadError(
                f"skill {self.manifest.name} requests unavailable tools: {', '.join(unavailable)}"
            )


class SkillLoader:
    """Load non-symlink Skill directories in deterministic path order."""

    def load_directory(self, root: Path) -> tuple[LoadedSkill, ...]:
        """Load every immediate child containing a strict `SKILL.md`."""
        resolved_root = root.resolve(strict=True)
        skills: list[LoadedSkill] = []
        for directory in sorted(resolved_root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            path = directory / "SKILL.md"
            if not path.is_file() or path.is_symlink():
                continue
            skills.append(self.load(path))
        names = [skill.manifest.name for skill in skills]
        if len(set(names)) != len(names):
            raise SkillLoadError("skill names must be unique")
        return tuple(skills)

    def load(self, path: Path) -> LoadedSkill:
        """Parse YAML frontmatter and non-empty Markdown instructions."""
        if path.name != "SKILL.md" or path.is_symlink():
            raise SkillLoadError("skill entry point must be a non-symlink SKILL.md")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise SkillLoadError(f"skill {path} is missing YAML frontmatter")
        try:
            frontmatter, instructions = text[4:].split("\n---\n", 1)
        except ValueError as exc:
            raise SkillLoadError(f"skill {path} has unterminated YAML frontmatter") from exc
        try:
            raw = yaml.safe_load(frontmatter)
            if not isinstance(raw, dict):
                raise SkillLoadError(f"skill {path} frontmatter must be a mapping")
            manifest = SkillManifest.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise SkillLoadError(f"invalid skill manifest in {path}: {exc}") from exc
        stripped = instructions.strip()
        try:
            return LoadedSkill(manifest=manifest, instructions=stripped, source=str(path))
        except ValidationError as exc:
            raise SkillLoadError(f"invalid skill instructions in {path}: {exc}") from exc


class SkillRegistry:
    """Select Skills and compile their instructions after capability validation."""

    def __init__(self, skills: tuple[LoadedSkill, ...]) -> None:
        self._skills = {skill.manifest.name: skill for skill in skills}
        if len(self._skills) != len(skills):
            raise SkillLoadError("skill names must be unique")

    def compile(self, names: tuple[str, ...], *, available_tools: set[str]) -> str:
        """Compile selected Skills in requested order with source and version labels."""
        if len(set(names)) != len(names):
            raise SkillLoadError("selected skill names must be unique")
        selected: list[LoadedSkill] = []
        for name in names:
            skill = self._skills.get(name)
            if skill is None:
                raise SkillLoadError(f"unknown skill: {name}")
            skill.validate_capabilities(available_tools)
            selected.append(skill)
        return "\n\n".join(
            (
                f"<skill name={skill.manifest.name!r} version={skill.manifest.version!r} "
                f"source={skill.source!r}>\n{skill.instructions}\n</skill>"
            )
            for skill in selected
        )
