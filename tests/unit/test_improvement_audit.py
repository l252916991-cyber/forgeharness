import json
import zipfile

import pytest

from benchmarks.improvement_audit import inspect_host, public_names, sha256


def test_exact_host_wheel_and_missing_facade_export(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "app").mkdir()
    wheel = tmp_path / "vendor/forgeharness-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "forgeharness/sdk/__init__.py", 'class Harness: pass\n__all__ = ["Harness"]'
        )
    binding = tmp_path / "vendor/binding.json"
    binding.write_text(
        json.dumps({"version": "1.0.0", "sdk_api": 1, "wheel_sha256": sha256(wheel)})
    )
    (tmp_path / "app/host.py").write_text("from forgeharness.sdk import Harness, Missing\n")
    result = inspect_host(tmp_path, "vendor/binding.json", "app")
    assert result["wheel_matches_binding"]
    assert not result["facade_imports_valid"]
    assert result["imports"][0]["missing_exports"] == ["Missing"]
    assert not result["module_imports_valid"]
    binding.write_text(json.dumps({"version": "1.0.0", "sdk_api": 1, "wheel_sha256": "wrong"}))
    assert not inspect_host(tmp_path, "vendor/binding.json", "app")["wheel_matches_binding"]
    binding.write_text(json.dumps({"version": "../escape", "sdk_api": 1, "wheel_sha256": "wrong"}))
    with pytest.raises(ValueError, match="distribution version"):
        inspect_host(tmp_path, "vendor/binding.json", "app")


def test_no_inferred_export_list():
    with pytest.raises(ValueError, match="public export"):
        public_names("from somewhere import Harness")
