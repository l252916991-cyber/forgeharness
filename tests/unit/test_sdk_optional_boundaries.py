"""The base SDK and cloud contracts do not import optional Skills dependencies."""

import subprocess
import sys


def test_base_sdk_and_cloud_contract_import_without_pyyaml() -> None:
    script = """
import importlib.abc
import sys

class WithoutYaml(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'yaml' or fullname.startswith('yaml.'):
            raise ModuleNotFoundError('PyYAML intentionally unavailable', name='yaml')

sys.meta_path.insert(0, WithoutYaml())
from forgeharness.sdk import ApplicationSpec, CredentialMask, Harness, RunBudget
from forgeharness.cloud.execution_contract import PhysicalResult
assert 'yaml' not in sys.modules
try:
    from forgeharness.sdk import SkillInstaller
except ImportError as error:
    assert 'forgeharness[skills]' in str(error)
else:
    raise AssertionError('Skills must require its optional dependency')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
