"""Deterministic multi-material audit tasks for the collaboration scaling study.

Materials deliberately contain no credential-shaped text: the harness masks such
text before any model sees it (``CredentialMask``) and refuses kernel requests it
would change, so a secrets rule could not be audited by any arm.

Each task is a set of N independent service configurations audited against three
rules. Every service violates at most one rule, and every service carries
near-miss distractors: compliant values at the boundary and a changelog that
mentions old, violating values. The expected answer lives only in ``Case`` and is
read by the external grader, never by a prompt.

Synthetic and author-generated: a scaling probe, not a business benchmark.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

SIZES = (4, 8, 16, 32)
_SEEDS = {"dev": 1101, "evaluation": 2202}
_CASES_PER_SIZE = {"dev": 1, "evaluation": 4}

RULES = (
    "You are auditing service configurations against three rules.\n"
    "R1 Retries: an endpoint with retries greater than 0 must be idempotent. GET, PUT and "
    "DELETE are idempotent; POST is idempotent only if its headers include idempotency-key.\n"
    "R2 Timeout: request_timeout must be at most 30 seconds (30s and 30000ms are allowed).\n"
    "R3 Connections: max_connections must be at most 64.\n"
    "Only the current configuration counts; the changelog describes history and is not "
    "evidence of a current violation.\n"
    "Identify every service that currently violates at least one rule."
)

_NAMES = (
    "inventory-sync", "billing-api", "search-index", "notify-hub", "auth-gateway",
    "report-batch", "media-store", "geo-lookup", "order-router", "audit-log",
    "price-feed", "user-profile", "cart-service", "ledger-core", "ticket-desk",
    "sms-relay", "catalog-read", "fraud-score", "export-worker", "session-cache",
    "quota-meter", "image-resize", "payout-batch", "webhook-out", "config-store",
    "rate-limiter", "doc-render", "tax-calc", "address-check", "loyalty-points",
    "shipment-track", "refund-desk", "metrics-push", "feature-flags", "schema-reg",
    "chat-bridge", "survey-collect", "backup-agent", "cdn-purge", "label-print",
)  # fmt: skip
_TEAMS = ("logistics", "payments", "platform", "growth", "trust", "data", "retail")
_REGIONS = ("cn-shanghai-2", "cn-beijing-1", "cn-shenzhen-3", "cn-hangzhou-1")
_PATHS = ("items", "orders", "records", "jobs", "events", "accounts", "batches")


@dataclass(frozen=True)
class Service:
    id: str
    text: str
    violation: str | None  # "R1", "R2", "R3" or None; grader-only


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    size: int
    services: tuple[Service, ...]

    @property
    def violations(self) -> tuple[str, ...]:
        """Sorted IDs of violating services: the expected answer."""
        return tuple(s.id for s in self.services if s.violation is not None)


def _timeout(rng: random.Random, violating: bool) -> str:
    if violating:
        return rng.choice(("31s", "35s", "45s", "30500ms", "32000ms", "60s"))
    return rng.choice(("30s", "30000ms", "25s", "29s", "12s", "20000ms"))


def _connections(rng: random.Random, violating: bool) -> int:
    return rng.choice((72, 96, 100, 128)) if violating else rng.choice((16, 32, 48, 64))


def _endpoints(rng: random.Random, retry_violation: bool) -> list[str]:
    """Two or three endpoints; a violation is one retried POST without the header."""
    resource = rng.choice(_PATHS)
    safe_posts = (
        ("POST", rng.choice((1, 2, 3)), "[x-request-id, idempotency-key]"),
        ("POST", 0, "[x-request-id]"),
    )
    entries: list[tuple[str, int, str | None]] = [
        ("GET", rng.choice((0, 2, 3)), None),
        ("PUT", rng.choice((0, 1, 2)), None),
    ]
    if retry_violation:
        entries.append(("POST", rng.choice((1, 2, 3)), "[x-request-id]"))
    else:
        entries.append(rng.choice(safe_posts))
    rng.shuffle(entries)
    lines = ["endpoints:"]
    for index, (method, retries, headers) in enumerate(entries):
        suffix = "" if index == 0 else ("/import" if method == "POST" else f"/{index}")
        lines.append(f"  - path: /v1/{resource}{suffix}")
        lines.append(f"    method: {method}")
        lines.append(f"    retries: {retries}")
        if headers is not None:
            lines.append(f"    headers: {headers}")
    return lines


def _changelog(rng: random.Random) -> list[str]:
    """History that mentions old violating values, so reading it as current is wrong."""
    events = [
        f"raised request_timeout from 10s to {rng.choice((20, 25, 30))}s",
        f"reduced request_timeout from {rng.choice((45, 60, 90))}s after load tests",
        "lowered max_connections from 128 to 64 after a database incident",
        f"raised max_connections from 16 to {rng.choice((32, 48))}",
        f"set POST retries from {rng.choice((2, 3))} to 0 pending an idempotency key",
        "added idempotency-key header to the import endpoint",
        "upgraded runtime to python3.12",
        "enabled request tracing for all endpoints",
    ]
    picked = rng.sample(events, k=rng.randint(4, 6))
    months = sorted(rng.sample(range(1, 11), k=len(picked)), reverse=True)
    return ["changelog:"] + [
        f"  - 2026-{month:02d}-{rng.randint(1, 28):02d} {event}"
        for month, event in zip(months, picked, strict=True)
    ]


def _operations(rng: random.Random, name: str) -> list[str]:
    """Rule-irrelevant configuration, so each material has realistic bulk to read."""
    flags = rng.sample(
        ("dark-launch", "new-pricing", "bulk-export", "strict-validation", "async-writes",
         "shadow-traffic", "legacy-fallback", "regional-failover"),
        k=3,
    )  # fmt: skip
    dependencies = rng.sample(_NAMES, k=3)
    return [
        "deploy:",
        f"  replicas: {rng.choice((2, 3, 4, 6))}",
        f"  cpu: {rng.choice(('250m', '500m', '1'))}",
        f"  memory: {rng.choice(('256Mi', '512Mi', '1Gi', '2Gi'))}",
        f"  strategy: {rng.choice(('rolling', 'blue-green', 'canary'))}",
        "observability:",
        f"  log_level: {rng.choice(('info', 'warn', 'debug'))}",
        f"  metrics_port: {rng.choice((9090, 9100, 9464))}",
        f"  trace_sample_rate: {rng.choice(('0.01', '0.05', '0.1'))}",
        f"  dashboard: grafana/{name}",
        "feature_flags: [" + ", ".join(flags) + "]",
        "dependencies: [" + ", ".join(d for d in dependencies if d != name) + "]",
        f"health_check: /healthz every {rng.choice((5, 10, 15))}s",
    ]


def _service(rng: random.Random, index: int, name: str, violation: str | None) -> Service:
    service_id = f"svc-{index:02d}"
    lines = [
        f"service: {service_id} ({name})",
        f"owner: team-{rng.choice(_TEAMS)}",
        f"region: {rng.choice(_REGIONS)}",
        "runtime: python3.12",
        "",
        *_endpoints(rng, violation == "R1"),
        f"request_timeout: {_timeout(rng, violation == 'R2')}",
        "database:",
        f"  host: db-{index:02d}.internal",
        f"  user: {name.replace('-', '_')}_rw",
        f"  schema: {name.split('-')[0]}",
        f"cache_ttl: {rng.choice((60, 120, 300, 900))}s",
        f"max_connections: {_connections(rng, violation == 'R3')}",
        *_operations(rng, name),
        "",
        *_changelog(rng),
    ]
    return Service(id=service_id, text="\n".join(lines), violation=violation)


def _case(split: str, size: int, number: int) -> Case:
    rng = random.Random(_SEEDS[split] * 1000 + size * 10 + number)
    violators = max(1, min(size - 1, round(size * rng.uniform(0.2, 0.4))))
    flagged = set(rng.sample(range(1, size + 1), k=violators))
    names = rng.sample(_NAMES, k=size)
    services = tuple(
        _service(
            rng,
            index,
            names[index - 1],
            rng.choice(("R1", "R2", "R3")) if index in flagged else None,
        )
        for index in range(1, size + 1)
    )
    return Case(id=f"{split}-n{size:02d}-{number}", split=split, size=size, services=services)


def cases(split: str) -> tuple[Case, ...]:
    """All cases of one split, ordered by size then number."""
    if split not in _SEEDS:
        raise ValueError("split must be 'dev' or 'evaluation'")
    return tuple(
        _case(split, size, number)
        for size in SIZES
        for number in range(1, _CASES_PER_SIZE[split] + 1)
    )
