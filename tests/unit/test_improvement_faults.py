import pytest

from benchmarks.improvement_faults import GROUPS, junit_counts, run


def test_fault_evidence_requires_observed_tests_and_no_skips(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite tests="4" failures="1" errors="0" skipped="1"/></testsuites>'
    )
    assert junit_counts(path) == {"tests": 4, "failures": 1, "errors": 0, "skipped": 1}
    assert len(GROUPS) == 6
    with pytest.raises(ValueError, match="workspace reports"):
        run(tmp_path / "outside")
