"""The scaling study's tasks: deterministic, and labelled the way the rules read."""

from __future__ import annotations

import re

import pytest

from evals.multi_material_cases import RULES, SIZES, Case, Service, cases


def _current_config(text: str) -> str:
    return text.split("\nchangelog:")[0]


def _rule_check(service: Service) -> str | None:
    """An independent reading of the three rules, applied to the current config only."""
    config = _current_config(service.text)
    for block in re.findall(r"  - path: .*?(?=\n  - path:|\nrequest_timeout:)", config, re.S):
        method = re.search(r"method: (\w+)", block)
        retries = re.search(r"retries: (\d+)", block)
        assert method and retries
        idempotent = method.group(1) in {"GET", "PUT", "DELETE"} or (
            method.group(1) == "POST" and "idempotency-key" in block
        )
        if int(retries.group(1)) > 0 and not idempotent:
            return "R1"
    timeout = re.search(r"request_timeout: (\d+)(ms|s)\n", config)
    assert timeout
    seconds = int(timeout.group(1)) / (1000 if timeout.group(2) == "ms" else 1)
    if seconds > 30:
        return "R2"
    connections = re.search(r"\nmax_connections: (\d+)\n", config)
    assert connections
    if int(connections.group(1)) > 64:
        return "R3"
    return None


@pytest.mark.parametrize("split", ["dev", "evaluation"])
def test_every_label_matches_an_independent_reading_of_the_rules(split: str) -> None:
    for case in cases(split):
        for service in case.services:
            assert _rule_check(service) == service.violation, (case.id, service.id)


def test_splits_are_deterministic_disjoint_and_cover_every_size() -> None:
    dev, evaluation = cases("dev"), cases("evaluation")
    assert dev == cases("dev") and evaluation == cases("evaluation")
    assert sorted({c.size for c in dev}) == sorted({c.size for c in evaluation}) == list(SIZES)
    assert len(evaluation) == 4 * len(SIZES)
    dev_texts = {s.text for c in dev for s in c.services}
    assert not dev_texts & {s.text for c in evaluation for s in c.services}


def test_each_case_has_some_but_not_all_services_in_violation() -> None:
    for case in (*cases("dev"), *cases("evaluation")):
        assert isinstance(case, Case)
        assert [s.id for s in case.services] == [f"svc-{i:02d}" for i in range(1, case.size + 1)]
        assert 1 <= len(case.violations) < case.size


def test_distractors_put_old_violating_values_only_in_the_changelog() -> None:
    """Near misses are what make a careless reading fail, so they must be present."""
    services = [s for c in cases("evaluation") for s in c.services]
    histories = [s.text.split("\nchangelog:")[1] for s in services]
    assert sum("from 128 to 64" in h for h in histories) > len(services) // 4
    assert all("from 128" not in _current_config(s.text) for s in services)
    boundary = [s for s in services if re.search(r"request_timeout: (30s|30000ms)\n", s.text)]
    assert boundary and all(s.violation != "R2" for s in boundary)
    at_limit = [s for s in services if "\nmax_connections: 64\n" in s.text]
    assert at_limit and all(s.violation != "R3" for s in at_limit)


def test_no_material_is_changed_by_the_harness_credential_mask() -> None:
    """The harness masks credential-shaped text before a model sees it, and the kernel
    refuses input the mask would change; a task built on such text measures nothing."""
    from forgeharness.sdk import CredentialMask

    mask = CredentialMask()
    for case in (*cases("dev"), *cases("evaluation")):
        for service in case.services:
            assert mask.transform(service.text) == service.text, service.id


def test_the_shared_task_text_names_no_service() -> None:
    assert "svc-" not in RULES
