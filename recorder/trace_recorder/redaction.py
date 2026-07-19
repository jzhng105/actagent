"""Redaction: secrets must never reach disk.

Redaction runs in the core, on event bodies before the trace write,
identically for every adapter. Built-in detectors:

- key names matching the KEY_NAME_RE pattern,
- values matching common credential shapes (JWTs, `sk-`/`ghp_`-style
  prefixes, cloud key patterns),
- connection strings with embedded passwords.

Matched values are replaced with ``"__REDACTED__:<name>"`` and the JSON
path is appended to the event's redaction list. Deployment config may add
site-specific value patterns.

Arguments get key-name + value-shape + connection-string detectors.
Results get only key-name detectors by default — result data legitimately
looks entropic.

This is best-effort pattern matching; the trace directory remains
sensitive (gitignore it; encrypt at rest where policy requires).
"""

from __future__ import annotations

import re
from typing import Any

KEY_NAME_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)")

# (name, pattern) pairs for credential-shaped values.
VALUE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # JWT: three dot-separated base64url segments, header starts with eyJ
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b")),
    # OpenAI / Anthropic / Stripe style prefixes
    ("api_key", re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}\b")),
    # GitHub tokens
    ("github_token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    # AWS access key id
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Google API key
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Slack tokens
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # PEM private key material
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# scheme://user:password@host — the password portion is the secret.
CONNECTION_STRING_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:([^@\s]+)@")


class Redactor:
    """Walks JSON-shaped data and redacts secrets in place (on a copy)."""

    def __init__(self, extra_value_patterns: list[tuple[str, str]] | None = None) -> None:
        self._value_patterns = list(VALUE_PATTERNS)
        for name, pattern in extra_value_patterns or []:
            self._value_patterns.append((name, re.compile(pattern)))

    def redact_args(self, obj: Any) -> tuple[Any, list[str]]:
        """Full detector set: key names, value shapes, connection strings."""
        paths: list[str] = []
        redacted = self._walk(obj, "$", paths, value_detectors=True)
        return redacted, paths

    def redact_result(self, obj: Any) -> tuple[Any, list[str]]:
        """Key-name detectors only."""
        paths: list[str] = []
        redacted = self._walk(obj, "$", paths, value_detectors=False)
        return redacted, paths

    def _walk(self, obj: Any, path: str, paths: list[str], *, value_detectors: bool) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                child_path = f"{path}.{key}"
                if KEY_NAME_RE.search(str(key)) and isinstance(value, (str, int, float)) and value != "":
                    out[key] = f"__REDACTED__:{key}"
                    paths.append(child_path)
                else:
                    out[key] = self._walk(value, child_path, paths, value_detectors=value_detectors)
            return out
        if isinstance(obj, list):
            return [
                self._walk(item, f"{path}[{i}]", paths, value_detectors=value_detectors)
                for i, item in enumerate(obj)
            ]
        if isinstance(obj, str) and value_detectors:
            return self._redact_string(obj, path, paths)
        return obj

    def _redact_string(self, value: str, path: str, paths: list[str]) -> str:
        # Connection strings: surgically remove only the password.
        if CONNECTION_STRING_RE.search(value):
            paths.append(path)
            return CONNECTION_STRING_RE.sub(
                lambda m: m.group(0)[: m.start(1) - m.start(0)] + "__REDACTED__:password@",
                value,
            )
        for name, pattern in self._value_patterns:
            if pattern.search(value):
                paths.append(path)
                if pattern.fullmatch(value.strip()):
                    return f"__REDACTED__:{name}"
                return pattern.sub(f"__REDACTED__:{name}", value)
        return value
