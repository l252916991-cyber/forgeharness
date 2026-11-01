"""Security fixes must hold for fresh extra installs and isolated locked arms.

R1 findings: GHSA-3fx2-cqw9-xr3c, pypdf 6.21.4 and pyasn1 0.6.5.
These checks protect public requirement ranges as well as the resolved locks;
upgrading a lock alone must not leave new installations exposed to older builds.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
DEEPAGENTS = Path("evals/comparison/environments/deepagents")


@pytest.mark.parametrize(
    ("name", "extras", "affected", "fixed"),
    [
        ("pypdf", {"knowledge", "cli", "all"}, ("6.19.0", "6.21.2", "6.21.3"), "6.21.4"),
        ("langgraph", {"frameworks", "all"}, ("1.0.0", "1.2.11", "1.2.13"), "1.2.14"),
    ],
)
def test_optional_metadata_refuses_affected_versions_and_accepts_fixed_release(
    name, extras, affected, fixed
):
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    found = set()
    for extra, values in groups.items():
        for value in values:
            requirement = Requirement(value)
            if requirement.name != name:
                continue
            found.add(extra)
            assert requirement.url is None
            assert all(version not in requirement.specifier for version in affected), extra
            assert fixed in requirement.specifier, extra
    assert found == extras


def test_isolated_metadata_constrains_pyasn1_on_every_supported_platform():
    config = tomllib.loads((ROOT / DEEPAGENTS / "pyproject.toml").read_text())
    constraints = [
        Requirement(value)
        for value in config["tool"]["uv"].get("constraint-dependencies", [])
        if Requirement(value).name == "pyasn1"
    ]
    assert constraints, "the isolated arm needs its own decoder constraint"
    assert all(
        requirement.marker is None and requirement.url is None for requirement in constraints
    )
    assert not all("0.6.4" in requirement.specifier for requirement in constraints)
    assert all("0.6.5" in requirement.specifier for requirement in constraints)


@pytest.mark.parametrize(
    ("relative", "name", "minimum"),
    [
        (Path("uv.lock"), "pypdf", "6.21.4"),
        (Path("uv.lock"), "langgraph", "1.2.14"),
        (Path("uv.lock"), "langgraph-sdk", "0.4.6"),
        (DEEPAGENTS / "uv.lock", "pyasn1", "0.6.5"),
    ],
)
def test_resolved_security_fixes_are_present_in_their_actual_environment(relative, name, minimum):
    packages = tomllib.loads((ROOT / relative).read_text())["package"]
    versions = [Version(package["version"]) for package in packages if package["name"] == name]
    assert versions, f"{name} must be resolved in {relative}"
    assert all(version >= Version(minimum) for version in versions), (relative, name, versions)
