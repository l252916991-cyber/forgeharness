import json
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name,minimum", [("brotli", "1.2.1"), ("setuptools", "84.1.0")])
def test_optional_dependency_security_floors(name, minimum):
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    versions = [p["version"] for p in packages if p["name"] == name]
    assert versions and all(Version(v) >= Version(minimum) for v in versions)


def test_comparison_harness_security_fix_and_protocol_are_bound():
    packages = tomllib.loads((ROOT / "evals/comparison/environments/pydantic/uv.lock").read_text())[
        "package"
    ]
    installed = {p["name"]: p["version"] for p in packages}
    arm = json.loads((ROOT / "evals/comparison/protocol.json").read_text())["arms"]["pydantic"]
    assert Version(installed["pydantic-ai-harness"]) >= Version("0.59.0")
    assert arm["version"] == installed["pydantic-ai-harness"]
    assert arm["companion"] == "pydantic-ai-slim==" + installed["pydantic-ai-slim"]
