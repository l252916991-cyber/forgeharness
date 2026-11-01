"""Deterministic policy decisions applied before tool execution.

Two policies live here. ``RiskBasedPolicy`` decides from a tool's declared risk
level alone, which is the correct default for a harness that must not guess what
a tool does. ``ParameterAwarePolicy`` refines that decision with the arguments the
call actually carries — which path it writes, which command it runs, how often the
same task has asked — because the interesting authorization question is almost
never "may this tool run" but "may this tool run *against* this target".

The refinement is deliberately argument-driven rather than model-driven: every
input is a value the model already put in the call, so a decision can be replayed
from the trace without re-deriving anything a provider said.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from threading import Lock
from time import monotonic
from typing import Protocol

from forgeharness.domain.models import FrozenModel, ToolCall
from forgeharness.tools.base import RiskLevel, ToolSpec

# Argument names a call may declare a target under. Only names a tool's own schema
# allows can appear in ``call.arguments``, so a name absent from this table is not
# a target we misunderstand — it is simply not a target this layer reasons about.
PATH_ARGUMENTS: tuple[str, ...] = (
    "path",
    "file_path",
    "target_path",
    "source_path",
    "destination_path",
    "directory",
    "dir",
    "paths",
)
COMMAND_ARGUMENTS: tuple[str, ...] = ("command", "cmd", "script", "shell")
URL_ARGUMENTS: tuple[str, ...] = ("url", "endpoint", "uri", "target_url")


class PolicyDecisionType(StrEnum):
    """Possible pre-execution policy outcomes."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(FrozenModel):
    """Policy outcome with a user- and trace-visible reason.

    ``rule`` names the predicate that produced the decision, or is ``None`` when
    the risk-level fallback decided. It exists so an operator reading a denial in
    the trace can find the one line of configuration responsible for it; a reason
    alone leaves them guessing which of a dozen rules fired.
    """

    type: PolicyDecisionType
    reason: str
    rule: str | None = None


class ToolPolicy(Protocol):
    """Interface for task-aware tool authorization."""

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Decide whether a normalized tool call may execute."""
        ...


class RiskBasedPolicy:
    """Allow reads, require approval for writes, and deny external effects by default."""

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Return the default least-authority decision for a tool risk level."""
        del task_id, call
        if spec.risk == RiskLevel.READ:
            return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="read-only tool")
        if spec.risk == RiskLevel.WRITE:
            return PolicyDecision(
                type=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="workspace mutation requires approval",
            )
        return PolicyDecision(
            type=PolicyDecisionType.DENY,
            reason=f"{spec.risk.value} tools are disabled by the default policy",
        )


# --------------------------------------------------------------------------- #
# Argument extraction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CallTargets:
    """Values from one call that a policy is allowed to reason about."""

    paths: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """True when the call declares no target at all."""
        return not (self.paths or self.commands or self.urls)


def _strings(value: object) -> tuple[str, ...]:
    """Coerce a declared argument to the string values it carries.

    Tools legitimately declare one path or a list of paths, and argv-style tools
    declare a list of tokens. Anything else is not a target this layer can reason
    about, so it is dropped rather than stringified: ``repr`` of a nested object is
    not a path, and matching rules against it would produce confident nonsense.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def extract_targets(call: ToolCall) -> CallTargets:
    """Read every declared target out of one call, in declaration order."""
    arguments = call.arguments

    def collect(names: Sequence[str]) -> tuple[str, ...]:
        found: list[str] = []
        for name in names:
            found.extend(_strings(arguments.get(name)))
        return tuple(found)

    return CallTargets(
        paths=collect(PATH_ARGUMENTS),
        commands=collect(COMMAND_ARGUMENTS),
        urls=collect(URL_ARGUMENTS),
    )


# --------------------------------------------------------------------------- #
# Path reasoning
# --------------------------------------------------------------------------- #

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class NormalizedPath:
    """A requested path plus the reason it cannot be trusted, if any.

    Normalization is lexical, not filesystem-backed, on purpose: policy runs before
    the tool does, so it must not stat the target — a denied call would otherwise
    still be able to probe which paths exist. Tools keep resolving symlinks when
    they execute; this layer only rejects what is decidable from the string alone.
    """

    raw: str
    path: str
    absolute: bool = False
    escapes_workspace: bool = False

    @property
    def valid(self) -> bool:
        """True when the path is relative and stays inside the workspace."""
        return not (self.absolute or self.escapes_workspace)


def normalize_workspace_path(raw: str) -> NormalizedPath:
    """Collapse ``.``/``..`` and report whether the result leaves the workspace."""
    if "\x00" in raw:
        return NormalizedPath(raw=raw, path="", escapes_workspace=True)
    candidate = raw.replace("\\", "/").strip()
    if candidate.startswith("/") or candidate.startswith("~") or _DRIVE_LETTER.match(candidate):
        return NormalizedPath(raw=raw, path="", absolute=True)
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return NormalizedPath(raw=raw, path="", escapes_workspace=True)
            parts.pop()
            continue
        parts.append(part)
    return NormalizedPath(raw=raw, path="/".join(parts) or ".")


def path_matches(pattern: str, path: str) -> bool:
    """Match a gitignore-flavoured pattern against a workspace-relative path.

    A pattern with no separator matches the file name at any depth, so ``*.pem``
    covers ``keys/prod.pem`` without the host enumerating directories. A pattern
    ending in ``/**`` matches the directory and everything beneath it.
    """
    normalized = pattern.replace("\\", "/").strip("/")
    if normalized in ("", "."):
        return path == "."
    if normalized.endswith("/**"):
        base = normalized[:-3]
        return path == base or path.startswith(base + "/")
    if "/" in normalized:
        return fnmatchcase(path, normalized)
    return fnmatchcase(path, normalized) or fnmatchcase(path.rsplit("/", 1)[-1], normalized)


@dataclass(frozen=True)
class PathRule:
    """Deny, gate, or allow a call whose target path matches ``pattern``."""

    pattern: str
    decision: PolicyDecisionType
    reason: str
    name: str = ""

    @property
    def rule_id(self) -> str:
        """Stable identifier used in the trace and in denial messages."""
        return f"path:{self.name or self.pattern}"


@dataclass(frozen=True)
class CommandRule:
    """Deny, gate, or allow a call whose command line matches ``pattern``."""

    pattern: str
    decision: PolicyDecisionType
    reason: str
    name: str = ""
    regex: bool = False

    @property
    def rule_id(self) -> str:
        """Stable identifier used in the trace and in denial messages."""
        return f"command:{self.name or self.pattern}"

    def matches(self, command: str) -> bool:
        """Report whether this rule applies to one declared command string."""
        if self.regex:
            return re.search(self.pattern, command) is not None
        return self.pattern in command


# A conservative starting set. These are the paths where a mistake is
# unrecoverable — a committed key cannot be uncommitted — so they are denied rather
# than gated behind an approval nobody reads carefully at 2am.
DEFAULT_SECRET_PATH_RULES: tuple[PathRule, ...] = (
    PathRule(".env", PolicyDecisionType.DENY, "environment files carry credentials", "env"),
    PathRule(".env.*", PolicyDecisionType.DENY, "environment files carry credentials", "env"),
    PathRule("*.pem", PolicyDecisionType.DENY, "key material must not enter the transcript", "pem"),
    PathRule("*.key", PolicyDecisionType.DENY, "key material must not enter the transcript", "key"),
    PathRule("id_rsa*", PolicyDecisionType.DENY, "private keys must not move through tools", "ssh"),
    PathRule(
        "id_ed25519*", PolicyDecisionType.DENY, "private keys must not move through tools", "ssh"
    ),
    PathRule(".git/**", PolicyDecisionType.DENY, "VCS internals belong to the harness", "git"),
    PathRule(".git", PolicyDecisionType.DENY, "VCS internals belong to the harness", "git"),
)

# Defence in depth, not a sandbox. A tool that composes its own fixed argv (as the
# coding ``run_tests`` does) never reaches these patterns; they exist for hosts that
# let a model name a command. The regexes are deliberately loose: a rule that misses
# an obfuscated variant is worse than one that occasionally over-denies, because an
# operator can widen a denial and cannot recall a deletion.
DEFAULT_DENIED_COMMANDS: tuple[CommandRule, ...] = (
    CommandRule(
        pattern=(
            r"\brm\b"
            r"(?=[^\n]*(?:-{1,2}[A-Za-z]*r[A-Za-z]*\b|--recursive\b))"
            r"(?=[^\n]*(?:-{1,2}[A-Za-z]*f[A-Za-z]*\b|--force\b))"
        ),
        decision=PolicyDecisionType.DENY,
        reason="recursive forced deletion is not an agent action",
        name="rm-rf",
        regex=True,
    ),
    CommandRule(
        pattern=r"\b(mkfs|mke2fs|diskutil\s+erase|fdisk)\b",
        decision=PolicyDecisionType.DENY,
        reason="filesystem formatting destroys the workspace irreversibly",
        name="mkfs",
        regex=True,
    ),
    CommandRule(
        pattern=r"\bdd\b[^\n]*\bof=/dev/",
        decision=PolicyDecisionType.DENY,
        reason="raw device writes escape the workspace",
        name="dd-device",
        regex=True,
    ),
    CommandRule(
        pattern=r"\|\s*(sudo\s+)?(ba|z|k|fi)?sh\b",
        decision=PolicyDecisionType.DENY,
        reason="piping fetched data into a shell executes unreviewed code",
        name="pipe-to-shell",
        regex=True,
    ),
    CommandRule(
        pattern=r"\b(curl|wget)\b[^\n]*\b(-o|-O|--output)\b[^\n]*\s/(etc|usr|bin|sbin)/",
        decision=PolicyDecisionType.DENY,
        reason="downloads may not overwrite system paths",
        name="download-to-system",
        regex=True,
    ),
    CommandRule(
        pattern=r"\bchmod\s+(-[A-Za-z]+\s+)*0?777\s+/(\s|$)",
        decision=PolicyDecisionType.DENY,
        reason="world-writable root is never a needed side effect",
        name="chmod-root",
        regex=True,
    ),
    CommandRule(
        pattern=r":\(\)\s*\{\s*:\s*\|\s*:",
        decision=PolicyDecisionType.DENY,
        reason="fork bombs exhaust the host, not the task",
        name="fork-bomb",
        regex=True,
    ),
    CommandRule(
        pattern=r"\bgit\s+push\b",
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        reason="publishing a branch leaves the workspace and needs a human",
        name="git-push",
        regex=True,
    ),
)


# --------------------------------------------------------------------------- #
# Rate ceilings
# --------------------------------------------------------------------------- #


class RateLimitScope(StrEnum):
    """What a ceiling counts over."""

    TASK = "task"
    TOOL = "tool"
    TASK_AND_TOOL = "task_and_tool"


@dataclass(frozen=True)
class RateLimit:
    """At most ``max_calls`` attempts per ``window_seconds`` for the given scope."""

    max_calls: int
    window_seconds: float
    scope: RateLimitScope = RateLimitScope.TASK_AND_TOOL
    tools: tuple[str, ...] = ()
    name: str = ""

    def __post_init__(self) -> None:
        """Reject ceilings that could never admit a call."""
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    @property
    def rule_id(self) -> str:
        """Stable identifier used in the trace and in denial messages."""
        window = f"{self.window_seconds:g}s"
        return f"rate:{self.name or f'{self.max_calls}/{window}'}"

    def applies_to(self, tool: str) -> bool:
        """Report whether this ceiling covers the named tool."""
        return not self.tools or tool in self.tools

    def key(self, task_id: str, tool: str) -> str:
        """Return the counter this call would consume."""
        if self.scope is RateLimitScope.TASK:
            return f"task:{task_id}"
        if self.scope is RateLimitScope.TOOL:
            return f"tool:{tool}"
        return f"task:{task_id}|tool:{tool}"


class SlidingWindowLimiter:
    """Count attempts per key inside a trailing window.

    Time is the one input to policy that a trace cannot replay, so the observed
    count is echoed in the denial reason: an operator comparing two identical calls
    that decided differently can see the counter that explains it. Hosts that need
    byte-exact replay leave their ceiling list empty and this class is never used.
    """

    #: Keys are dropped once a dict grows past this, so a long-lived process with
    #: many ephemeral task ids does not accumulate counters forever.
    _MAX_KEYS = 4_096

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        """Inject the clock so tests advance time instead of sleeping."""
        self._clock = clock
        self._lock = Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def record(self, key: str, *, window_seconds: float) -> int:
        """Record one attempt and return how many the window now holds."""
        now = self._clock()
        with self._lock:
            window = self._hits[key]
            cutoff = now - window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            window.append(now)
            count = len(window)
            if len(self._hits) > self._MAX_KEYS:
                self._evict(now)
            return count

    def count(self, key: str, *, window_seconds: float) -> int:
        """Read the current count without recording an attempt."""
        with self._lock:
            window = self._hits.get(key)
            if window is None:
                return 0
            cutoff = self._clock() - window_seconds
            return sum(1 for hit in window if hit > cutoff)

    def reset(self) -> None:
        """Forget every counter, which is what a new run of a reused policy wants."""
        with self._lock:
            self._hits.clear()

    def _evict(self, now: float) -> None:
        """Drop keys whose newest attempt is already outside any plausible window."""
        stale = [
            key for key, window in self._hits.items() if not window or now - window[-1] > 86_400
        ]
        for key in stale:
            del self._hits[key]


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #

_STRICTNESS = {
    PolicyDecisionType.ALLOW: 0,
    PolicyDecisionType.REQUIRE_APPROVAL: 1,
    PolicyDecisionType.DENY: 2,
}


class ParameterAwarePolicy:
    """Refine a risk-level decision with the targets a call actually names.

    Precedence, highest first — this order is the contract and is covered by tests:

    1. a path that is absolute or escapes the workspace  -> deny, always
    2. any matching path rule or command rule that denies -> deny
    3. a matching rule that wants approval               -> require_approval
    4. a matching rule that allows                        -> allow
    5. an attempt over a declared rate ceiling            -> deny
    6. the base policy's risk-level decision

    Matching rules are a set of predicates, not an ordered list: the strictest match
    wins. That way a host can bolt on a denial without auditing where it would have
    landed against every existing allowance, and reordering config can never silently
    widen authority. Rules are evaluated *before* the ceiling and the ceiling is
    charged *after* them, so an allowlisted path still consumes the same budget as an
    unlisted one.
    """

    def __init__(
        self,
        *,
        base: ToolPolicy | None = None,
        paths: Sequence[PathRule] = (),
        commands: Sequence[CommandRule] = (),
        rates: Sequence[RateLimit] = (),
        clock: Callable[[], float] = monotonic,
        reject_escaping_paths: bool = True,
    ) -> None:
        """Bind the rule sets. Empty rule sets make this behave like the base."""
        self._base = base or RiskBasedPolicy()
        self._paths = tuple(paths)
        self._commands = tuple(commands)
        self._rates = tuple(rates)
        self._reject_escaping_paths = reject_escaping_paths
        self._limiter = SlidingWindowLimiter(clock=clock)

    @property
    def limiter(self) -> SlidingWindowLimiter:
        """Expose the counter so a host can reset it between runs."""
        return self._limiter

    def evaluate(self, *, task_id: str, spec: ToolSpec, call: ToolCall) -> PolicyDecision:
        """Decide one call from its declared targets, then its risk level."""
        targets = extract_targets(call)
        if self._reject_escaping_paths:
            boundary = self._boundary_denial(targets.paths)
            if boundary is not None:
                return boundary
        matched = self._matching_rules(targets)
        if matched:
            strictest = max(matched, key=lambda decision: _STRICTNESS[decision.type])
            if strictest.type is PolicyDecisionType.DENY:
                return strictest
        ceiling = self._ceiling_denial(task_id=task_id, tool=spec.name)
        if ceiling is not None:
            return ceiling
        if matched:
            return max(matched, key=lambda decision: _STRICTNESS[decision.type])
        return self._base.evaluate(task_id=task_id, spec=spec, call=call)

    def _boundary_denial(self, paths: Sequence[str]) -> PolicyDecision | None:
        """Reject a path whose string form already proves it leaves the workspace."""
        for raw in paths:
            normalized = normalize_workspace_path(raw)
            if normalized.valid:
                continue
            kind = "absolute" if normalized.absolute else "outside the workspace"
            return PolicyDecision(
                type=PolicyDecisionType.DENY,
                reason=f"path {raw!r} is {kind}",
                rule="path.boundary",
            )
        return None

    def _matching_rules(self, targets: CallTargets) -> list[PolicyDecision]:
        """Collect the decision of every rule whose pattern the call satisfies."""
        matched: list[PolicyDecision] = []
        for raw in targets.paths:
            path = normalize_workspace_path(raw).path
            for path_rule in self._paths:
                if path_matches(path_rule.pattern, path):
                    matched.append(
                        PolicyDecision(
                            type=path_rule.decision,
                            reason=path_rule.reason,
                            rule=path_rule.rule_id,
                        )
                    )
        for command in targets.commands:
            for command_rule in self._commands:
                if command_rule.matches(command):
                    matched.append(
                        PolicyDecision(
                            type=command_rule.decision,
                            reason=command_rule.reason,
                            rule=command_rule.rule_id,
                        )
                    )
        return matched

    def _ceiling_denial(self, *, task_id: str, tool: str) -> PolicyDecision | None:
        """Charge the call against each ceiling and deny the first that overflows."""
        for limit in self._rates:
            if not limit.applies_to(tool):
                continue
            count = self._limiter.record(
                limit.key(task_id, tool), window_seconds=limit.window_seconds
            )
            if count > limit.max_calls:
                return PolicyDecision(
                    type=PolicyDecisionType.DENY,
                    reason=(
                        f"{count} calls in the last {limit.window_seconds:g}s exceeds "
                        f"the ceiling of {limit.max_calls}"
                    ),
                    rule=limit.rule_id,
                )
        return None


def limited_policy(
    *,
    max_calls: int,
    window_seconds: float,
    scope: RateLimitScope = RateLimitScope.TASK_AND_TOOL,
    tools: Sequence[str] = (),
    base: ToolPolicy | None = None,
    clock: Callable[[], float] = monotonic,
) -> ParameterAwarePolicy:
    """Build a policy whose only refinement is a rate ceiling.

    The common case — "this task may not issue more than N calls a minute" — should
    not require the caller to spell out a ``RateLimit`` and wrap it in a tuple.
    """
    return ParameterAwarePolicy(
        base=base,
        clock=clock,
        rates=(
            RateLimit(
                max_calls=max_calls,
                window_seconds=window_seconds,
                scope=scope,
                tools=tuple(tools),
            ),
        ),
    )
