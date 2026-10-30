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
    "access_key", "accesskey", "access-key",
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

# Bare provider API keys. These are the highest-value secret in this
# codebase and were not matched by any earlier pattern: an
# ``Authorization:`` header or a ``key=value`` pair is redacted, but the same
# key appearing bare in a message, a tool argument, or a provider error body
# was logged verbatim. Provider keys are written into ``os.environ`` at
# startup, so any path that echoes them is a live leak.
#
# The vendor prefixes are explicit rather than a generic ``sk-`` rule: a bare
# ``sk-`` followed by 20+ alphanumerics also matches ordinary base64 blobs
# and long identifiers, and over-redacting makes logs unreadable.
_PROVIDER_KEY_RE = re.compile(
    r"(?P<key>"
    r"\b(?:"
    r"sk-ant-[A-Za-z0-9\-_]{20,}"       # Anthropic
    r"|sk-or-v1-[A-Za-z0-9]{20,}"       # OpenRouter
    r"|sk-proj-[A-Za-z0-9\-_]{20,}"     # OpenAI project keys
    r"|sk-[A-Za-z0-9]{32,}"             # OpenAI legacy / DeepSeek / Groq / Moonshot
    r"|gsk_[A-Za-z0-9]{20,}"            # Groq
    r"|xai-[A-Za-z0-9]{20,}"            # xAI
    r"|AIza[A-Za-z0-9\-_]{30,}"         # Google
    r"|gh[pousr]_[A-Za-z0-9]{30,}"      # GitHub
    r"|glpat-[A-Za-z0-9\-_]{20,}"       # GitLab
    r"|hf_[A-Za-z0-9]{30,}"             # HuggingFace
    r"|AKIA[0-9A-Z]{16}"                # AWS access key id
    # --- added for H6: vendors whose keys matched nothing above -----------
    r"|tvly-[A-Za-z0-9\-_]{20,}"        # Tavily
    r"|pplx-[A-Za-z0-9]{20,}"           # Perplexity
    r"|fw_[A-Za-z0-9]{20,}"             # Fireworks
    r"|r8_[A-Za-z0-9]{20,}"             # Replicate
    r"|jina_[A-Za-z0-9]{20,}"           # Jina
    r"|BSA[A-Za-z0-9\-_]{20,}"          # Brave Search
    r"|npm_[A-Za-z0-9]{30,}"            # npm automation token
    r"|pypi-[A-Za-z0-9\-_]{30,}"        # PyPI upload token
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{20,}"   # Stripe secret / restricted
    r"|dop_v1_[A-Za-z0-9]{40,}"         # DigitalOcean
    r"|shp(?:at|ss|ca|pa)_[A-Za-z0-9]{20,}"    # Shopify
    r"))\b"
)

# Zhipu / BigModel keys are ``<32 hex>.<16 alnum>`` — neither segment alone is
# long or distinctive enough for the generic entropy rule, and the ``.``
# separator means no vendor-prefix rule can catch it either.
_ZHIPU_KEY_RE = re.compile(r"\b(?P<key>[0-9a-fA-F]{32}\.[A-Za-z0-9]{16})\b")


# ---------------------------------------------------------------------------
# Generic high-entropy tokens
# ---------------------------------------------------------------------------
#
# Vendor-prefix matching is a losing game: every provider invents a new
# prefix, and a user's self-hosted or proxy endpoint can mint keys with no
# prefix at all. The rules above therefore only cover keys we happened to
# enumerate; anything else — a Tavily key before the prefix was known, a
# self-hosted gateway's bearer secret, an arbitrary ``apiKey`` field — was
# written to the log verbatim.
#
# This pass catches the *shape* of a credential instead of its name.
#
# Deliberate exclusions, so logs stay usable:
#   * pure-hex tokens (git SHAs, md5/sha digests, trace ids) — no vendor
#     issues a hex-only API key, and redacting every digest makes logs
#     undebuggable;
#   * anything shorter than 32 characters — a 20-char random string is as
#     likely to be a session id fragment as a key, and short tokens are
#     covered by the explicit vendor rules above;
#   * ``/`` and ``+`` are outside the character class, so filesystem paths
#     and URL paths (which are long, contain digits, and have high character
#     variety) do not match.

_ENTROPY_CANDIDATE_RE = re.compile(r"(?<![\w\-])(?P<tok>[A-Za-z0-9_\-]{32,200})(?![\w\-])")
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

#: Bits per character. Uniform random base62 is ~5.95; English prose is ~4.0
#: but is broken up by spaces so it never forms one long token.
_MIXED_CASE_ENTROPY = 3.6
#: A lowercase-only run is usually a long identifier (a test name, a config
#: key, a generated directory) rather than a credential, so it has to clear a
#: much higher bar before we destroy it in the log.
_LOWERCASE_ONLY_ENTROPY = 4.3
_MIN_DISTINCT_CHARS = 12


def _shannon_entropy(s: str) -> float:
    from collections import Counter
    from math import log2

    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((n / total) * log2(n / total) for n in counts.values())


def _looks_like_secret_token(tok: str) -> bool:
    """Heuristic: is *tok* shaped like a credential rather than an identifier?"""
    if len(tok) < 32:
        return False
    if _HEX_ONLY_RE.match(tok):
        return False
    has_lower = any(c.islower() for c in tok)
    has_upper = any(c.isupper() for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    if not has_digit or not (has_lower or has_upper):
        return False
    if len(set(tok)) < _MIN_DISTINCT_CHARS:
        return False
    entropy = _shannon_entropy(tok)
    if has_upper and has_lower:
        # Mixed case + digits is the shape every key generator produces.
        return entropy >= _MIXED_CASE_ENTROPY
    return entropy >= _LOWERCASE_ONLY_ENTROPY


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

    def _entropy_sub(m: re.Match[str]) -> str:
        tok = m.group("tok")
        return "***" if _looks_like_secret_token(tok) else tok

    out = _AUTHORIZATION_RE.sub(_auth_sub, text)
    out = _BEARER_RE.sub(_bearer_sub, out)
    out = _JWT_RE.sub(_jwt_sub, out)
    out = _PROVIDER_KEY_RE.sub("***", out)
    out = _ZHIPU_KEY_RE.sub("***", out)
    out = _KV_SECRET_RE.sub(_kv_sub, out)
    out = _PASSWORD_RE.sub(_kv_sub, out)
    # Runs after the named patterns so a recognised vendor key is replaced by
    # the same ``***`` either way; before the PII passes so an email-local-part
    # or phone-shaped digit run inside a token cannot mask it first.
    out = _ENTROPY_CANDIDATE_RE.sub(_entropy_sub, out)
    out = _CARD_RE.sub(_card_sub, out)
    out = _EMAIL_RE.sub("***@***", out)
    out = _CN_MOBILE_RE.sub("***", out)
    out = _PHONE_RE.sub("***", out)
    return out


__all__ = ["redact_sensitive"]
