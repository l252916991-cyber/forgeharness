import io
import tarfile
import zipfile

import pytest
from test_session_verification import verifier as verifier

from benchmarks import session_verification as base
from benchmarks.business_verification import BusinessVerification
from benchmarks.framework_snapshot import snapshot


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_asset",
        "changed_asset",
        "sealed_distribution",
        "comparison_project",
    ],
)
def test_business_packages_bind_assets_and_exclude_sealed_inputs(verifier, fault):
    root = base.ROOT
    (root / "src/forgeharness/console.html").write_text("<h1>synthetic</h1>")
    (root / "data/synthetic").mkdir(parents=True)
    (root / "data/synthetic/page.txt").write_text("synthetic public fixture")
    (root / "data/sealed").mkdir()
    (root / "data/sealed/test.json").write_text("test data, not the project sealed set")
    project = root / "evals/comparison/environments/test"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("synthetic independent comparison project")
    gate = BusinessVerification(snapshot(root))
    wheel = root / "test.whl"
    sdist = root / "test.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative in gate.frozen["files"]:
            if relative.startswith("src/forgeharness/"):
                if relative.endswith(".html") and fault == "missing_asset":
                    continue
                payload = (root / relative).read_bytes()
                if relative.endswith(".html") and fault == "changed_asset":
                    payload = b"changed"
                archive.writestr(relative.removeprefix("src/"), payload)
    with tarfile.open(sdist, "w:gz") as archive:
        for relative in gate.frozen["files"]:
            if (
                relative.startswith(("examples/", "data/synthetic/"))
                or (relative.startswith("data/sealed/") and fault == "sealed_distribution")
                or (relative.startswith("evals/") and fault == "comparison_project")
            ):
                payload = (root / relative).read_bytes()
                member = tarfile.TarInfo("forgeharness-0.4.0/" + relative)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    if fault == "none":
        gate.check_archives(root, wheel, sdist)
    else:
        with pytest.raises(ValueError):
            gate.check_archives(root, wheel, sdist)
