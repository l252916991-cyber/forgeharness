"""Default credential masking plus a host-supplied evidence transformation hook."""

import re
from typing import Any, Protocol

from forgeharness.observability.hash_chain import _redact


class EvidenceTransform(Protocol):
    """Apply the same deterministic JSON transformation before model and storage."""

    def transform(self, value: Any) -> Any: ...


class CredentialMask:
    """Mask common credential forms. Hosts must add domain PII/retention rules."""

    _keys = frozenset(
        {
            "api_key",
            "password",
            "authorization",
            "token",
            "secret",
            "access_token",
            "github_token",
            "model_credentials",
        }
    )
    _patterns = (
        re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}"),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~-]{8,}"),
        re.compile(r"(?i)\b(?:api[_-]?key|password|access_token|secret)\s*[:=]\s*[^\s,;]+"),
    )

    def transform(self, value: Any) -> Any:
        """Preserve JSON structure while masking recognized secret values."""
        value = _redact(value)
        if isinstance(value, str):
            for pattern in self._patterns:
                value = pattern.sub("[REDACTED]", value)
            return value
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower() in self._keys else self.transform(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.transform(item) for item in value]
        return value
