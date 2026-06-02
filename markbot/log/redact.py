"""Sensitive-information redaction for log records.

Returns a *new* string with secrets, credentials, and personally
identifiable information replaced by ``***``. Designed to be cheap on
the hot path (pre-compiled patterns, single pass) and conservative on
false positives — we only redact when we recognise the surrounding
shape (header name, ``key=value`` pair, etc.).

Used by :func:`markbot.log.filter.default_filter` so the same rules
apply to both console and file sinks, and exposed publicly so unit
tests and other modules (e.g. memory redaction) can share the same
patterns.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# ``Authorization: <scheme> <token>`` -- keep scheme, replace token.
_AUTHORIZATION_RE = re.compile(
    r"(?P<header>authorization\s*:\s*)(?P<scheme>[A-Za-z][A-Za-z0-9\-]*)\s+(?P<token>[^\s,;]+)",
    re.IGNORECASE,
)

# ``Bearer <token>`` or ``Token <token>`` standalone (after JSON parse, etc.).
_BEARER_RE = re.compile(
    r"(?P<scheme>bearer|token|api[_\-]?key)\s+(?P<token>[A-Za-z0-9\-._~+/]+=*)",
    re.IGNORECASE,
)

# ``key=value`` pairs in URLs, JSON-ish dumps, and CLI flags. Captures
# a curated list of common secret names; everything else is left alone
# to avoid corrupting innocuous numbers.
_KV_SECRET_KEYS = (
    "api_key", "apikey", "api-key",
    "token",
    "access_token", "accesstoken", "access-token",
    "refresh_token", "refreshtoken", "refresh-token",
    "secret", "secret_key", "secretkey", "secret-key",
    "client_secret", "clientsecret", "client-secret",
    "private_key", "privatekey", "private-key",
    "session", "session_token", "sessiontoken",
    "x-api-key", "x-auth-token", "x-access-token",
)
_KV_SECRET_RE = re.compile(
    r"['\"]?(?P<key>(?:"
    + "|".join(re.escape(k) for k in _KV_SECRET_KEYS)
    + r"))['\"]?\s*[:=]\s*['\"]?(?P<val>[^,}\]\s]+?)['\"]?(?=\s*[,}\] ]|$)",
    re.IGNORECASE,
)

# ``"password" : "..."``  /  ``password=...``  /  ``pwd=...``
_PASSWORD_RE = re.compile(
    r"['\"]?(?P<key>password|passwd|pwd|pass)['\"]?\s*[:=]\s*['\"]?(?P<val>[^,}\]\s]+?)['\"]?(?=\s*[,}\] ]|$)",
    re.IGNORECASE,
)

# JSON Web Tokens: three base64url segments separated by dots.
_JWT_RE = re.compile(
    r"(?P<jwt>eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"
)

# Credit-card-shaped numbers: 13-19 digits with optional spaces/dashes.
# We require the Luhn checksum to fire — this avoids trashing ID numbers
# like order IDs that happen to be 16 digits.
_CARD_RE = re.compile(
    r"(?<!\d)(?P<card>(?:\d[ -]?){12,18}\d)(?!\d)"
)


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# Email -- required ``@`` and a TLD-shaped suffix. Conservative on
# internal-host strings to avoid false positives.
_EMAIL_RE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)

# China mobile (11 digits, leading 1, second digit 3-9). Standalone so
# it doesn't catch every 11-digit number.
_CN_MOBILE_RE = re.compile(
    r"(?<!\d)1[3-9]\d{9}(?!\d)"
)

# US-style phone numbers. We require *at least one* explicit separator
# (space, dash, or matched parens) so a bare 11-digit number (e.g. an
# order ID like ``10000000000``) does not get caught.
_PHONE_RE = re.compile(
    r"(?<![\w.\-])"
    r"(?:\+?\d{1,3}[ \-])?"        # optional country code with sep
    r"\(?\d{3}\)?[ \-]"            # 3-digit area code w/ sep on right
    r"\d{3}[ \-]\d{4}"             # 3 + 4 w/ sep between
    r"(?![\w.\-])"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def redact_sensitive(text: str) -> str:
    """Return *text* with secrets and PII replaced by ``***``.

    The function is pure: it never mutates its input and is safe to
    call on any string. Order of substitutions matters: we redact the
    most specific patterns first (Authorization, JWT) so that their
    replacements don't get partially masked by later passes.
    """
    if not text:
        return text

    def _auth_sub(m: re.Match[str]) -> str:
        return f"{m.group('header')}{m.group('scheme')} ***"

    def _bearer_sub(m: re.Match[str]) -> str:
        return f"{m.group('scheme')} ***"

    def _kv_sub(m: re.Match[str]) -> str:
        return f"{m.group('key')}=***"

    def _jwt_sub(m: re.Match[str]) -> str:
        return "***.jwt"

    def _card_sub(m: re.Match[str]) -> str:
        return "***" if _luhn_ok(m.group("card").replace(" ", "").replace("-", "")) else m.group(0)

    out = _AUTHORIZATION_RE.sub(_auth_sub, text)
    out = _BEARER_RE.sub(_bearer_sub, out)
    out = _JWT_RE.sub(_jwt_sub, out)
    out = _KV_SECRET_RE.sub(_kv_sub, out)
    out = _PASSWORD_RE.sub(_kv_sub, out)
    out = _CARD_RE.sub(_card_sub, out)
    out = _EMAIL_RE.sub("***@***", out)
    out = _CN_MOBILE_RE.sub("***", out)
    out = _PHONE_RE.sub("***", out)
    return out


__all__ = ["redact_sensitive"]
