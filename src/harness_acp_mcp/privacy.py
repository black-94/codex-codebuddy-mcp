from __future__ import annotations

import json
import re
from typing import Any

# A harness account object is only ever echoed to MCP callers for display. Real ACP
# servers (CodeBuddy's ``_codebuddy.ai/getUserInfo`` in particular) return live
# credentials such as ``token`` and ``accessToken`` inside that object, so the public
# result must be rebuilt from an explicit whitelist of non-secret identity fields
# instead of forwarding the harness object verbatim.
ACCOUNT_FIELDS = frozenset({
    "userId",
    "user_id",
    "username",
    "userName",
    "name",
    "displayName",
    "nickname",
    "email",
})

REDACTED = "[redacted]"

# Key names that mark a value as a credential. Matched case-insensitively against
# object keys only (never against free text), so a harness that emits a credential in
# a JSON field can never have it persisted by the output log.
_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|passphrase|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|cookie|session[_-]?key",
    re.IGNORECASE,
)

# Free-text (stderr lines, unparsable strings) has no structure to key off, so mask a
# credential-looking ``key: value`` / ``key=value`` assignment and scheme-prefixed
# secrets (``Bearer``/``Basic``/``token``) as a best-effort second line of defense.
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z0-9_.\"'-]*(?:token|secret|password|passwd|passphrase|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|cookie|"
    r"session[_-]?key)[A-Za-z0-9_.\"'-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\n,}]+)",
    re.IGNORECASE,
)
_SCHEME_SECRET = re.compile(
    r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=-]{6,})"
)


def is_sensitive_key(name: Any) -> bool:
    return isinstance(name, str) and _SENSITIVE_KEY.search(name) is not None


def public_account(value: Any) -> dict[str, Any] | None:
    """Return the whitelisted, display-only subset of a harness account object.

    Only scalar values of whitelisted identity fields survive. Credentials and any
    other unrecognized field are dropped. Returns ``None`` when nothing safe remains,
    which keeps ``authenticated`` derived independently of the field whitelist.
    """
    if not isinstance(value, dict):
        return None
    account: dict[str, Any] = {}
    for key, item in value.items():
        if key not in ACCOUNT_FIELDS or is_sensitive_key(key):
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            continue
        if isinstance(item, str) and not item.strip():
            continue
        account[str(key)] = item
    return account or None


def redact_sensitive(value: Any) -> Any:
    """Recursively replace credentials with a placeholder without mutating the input.

    Used on every record persisted to the harness output log and on harness/error text
    surfaced to callers, so an authentication response's token is neither written to
    disk nor echoed back. Structured objects are redacted by key; strings are redacted
    as embedded JSON when possible and otherwise by credential-looking text patterns.
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(key) else redact_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(text: str) -> str:
    stripped = text.strip()
    if stripped[:1] in {"{", "["}:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            return json.dumps(redact_sensitive(parsed), ensure_ascii=False)
    masked = _SCHEME_SECRET.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    return _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", masked
    )
